from pydantic import AnyHttpUrl, BaseModel


class DumpArguments(BaseModel):
    url: AnyHttpUrl
    use_alt_dumper: bool
