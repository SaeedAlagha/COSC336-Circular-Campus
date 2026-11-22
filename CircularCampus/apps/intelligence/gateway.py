"""AIGateway (design 5.2.1, process 6.1–6.5): every AI function runs through here.

* 6.1 the caller passes only minimal authorised context (it never queries the DB for the model);
* 6.3 the Gemini adapter runs with a bounded timeout;
* 6.4 output is validated by the caller-supplied validator;
* 6.5 an AITask + AIResult record provenance (model, configuration, input hash, latency,
  fallback reason) so original advice and the later human decision stay distinguishable.

If Gemini is unconfigured, unavailable or returns invalid output, a deterministic
fallback runs and is recorded and *labelled* as a rule-based fallback — never as AI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

from django.db import transaction
from django.utils import timezone

from .gemini import GeminiClient, GeminiError, elapsed_ms
from .models import AIResult, AITask, ModelVersion

log = logging.getLogger("campus.ai")

CONFIG_VERSIONS = {
    "classify": "classify-prompt-v1",
    "embed": "embed-v1",
    "rank": "rank-weights-v1",
    "action": "action-prompt-v1",
    "assistant": "assistant-prompt-v1",
    "report": "report-prompt-v1",
}
LOCAL_MODELS = {
    "classify": "keyword-category-rules-v1",
    "embed": "concept-similarity-v1",
    "rank": "concept-similarity-v1",
    "action": "action-hierarchy-rules-v1",
    "assistant": "retrieval-only-v1",
    "report": "template-narrative-v1",
}


def model_version(provider, model, function):
    mv, _ = ModelVersion.objects.get_or_create(provider=provider, model=model, function=function, config_version=CONFIG_VERSIONS[function])
    return mv


def input_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class AIOutcome:
    def __init__(self, result, output, fallback, message=""):
        self.result = result
        self.output = output
        self.fallback = fallback
        self.message = message


# [UML: Class AIGateway (Figure 17) — Dependency "creates" AIResult; has NO approve/release/dispose operation]
class AIGateway:
    """Has no approve, release or dispose operation by design (Figure 17)."""

    def __init__(self, client=None):
        self.client = client or GeminiClient()

    @property
    def live(self):
        return self.client.configured

    def invoke(self, **task):
        """AIGateway.invoke(task) — the design name for ``run``. There is no approve/release/dispose method."""
        return self.run(**task)

    @staticmethod
    def validate(output, required_keys=()):
        """AIGateway.validate(output): a result must be a JSON object containing the required keys."""
        return isinstance(output, dict) and all(k in output for k in required_keys)

    def run(self, *, function, user, input_refs, payload, ai_call, fallback, deadline_ms=15000, input_version="",
            limitations=None, model_name=None):
        """``ai_call(client) -> (output_dict, revision)`` must raise GeminiError or ValueError on invalid output.
        ``fallback(reason) -> output_dict`` is deterministic and never calls a provider."""
        digest = input_hash(payload)
        gemini_model = model_name or (self.client.embedding_model if function == "embed" else self.client.model)
        attempted = model_version(ModelVersion.GEMINI if self.live else ModelVersion.LOCAL, gemini_model if self.live else LOCAL_MODELS[function], function)
        with transaction.atomic():
            task = AITask.objects.create(function=function, requested_by=user, model_version=attempted, input_refs=input_refs,
                                         input_hash=digest, deadline_ms=deadline_ms, started_at=timezone.now())
        start = time.monotonic()
        output, revision, fallback_reason, error_code = None, "not disclosed", "", ""
        if self.live:
            task.attempt_count = 1
            try:
                output, revision = ai_call(self.client)
            except GeminiError as exc:
                error_code, fallback_reason = exc.code, exc.safe_message
                log.warning("Gemini %s failed: %s", function, exc.code)
            except (ValueError, KeyError, TypeError) as exc:
                error_code, fallback_reason = GeminiError.INVALID_SCHEMA, "AI output failed validation and was discarded."
                log.warning("Gemini %s output rejected: %s", function, type(exc).__name__)
        else:
            error_code, fallback_reason = GeminiError.NOT_CONFIGURED, "Gemini is not configured on this computer (no GEMINI_API_KEY)."
        used_fallback = output is None
        if used_fallback:
            output = fallback(fallback_reason)
            used_mv = model_version(ModelVersion.LOCAL, LOCAL_MODELS[function], function)
        else:
            used_mv = attempted
            if revision and revision != "not disclosed" and used_mv.revision != revision:
                used_mv.revision = str(revision)[:80]
                used_mv.save(update_fields=["revision"])
        with transaction.atomic():
            task.finished_at = timezone.now()
            task.latency_ms = elapsed_ms(start)
            task.state = AITask.FALLBACK if used_fallback else AITask.SUCCEEDED
            task.error_code = error_code
            task.save()
            result = AIResult.objects.create(
                task=task, model_version=used_mv, function=function, input_version=str(input_version)[:80], output=output,
                confidence=None, confidence_status=AIResult.UNAVAILABLE, limitations=list(limitations or []) + ([fallback_reason] if fallback_reason else []),
                fallback=used_fallback, fallback_reason=fallback_reason[:200],
            )
        return AIOutcome(result, output, used_fallback, fallback_reason)
