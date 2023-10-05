from typing import Any

from pydantic import BaseModel
from pydantic.fields import ModelPrivateAttr, Undefined
from pydantic.typing import NoArgAnyCallable


class FillableModelPrivateAttr(ModelPrivateAttr):
    pass


def FillablePrivateAttr(  # noqa: N802
    default: Any = Undefined,
    *,
    default_factory: NoArgAnyCallable | None = None,
) -> Any:
    return FillableModelPrivateAttr(default=default, default_factory=default_factory)


class FillablePrivateAttrMixin(BaseModel):
    def __init__(self, **data: Any) -> None:
        if self.__private_attributes__:
            fillable_private_fields = {}
            for name, value in data.copy().items():
                if name in self.__private_attributes__ and isinstance(
                    self.__private_attributes__[name],
                    FillableModelPrivateAttr,
                ):
                    fillable_private_fields[name] = value
                    data.pop(name)
            super().__init__(**data)
            for name, value in fillable_private_fields.items():
                object.__setattr__(self, name, value)
        else:
            super().__init__(**data)
