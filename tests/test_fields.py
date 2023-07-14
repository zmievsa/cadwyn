import re

import pytest
from pydantic import BaseModel

from universi import Field


def test__allow_mutation_arg():
    class ModelWithAllowMutationArg(BaseModel):
        foo: str = Field(allow_mutation=False)

        class Config:
            validate_assignment = True

    with pytest.raises(
        TypeError,
        match=re.escape('"foo" has allow_mutation set to False and cannot be assigned'),
    ):
        ModelWithAllowMutationArg(foo="bar").foo = "baz"
