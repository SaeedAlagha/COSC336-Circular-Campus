"""Minimal server-side Gemini REST adapter (Gemini API v1beta).

Request shapes follow the official google-genai SDK v3.6.1 (checked in this
project): POST {base}/models/{model}:generateContent with the key in the
``x-goog-api-key`` header, JSON output via generationConfig.responseMimeType +
responseJsonSchema, and {base}/models/{model}:batchEmbedContents for vectors.

The key is read from settings (environment / .env) and never sent to the
browser, logged, or written to the database (NFR-11, NFR-19).
"""
from __future__ import annotations

import base64
import json
import math
import socket
import time
import urllib.error
import urllib.request

from django.conf import settings


class GeminiError(Exception):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH = "AUTH_FAILED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "AI_TIMEOUT"
    NETWORK = "NETWORK_ERROR"
    PROVIDER_DOWN = "PROVIDER_DOWN"
    BLOCKED = "SAFETY_BLOCKED"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    BAD_REQUEST = "BAD_REQUEST"

    MESSAGES = {
        NOT_CONFIGURED: "Gemini is not configured (no GEMINI_API_KEY).",
        AUTH: "Gemini rejected the API key.",
        MODEL_NOT_FOUND: "The configured Gemini model was not found.",
        RATE_LIMITED: "Gemini rate limit reached; try again shortly.",
        TIMEOUT: "Gemini did not answer within the time limit.",
        NETWORK: "Gemini could not be reached from this computer.",
        PROVIDER_DOWN: "Gemini is temporarily unavailable.",
        BLOCKED: "Gemini declined to answer this input.",
        INVALID_SCHEMA: "Gemini returned output that failed validation.",
        BAD_REQUEST: "Gemini rejected the request.",
    }

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail[:300]
        super().__init__(f"{code}: {self.MESSAGES.get(code, '')}")

    @property
    def safe_message(self):
        return self.MESSAGES.get(self.code, "The AI service failed.")


class GeminiClient:
    def __init__(self, config=None, opener=None):
        cfg = config or settings.GEMINI
        self.api_key = cfg.get("API_KEY", "")
        self.model = cfg.get("MODEL", "")
        self.embedding_model = cfg.get("EMBEDDING_MODEL", "")
        self.base_url = cfg.get("BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.timeout = float(cfg.get("TIMEOUT_SECONDS", 10))
        self.enabled = bool(cfg.get("ENABLED", True))
        self._open = opener or urllib.request.urlopen

    @property
    def configured(self):
        return self.enabled and bool(self.api_key) and bool(self.model)

    # ------------------------------------------------------------ transport ---
    def _request(self, method, path, body=None, timeout=None):
        if not self.configured:
            raise GeminiError(GeminiError.NOT_CONFIGURED)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base_url}/{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        try:
            with self._open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read(4_000_000)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
            except Exception:
                detail = ""
            status = exc.code
            if status in (401, 403) or (status == 400 and "API key" in detail):
                raise GeminiError(GeminiError.AUTH, f"HTTP {status}")
            if status == 404:
                raise GeminiError(GeminiError.MODEL_NOT_FOUND, f"HTTP 404")
            if status == 429:
                raise GeminiError(GeminiError.RATE_LIMITED, "HTTP 429")
            if status >= 500:
                raise GeminiError(GeminiError.PROVIDER_DOWN, f"HTTP {status}")
            raise GeminiError(GeminiError.BAD_REQUEST, f"HTTP {status}")
        except (socket.timeout, TimeoutError):
            raise GeminiError(GeminiError.TIMEOUT)
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise GeminiError(GeminiError.TIMEOUT)
            raise GeminiError(GeminiError.NETWORK, type(exc.reason).__name__ if hasattr(exc, "reason") else "")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GeminiError(GeminiError.INVALID_SCHEMA, "Response body was not JSON")

    # ----------------------------------------------------------- generation ---
    def generate_json(self, system, prompt, schema, timeout=None, temperature=0.2, images=None):
        """``images``: optional list of (mime_type, bytes) sent inline (bonus: photo classification)."""
        parts = [{"text": prompt}]
        for mime, data in images or []:
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema, "temperature": temperature},
        }
        data = self._request("POST", f"models/{self.model}:generateContent", body, timeout)
        if data.get("promptFeedback", {}).get("blockReason"):
            raise GeminiError(GeminiError.BLOCKED, data["promptFeedback"]["blockReason"])
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiError(GeminiError.INVALID_SCHEMA, "No candidates")
        cand = candidates[0]
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise GeminiError(GeminiError.BLOCKED, cand.get("finishReason"))
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        try:
            parsed = json.loads(text)
        except ValueError:
            raise GeminiError(GeminiError.INVALID_SCHEMA, "Model text was not valid JSON")
        if not isinstance(parsed, dict):
            raise GeminiError(GeminiError.INVALID_SCHEMA, "Expected a JSON object")
        return parsed, data.get("modelVersion") or "not disclosed"

    # ----------------------------------------------------------- embeddings ---
    def embed(self, texts, task_type="SEMANTIC_SIMILARITY", timeout=None):
        if not texts:
            return []
        model = f"models/{self.embedding_model}"
        body = {"requests": [{"model": model, "content": {"parts": [{"text": t[:6000]}]}, "taskType": task_type} for t in texts]}
        data = self._request("POST", f"{model}:batchEmbedContents", body, timeout)
        vectors = [e.get("values") for e in data.get("embeddings", [])]
        if len(vectors) != len(texts):
            raise GeminiError(GeminiError.INVALID_SCHEMA, "Embedding count mismatch")
        dims = {len(v or []) for v in vectors}
        if len(dims) != 1 or 0 in dims:
            raise GeminiError(GeminiError.INVALID_SCHEMA, "Inconsistent vector dimension")
        for v in vectors:
            if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) or not any(v):
                raise GeminiError(GeminiError.INVALID_SCHEMA, "Non-finite or zero vector")
        return vectors

    def list_models(self, timeout=None):
        return [m["name"] for m in self.list_model_details(timeout)]

    def list_model_details(self, timeout=None):
        """Model names with the API methods each one supports (e.g. generateContent)."""
        data = self._request("GET", "models?pageSize=200", None, timeout)
        return [{"name": m.get("name", "").replace("models/", ""), "methods": m.get("supportedGenerationMethods", [])} for m in data.get("models", [])]


def elapsed_ms(start):
    return int((time.monotonic() - start) * 1000)
