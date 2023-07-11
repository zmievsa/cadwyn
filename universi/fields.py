from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic.fields import FieldInfo as PydanticFieldInfo
from pydantic.fields import Undefined

from universi._utils import Sentinel

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny


class FieldInfo(PydanticFieldInfo):
    __slots__ = ("_universi_field_names",)

    def __init__(
        self,
        default: Any = Undefined,
        repr: bool = Sentinel,
        allow_mutation: bool = Sentinel,
        **kwargs: Any,
    ) -> None:
        self._universi_field_names = {k for k, v in kwargs.items() if v is not Sentinel}
        if repr is Sentinel:
            repr = True
        else:
            self._universi_field_names.add("repr")
        if allow_mutation is Sentinel:
            allow_mutation = True
        else:
            self._universi_field_names.add("allow_mutation")
        if default is not Undefined:
            self._universi_field_names.add("default")

        super().__init__(
            default,
            repr=repr,
            allow_mutation=allow_mutation,
            **{k: None if v is Sentinel else v for k, v in kwargs.items()},
        )


def Field(
    default: Any = Undefined,
    *,
    default_factory: Callable = Sentinel,
    alias: str = Sentinel,
    title: str = Sentinel,
    description: str = Sentinel,
    exclude: "AbstractSetIntStr | MappingIntStrAny | Any" = Sentinel,
    include: "AbstractSetIntStr | MappingIntStrAny | Any" = Sentinel,
    gt: float = Sentinel,
    ge: float = Sentinel,
    lt: float = Sentinel,
    le: float = Sentinel,
    multiple_of: float = Sentinel,
    allow_inf_nan: bool = Sentinel,
    max_digits: int = Sentinel,
    decimal_places: int = Sentinel,
    min_items: int = Sentinel,
    max_items: int = Sentinel,
    unique_items: bool = Sentinel,
    min_length: int = Sentinel,
    max_length: int = Sentinel,
    allow_mutation: bool = Sentinel,
    regex: str = Sentinel,
    discriminator: str = Sentinel,
    repr: bool = Sentinel,
    **extra: Any,
) -> Any:
    field_info = FieldInfo(
        default=default,
        default_factory=default_factory,
        alias=alias,
        title=title,
        description=description,
        exclude=exclude,
        include=include,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        multiple_of=multiple_of,
        allow_inf_nan=allow_inf_nan,
        max_digits=max_digits,
        decimal_places=decimal_places,
        min_items=min_items,
        max_items=max_items,
        unique_items=unique_items,
        min_length=min_length,
        max_length=max_length,
        allow_mutation=allow_mutation,
        regex=regex,
        discriminator=discriminator,
        repr=repr,
        **extra,
    )
    field_info._validate()
    return field_info
