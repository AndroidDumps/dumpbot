from pydantic import AnyHttpUrl, BaseModel


class DumpArguments(BaseModel):
    url: AnyHttpUrl
