from typing import Dict, List, Optional

from pydantic import AnyHttpUrl, BaseModel


class JenkinsBuild(BaseModel):
    number: int
    result: Optional[str]
    actions: List[Dict]


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


class ActiveJenkinsBuild(BaseModel):
    job_name: str  # "dumpyara" or "privdump"
    build_id: str
    url: AnyHttpUrl
    requester_username: Optional[str] = None
    request_id: Optional[str] = None
