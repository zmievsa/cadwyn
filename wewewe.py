from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class MyModel(BaseModel):
    foo: str = Field(
        pattern="sads",
        deprecated=True,
        min_length=13,
        max_length=50,
        title="asasds*",
        strict=True,
        kw_only=True,
        frozen=True,
    )


@app.post("/suka", response_model=MyModel)
def pidor(mod: MyModel):
    return mod
