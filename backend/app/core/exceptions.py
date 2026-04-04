from typing import Any


class AppError(Exception):
    """Base class for all application errors. Carries an HTTP status and a stable error_code."""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


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
