"""Domain errors → RFC 9457 problem responses (mapped in the API layer)."""

from __future__ import annotations


class LumenError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(LumenError):
    status_code, code = 404, "not_found"


class Conflict(LumenError):
    status_code, code = 409, "conflict"


class InvalidInput(LumenError):
    status_code, code = 422, "invalid_input"


class Unauthorized(LumenError):
    status_code, code = 401, "unauthorized"


class Forbidden(LumenError):
    status_code, code = 403, "forbidden"


class PayloadTooLarge(LumenError):
    status_code, code = 413, "payload_too_large"


class Overloaded(LumenError):
    """Backpressure: tell the client to retry instead of queueing unboundedly."""

    status_code, code = 429, "overloaded"

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message, details={"retry_after": retry_after})


class PromotionBlocked(LumenError):
    status_code, code = 409, "promotion_blocked"


class UpstreamError(LumenError):
    status_code, code = 502, "upstream_error"
