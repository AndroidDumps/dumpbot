from typing import Dict, List, Optional

from pydantic import AnyHttpUrl, BaseModel


class JenkinsBuild(BaseModel):
    number: int
    result: Optional[str]
    actions: List[Dict]


class DumpArguments(BaseModel):
    url: AnyHttpUrl
    use_alt_dumper: bool
