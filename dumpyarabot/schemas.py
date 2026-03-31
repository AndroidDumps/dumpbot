from datetime import datetime
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any

from pydantic import AnyHttpUrl, BaseModel



class DumpArguments(BaseModel):
    url: AnyHttpUrl
    use_alt_dumper: bool
    use_privdump: bool
    initial_message_id: Optional[int] = None
    initial_chat_id: Optional[int] = None


class PendingReview(BaseModel):
    request_id: str
    original_chat_id: int
    original_message_id: int
    requester_id: int
    requester_username: Optional[str]
    url: str
    review_chat_id: int
    review_message_id: int
    submission_confirmation_message_id: Optional[int] = None


class AcceptOptionsState(BaseModel):
    alt: bool = False
    force: bool = False
    privdump: bool = False


class MockupState(BaseModel):
    current_menu: str = (
        "initial"  # "initial", "options", "completed", "rejected", "cancelled"
    )
    request_id: str
    original_command_message_id: int = 0




class JobStatus(str, Enum):
    """Status of a dump job in the worker queue."""
    QUEUED = "queued"         # Job is waiting to be processed
    PROCESSING = "processing" # Job is currently being processed by a worker
    COMPLETED = "completed"   # Job completed successfully
    FAILED = "failed"         # Job failed with errors
    CANCELLED = "cancelled"   # Job was cancelled by user/admin


class JobProgress(BaseModel):
    """Progress information for a dump job."""
    current_step: str
    total_steps: int
    current_step_number: int
    percentage: float
    details: Optional[str] = None
    error_message: Optional[str] = None


class JobMetadata(BaseModel):
    """Metadata for ARQ job tracking and rich status displays."""
    job_type: str = "dump"
    telegram_context: Optional[Dict[str, Any]] = None  # chat_id, message_id, user_id, url
    progress_history: List[Dict[str, Any]] = []  # List of progress updates
    device_info: Optional[Dict[str, Any]] = None  # Populated after property extraction
    repository: Optional[Dict[str, Any]] = None  # Populated on successful completion
    error_context: Optional[Dict[str, Any]] = None  # Populated on failure
    start_time: Optional[str] = None  # ISO format
    end_time: Optional[str] = None  # ISO format
    status: Optional[str] = None  # running, completed, failed


class DumpJob(BaseModel):
    """Schema for dump jobs in the worker queue."""
    job_id: str
    dump_args: DumpArguments
    add_blacklist: bool = False
    status: JobStatus = JobStatus.QUEUED
    worker_id: Optional[str] = None
    progress: Optional[JobProgress] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_data: Optional[Dict[str, Any]] = None  # GitLab URLs, build info, etc.
    error_details: Optional[str] = None
    initial_message_id: Optional[int] = None  # Track the initial message for editing
    initial_chat_id: Optional[int] = None     # Track chat for cross-chat support
    metadata: Optional[JobMetadata] = None   # Rich metadata for ARQ job tracking

    def __init__(self, **data):
        if "created_at" not in data:
            data["created_at"] = datetime.utcnow()
        super().__init__(**data)
