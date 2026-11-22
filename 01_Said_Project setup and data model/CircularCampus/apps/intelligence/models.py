"""D6 AI results and model versions (design 5.3.8/5.3.9, Figures 27/28; monitoring Fig. 48)."""
import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class AIFunction:
    CLASSIFY = "classify"
    EMBED = "embed"
    RANK = "rank"
    ACTION = "action"
    ASSISTANT = "assistant"
    REPORT = "report"
    CHOICES = [(CLASSIFY, "Classification & tagging"), (EMBED, "Semantic representation"), (RANK, "Matching & ranking"), (ACTION, "Sustainable-action advice"), (ASSISTANT, "Assistant"), (REPORT, "Report narrative")]


class ModelVersion(models.Model):
    """Provider/model/configuration actually used. Hidden provider revisions are stored as 'not disclosed' (AI-02)."""

    GEMINI = "gemini"
    LOCAL = "local-rules"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=20, choices=[(GEMINI, "Google Gemini API"), (LOCAL, "Local deterministic fallback (not AI)")])
    model = models.CharField(max_length=80)
    revision = models.CharField(max_length=80, default="not disclosed")
    function = models.CharField(max_length=12, choices=AIFunction.CHOICES)
    config_version = models.CharField(max_length=40)
    threshold_version = models.CharField(max_length=40, default="thresholds-v1")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "model", "function", "config_version"], name="uniq_model_version")]

    def __str__(self):
        return f"{self.provider}:{self.model} ({self.function}, {self.config_version})"

    @property
    def is_generative(self):
        return self.provider == self.GEMINI


class AITask(models.Model):
    """Exists before any provider response so timeouts and failures stay measurable."""

    QUEUED = "QUEUED"
    SUCCEEDED = "SUCCEEDED"
    FALLBACK = "FALLBACK"
    FAILED = "FAILED"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    function = models.CharField(max_length=12, choices=AIFunction.CHOICES)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # [UML: Association — ModelVersion 1 "configures" 0..* AITask (Figure 27)]
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="tasks")
    input_refs = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    input_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=10, choices=[(QUEUED, "Queued"), (SUCCEEDED, "Succeeded"), (FALLBACK, "Fallback used"), (FAILED, "Failed")], default=QUEUED)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    deadline_ms = models.PositiveIntegerField()
    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-queued_at"]


class AIResult(models.Model):
    CALIBRATED = "calibrated"
    UNCALIBRATED = "uncalibrated"
    UNAVAILABLE = "unavailable"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — AITask 1 "returns" 0..1 AIResult (Figure 27)]
    task = models.OneToOneField(AITask, on_delete=models.PROTECT, related_name="result")
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="results")
    function = models.CharField(max_length=12, choices=AIFunction.CHOICES)
    input_version = models.CharField(max_length=80, blank=True)
    output = models.JSONField(encoder=DjangoJSONEncoder, default=dict)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    confidence_status = models.CharField(max_length=12, choices=[(CALIBRATED, "Calibrated"), (UNCALIBRATED, "Uncalibrated"), (UNAVAILABLE, "Unavailable")], default=UNAVAILABLE)
    limitations = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    fallback = models.BooleanField(default=False)
    fallback_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def mark_unavailable(self, reason):
        """AIResult.markUnavailable(): record that no usable AI output exists (labelled fallback)."""
        self.fallback = True
        self.fallback_reason = reason[:200]
        self.confidence, self.confidence_status = None, self.UNAVAILABLE
        self.save(update_fields=["fallback", "fallback_reason", "confidence", "confidence_status"])
        return self

    @property
    def source_label(self):
        if self.fallback or not self.model_version.is_generative:
            return "Rule-based fallback — not generated by AI"
        return f"Gemini · {self.model_version.model}"


class AssetRepresentation(models.Model):
    """Cached embedding, valid only for the same input hash and model version (DR-04)."""

    # [UML: Composition — Asset 1 ◆—— 0..* AssetRepresentation "has" (Figure 27); cached vectors die with the asset]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.CASCADE, related_name="representations")
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="+")
    input_hash = models.CharField(max_length=64)
    vector = models.JSONField(encoder=DjangoJSONEncoder, )
    dimension = models.PositiveIntegerField()
    refresh_state = models.CharField(max_length=10, default="CURRENT")
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["asset", "model_version"], name="uniq_asset_representation")]


class MatchRecommendation(models.Model):
    PROPOSED = "PROPOSED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — AIResult 1 "produces" 0..* MatchRecommendation (Figures 18, 28)]
    ai_result = models.ForeignKey(AIResult, on_delete=models.PROTECT, related_name="matches")
    # [UML: Association — ResourceRequest 1 "receives" / "matches" 0..* MatchRecommendation (Figures 18, 28)]
    request = models.ForeignKey("exchange.ResourceRequest", on_delete=models.PROTECT, related_name="match_recommendations")
    # [UML: Association — Asset 1 "appears-in" / "recommends" 0..* MatchRecommendation (Figures 18, 28)]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="match_recommendations")
    rank = models.PositiveSmallIntegerField()
    score = models.DecimalField("suitability 0-100", max_digits=5, decimal_places=1)
    coverage = models.DecimalField(max_digits=4, decimal_places=2)
    components = models.JSONField(encoder=DjangoJSONEncoder, help_text="Observed feature values and weights; missing ones listed separately.")
    reason_codes = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    reasons = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    limitations = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    input_version = models.PositiveIntegerField(help_text="Request version scored.")
    status = models.CharField(max_length=10, default=PROPOSED, choices=[(PROPOSED, "Proposed"), (SELECTED, "Selected"), (REJECTED, "Rejected")])

    class Meta:
        ordering = ["ai_result", "rank"]

    def explain(self):
        """MatchRecommendation.explain(): reasons built only from the actual score components (FR-30)."""
        from .matching import one_line

        observed = self.components.get("observed", {})
        return one_line({"components": observed, "missing": self.components.get("missing", [])})


class HumanReview(models.Model):
    ACCEPTED = "ACCEPTED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    OVERRIDDEN = "OVERRIDDEN"
    SELECTED = "SELECTED"
    DEFERRED = "DEFERRED"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — AIResult 1 "has" / "reviews" 0..* HumanReview (Figures 17, 28)]
    ai_result = models.ForeignKey(AIResult, on_delete=models.PROTECT, related_name="reviews")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    # [UML: Association — MatchRecommendation 0..1 "reviewed-in" 0..* HumanReview (Figure 28)]
    selected_match = models.ForeignKey(MatchRecommendation, null=True, blank=True, on_delete=models.PROTECT, related_name="reviews")
    choice = models.CharField(max_length=10, choices=[(c, c.title()) for c in (ACCEPTED, EDITED, REJECTED, OVERRIDDEN, SELECTED, DEFERRED)])
    edits = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    reason = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-reviewed_at"]

    @classmethod
    def record(cls, ai_result, actor, choice, selected_match=None, edits=None, reason=""):
        """HumanReview.record(): keep the human decision separate from the original advice (AI-04)."""
        if selected_match is not None and selected_match.ai_result_id != ai_result.pk:
            raise ValueError("A selected match must belong to the same AI result.")
        return cls.objects.create(ai_result=ai_result, actor=actor, choice=choice, selected_match=selected_match, edits=edits or {}, reason=reason[:500])


class EvaluationRun(models.Model):
    """Monitoring/evaluation records (design 5.11.2-5.11.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    function = models.CharField(max_length=12, choices=AIFunction.CHOICES)
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT)
    dataset_version = models.CharField(max_length=40)
    baseline_version = models.CharField(max_length=40, default="keyword-baseline-v1")
    sample_size = models.PositiveIntegerField()
    metric_name = models.CharField(max_length=40)
    metric_value = models.DecimalField(max_digits=6, decimal_places=3, null=True)
    run_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)


class EvaluationObservation(models.Model):
    # [UML: Composition — EvaluationRun 1 ◆—— 0..* EvaluationObservation (Figure 48)]
    run = models.ForeignKey(EvaluationRun, on_delete=models.CASCADE, related_name="observations")
    case_id = models.CharField(max_length=40)
    expected = models.JSONField(encoder=DjangoJSONEncoder, )
    observed = models.JSONField(encoder=DjangoJSONEncoder, )
    correct = models.BooleanField(null=True)
    latency_ms = models.PositiveIntegerField(null=True)
    schema_ok = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "case_id"], name="uniq_eval_observation")]
