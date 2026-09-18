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
        # Headers the refusal itself carries — today only `Retry-After` on a
        # capacity refusal. main.py passes them through; the envelope is unchanged.
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

    BackpressureError bounds *submitting* work; this bounds *watching* it.
    Retry (see Retry-After), maybe on another replica; keep submitting jobs.
    """

    status_code = 503
    error_code = "stream_capacity"
