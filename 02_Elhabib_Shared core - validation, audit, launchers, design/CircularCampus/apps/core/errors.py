"""Stable failure codes from design 5.9.2."""


# [UML: Inheritance — BusinessError is-a Exception]
class BusinessError(Exception):
    """A rule violation with a stable code and a safe, actionable message."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    FORBIDDEN = "FORBIDDEN"
    STALE_VERSION = "STALE_VERSION"
    STOCK_CONFLICT = "STOCK_CONFLICT"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    MISSING_REVIEW = "MISSING_REVIEW"
    SELF_APPROVAL = "SELF_APPROVAL"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNSAFE_STOCK = "UNSAFE_STOCK"

    def __init__(self, code, message, field_errors=None, retryable=False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.field_errors = field_errors or {}
        self.retryable = retryable

    def __str__(self):
        return self.message
