from typing import Any


class AppError(Exception):
    """Base class for all application errors. Carries an HTTP status and a stable error_code."""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        # Response headers the refusal itself carries — `Retry-After` on a
        # capacity refusal is the one that matters today. The AppError handler
        # in main.py passes these straight through to the JSONResponse; the
        # error envelope (error_code / message / details / request_id) is
        # unchanged, so a client that ignores headers sees exactly what it
        # always saw.
        self.headers = headers


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "authentication_failed"


class AuthorizationError(AppError):
    status_code = 403
    error_code = "forbidden"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"


class RequestValidationError(AppError):
    status_code = 422
    error_code = "validation_error"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limit_exceeded"


class JobError(AppError):
    status_code = 400
    error_code = "job_error"


class StorageError(AppError):
    status_code = 500
    error_code = "storage_error"


class BackpressureError(AppError):
    status_code = 503
    error_code = "backpressure"


class StreamCapacityError(AppError):
    """This process is already running its maximum number of SSE streams.

    Distinct from BackpressureError on purpose: backpressure is about the
    worker being behind and applies to *submitting* work, this is about one
    API process's concurrent-stream budget and applies to *watching* it. A
    client that meets this should retry (the Retry-After header says when),
    possibly against another replica — it should not stop submitting jobs.
    """

    status_code = 503
    error_code = "stream_capacity"
