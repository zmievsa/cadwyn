from pydantic import BaseModel, field_validator


class MySchema(BaseModel):
    foo: str

    @field_validator("foo")
    @classmethod
    def validate_foo(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("foo must contain a colon")
        return value
