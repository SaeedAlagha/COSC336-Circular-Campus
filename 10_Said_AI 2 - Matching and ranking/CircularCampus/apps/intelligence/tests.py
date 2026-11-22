"""AI features 1–4 and the Gemini adapter, tested with mocks (no credentials, no network)."""
import io
import json
import socket
import urllib.error
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.core.testing import World
from apps.intelligence import matching
from apps.intelligence.gateway import AIGateway
from apps.intelligence.gemini import GeminiClient, GeminiError
from apps.intelligence.models import AIResult, AITask, HumanReview
from apps.intelligence.similarity import similarity_matrix

CFG = {"API_KEY": "test-key-not-real", "MODEL": "gemini-test", "EMBEDDING_MODEL": "emb-test", "BASE_URL": "https://example.invalid/v1beta", "TIMEOUT_SECONDS": 2, "ENABLED": True}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    calls = []

    def _open(req, timeout=None):
        calls.append((req, timeout))
        return FakeResp(json.dumps(payload).encode())
    _open.calls = calls
    return _open


def gen_payload(obj):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}], "modelVersion": "gemini-test-002"}


class GeminiAdapterTests(TestCase):
    def test_request_shape_key_in_header_and_parse(self):
        op = opener_returning(gen_payload({"a": 1}))
        out, rev = GeminiClient(CFG, opener=op).generate_json("sys", "prompt", {"type": "object"})
        req, timeout = op.calls[0]
        self.assertEqual(out, {"a": 1}); self.assertEqual(rev, "gemini-test-002"); self.assertEqual(timeout, 2)
        self.assertTrue(req.full_url.endswith("/models/gemini-test:generateContent"))
        self.assertEqual(req.get_header("X-goog-api-key"), "test-key-not-real")
        self.assertNotIn("test-key-not-real", req.full_url)
        body = json.loads(req.data)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", body["generationConfig"])

    def test_error_mapping(self):
        def raiser(exc):
            def _open(req, timeout=None):
                raise exc
            return _open
        cases = [
            (urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"")), GeminiError.RATE_LIMITED),
            (urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b"API key not valid")), GeminiError.AUTH),
            (urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b"")), GeminiError.MODEL_NOT_FOUND),
            (urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"")), GeminiError.PROVIDER_DOWN),
            (socket.timeout(), GeminiError.TIMEOUT),
            (urllib.error.URLError(OSError("dns")), GeminiError.NETWORK),
        ]
        for exc, code in cases:
            with self.assertRaises(GeminiError) as ctx:
                GeminiClient(CFG, opener=raiser(exc)).generate_json("s", "p", {})
            self.assertEqual(ctx.exception.code, code)
            self.assertNotIn("test-key", str(ctx.exception))

    def test_malformed_and_blocked_output_rejected(self):
        bad = {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
        with self.assertRaises(GeminiError) as ctx:
            GeminiClient(CFG, opener=opener_returning(bad)).generate_json("s", "p", {})
        self.assertEqual(ctx.exception.code, GeminiError.INVALID_SCHEMA)
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
        with self.assertRaises(GeminiError) as ctx:
            GeminiClient(CFG, opener=opener_returning(blocked)).generate_json("s", "p", {})
        self.assertEqual(ctx.exception.code, GeminiError.BLOCKED)

    def test_embeddings_validated(self):
        ok = {"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]}
        self.assertEqual(len(GeminiClient(CFG, opener=opener_returning(ok)).embed(["a", "b"])), 2)
        bad = {"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3]}]}
        with self.assertRaises(GeminiError):
            GeminiClient(CFG, opener=opener_returning(bad)).embed(["a", "b"])

    def test_not_configured_without_key(self):
        self.assertFalse(GeminiClient({**CFG, "API_KEY": ""}).configured)


class MatchingTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_worked_example_from_design(self):
        """Design 5.10.1: S=.90 C=.80 Q=1 L=.70 T=1 K=.60, E missing -> 85.3, coverage 95%."""
        W = matching.WEIGHTS
        comps = {"S": 0.9, "C": 0.8, "Q": 1.0, "L": 0.7, "T": 1.0, "K": 0.6}
        observed = sum(W[k] for k in comps)
        score = Decimal(100) * sum(W[k] * Decimal(str(v)) for k, v in comps.items()) / observed
        self.assertEqual(round(score, 1), Decimal("85.3"))
        self.assertEqual(observed, Decimal("0.95"))
        self.assertEqual(sum(W.values()), Decimal("1.00"))

    def test_hard_filters_run_before_ranking_and_top_five(self):
        for i in range(7):
            self.w.asset(tag=f"A-6{i:02d}", qty=3)
        self.w.asset(tag="A-690", qty=3, condition="FAIR")            # below minimum condition
        self.w.asset(tag="A-691", qty=3, description="VGA only", specifications="VGA", name="Old monitor")  # missing mandatory HDMI
        req = self.w.request(qty=2, mandatory_specs="HDMI")
        outcome, recs = matching.rank(self.w.requester, req)
        self.assertEqual(len(recs), 5)
        excluded = {e["asset_tag"] for e in outcome.output["excluded"]}
        self.assertTrue({"A-690", "A-691"} <= excluded)
        self.assertNotIn("A-690", [r.asset.asset_tag for r in recs])
        for r in recs:
            self.assertTrue(0 <= r.score <= 100)
            self.assertTrue(r.reasons)
        self.assertEqual(outcome.result.reviews.count(), 0, "viewing a match records no decision")
        from apps.exchange.models import Reservation
        self.assertFalse(Reservation.objects.exists(), "matching never reserves stock")

    def test_missing_factors_excluded_and_lower_coverage(self):
        self.w.asset(tag="A-650", qty=3, replacement=None)
        req = self.w.request(qty=2)
        outcome, recs = matching.rank(self.w.requester, req)
        comps = recs[0].components
        self.assertIn("K", comps["missing"])
        self.assertIn("E", comps["missing"])
        self.assertLess(recs[0].coverage, Decimal("1"))

    def test_semantic_fallback_matches_synonyms(self):
        sims = similarity_matrix("display screens for students", ["24-inch monitor with HDMI", "ergonomic office chair"])
        self.assertGreater(sims[0], sims[1])

    def test_gemini_embeddings_used_and_cached(self):
        self.w.asset(tag="A-660", qty=3)
        payload = {"embeddings": [{"values": [1.0, 0.0]}, {"values": [0.9, 0.1]}]}
        client = GeminiClient(CFG, opener=opener_returning(payload))
        req = self.w.request(qty=2)
        outcome, recs = matching.rank(self.w.requester, req, gateway=AIGateway(client))
        self.assertFalse(outcome.fallback)
        self.assertIn("Gemini embeddings", outcome.output["method"])
        from apps.intelligence.models import AssetRepresentation
        self.assertEqual(AssetRepresentation.objects.count(), 1)

