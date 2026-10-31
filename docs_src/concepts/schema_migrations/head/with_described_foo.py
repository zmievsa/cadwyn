from pydantic import BaseModel, Field


class MySchema(BaseModel):
    foo: str = Field(description="The value of my schema")
