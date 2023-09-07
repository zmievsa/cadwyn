from pydantic import BaseModel


class UnversionedSchema1(BaseModel):
    bar: int
