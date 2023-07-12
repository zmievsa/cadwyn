from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from universi.fields import FieldInfo

from .._utils import Sentinel

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny


@dataclass
class FieldChanges:
    default: Any
    default_factory: Any
    alias: str
    title: str
    description: str
    exclude: "AbstractSetIntStr | MappingIntStrAny | Any"
    include: "AbstractSetIntStr | MappingIntStrAny | Any"
    const: bool
    gt: float
    ge: float
    lt: float
    le: float
    multiple_of: float
    allow_inf_nan: bool
    max_digits: int
    decimal_places: int
    min_items: int
    max_items: int
    unique_items: bool
    min_length: int
    max_length: int
    allow_mutation: bool
    regex: str
    discriminator: str
    repr: bool


@dataclass
class OldSchemaFieldWas:
    field_name: str
    type: type
    field_changes: FieldChanges


@dataclass
class OldSchemaDidntHaveField:
    field_name: str


@dataclass
class OldSchemaHadField:
    field_name: str
    type: type
    field: FieldInfo


@dataclass(slots=True)
class AlterSchemaSubInstructionFactory:
    name: str
    # TODO: Add a validation  to check that field actually changed

    def had(
        self,
        *,
        type: Any = Sentinel,
        default: Any = Sentinel,
        default_factory: Callable = Sentinel,
        alias: str = Sentinel,
        title: str = Sentinel,
        description: str = Sentinel,
        exclude: "AbstractSetIntStr | MappingIntStrAny | Any" = Sentinel,
        include: "AbstractSetIntStr | MappingIntStrAny | Any" = Sentinel,
        const: bool = Sentinel,
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
    ) -> OldSchemaFieldWas:
        return OldSchemaFieldWas(
            field_name=self.name,
            type=type,
            field_changes=FieldChanges(
                default=default,
                default_factory=default_factory,
                alias=alias,
                title=title,
                description=description,
                exclude=exclude,
                include=include,
                const=const,
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
            ),
        )

    @property
    def didnt_exist(self) -> OldSchemaDidntHaveField:
        return OldSchemaDidntHaveField(field_name=self.name)

    def existed_with(self, *, type: type, info: FieldInfo) -> OldSchemaHadField:
        return OldSchemaHadField(field_name=self.name, type=type, field=info)


def field(name: str, /) -> AlterSchemaSubInstructionFactory:
    return AlterSchemaSubInstructionFactory(name=name)


AlterSchemaSubInstruction = OldSchemaFieldWas | OldSchemaDidntHaveField | OldSchemaHadField


@dataclass
class AlterSchemaInstruction:
    schema: type[BaseModel]
    changes: Sequence[AlterSchemaSubInstruction]


def schema(
    model: type[BaseModel],
    /,
    *changes: AlterSchemaSubInstruction,
) -> AlterSchemaInstruction:
    return AlterSchemaInstruction(schema=model, changes=changes)
