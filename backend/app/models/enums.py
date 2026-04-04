from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    SUPPORT = "support"
    ADMIN = "admin"


class JobType(StrEnum):
    CSV_UPLOAD = "csv_upload"
    REPORT_GEN = "report_gen"
    BULK_API_SYNC = "bulk_api_sync"
    DOC_ANALYSIS = "doc_analysis"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
