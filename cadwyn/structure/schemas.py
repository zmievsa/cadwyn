from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from cadwyn.exceptions import CadwynStructureError

from .._compat import PYDANTIC_V2
from .._utils import Sentinel

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny


@dataclass(slots=True)
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
    pattern: str
    discriminator: str
    repr: bool


@dataclass(slots=True)
class OldSchemaFieldHad:
    schema: type[BaseModel]
    field_name: str
    type: type
    field_changes: FieldChanges
    new_name: str


@dataclass(slots=True)
class OldSchemaFieldDidntExist:
    schema: type[BaseModel]
    field_name: str


@dataclass(slots=True)
class OldSchemaFieldExistedWith:
    schema: type[BaseModel]
    field_name: str
    type: type
    field: FieldInfo
    import_from: str | None = None
    import_as: str | None = None

    def __post_init__(self):
        if self.import_from is None and self.import_as is not None:
            raise CadwynStructureError(
                f'Field "{self.field_name}" has "import_as" but not "import_from" which is prohibited',
            )


@dataclass(slots=True)
class AlterFieldInstructionFactory:
    schema: type[BaseModel]
    name: str

    def had(
        self,
        *,
        name: str = Sentinel,
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
        pattern: str = Sentinel,
        discriminator: str = Sentinel,
        repr: bool = Sentinel,
    ) -> OldSchemaFieldHad:
        if PYDANTIC_V2:
            if regex is not Sentinel:
                raise CadwynStructureError("`regex` was removed in Pydantic 2. Use `pattern` instead")
            if include is not Sentinel:
                raise CadwynStructureError("`include` was removed in Pydantic 2. Use `exclude` instead")
            if min_items is not Sentinel:
                raise CadwynStructureError("`min_items` was removed in Pydantic 2. Use `min_length` instead")
            if max_items is not Sentinel:
                raise CadwynStructureError("`max_items` was removed in Pydantic 2. Use `max_length` instead")
            if unique_items is not Sentinel:
                raise CadwynStructureError(
                    "`unique_items` was removed in Pydantic 2. Use `Set` type annotation instead"
                    "(this feature is discussed in https://github.com/pydantic/pydantic-core/issues/296)",
                )
        else:
            if pattern is not Sentinel:
                raise CadwynStructureError("`pattern` is only available in Pydantic 2. use `regex` instead")
        return OldSchemaFieldHad(
            schema=self.schema,
            field_name=self.name,
            type=type,
            new_name=name,
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
                pattern=pattern,
                discriminator=discriminator,
                repr=repr,
            ),
        )

    @property
    def didnt_exist(self) -> OldSchemaFieldDidntExist:
        return OldSchemaFieldDidntExist(self.schema, field_name=self.name)

    def existed_as(
        self,
        *,
        type: Any,
        info: FieldInfo | None = None,
        import_from: str | None = None,
        import_as: str | None = None,
    ) -> OldSchemaFieldExistedWith:
        return OldSchemaFieldExistedWith(
            self.schema,
            field_name=self.name,
            type=type,
            field=info or Field(),
            import_from=import_from,
            import_as=import_as,
        )


AlterSchemaSubInstruction = OldSchemaFieldHad | OldSchemaFieldDidntExist | OldSchemaFieldExistedWith


@dataclass(slots=True)
class AlterSchemaInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(slots=True)
class AlterSchemaSubInstructionFactory:
    schema: type[BaseModel]

    def field(self, name: str, /) -> AlterFieldInstructionFactory:
        return AlterFieldInstructionFactory(self.schema, name)

    def had(self, *, name: str) -> AlterSchemaInstruction:
        return AlterSchemaInstruction(self.schema, name)


def schema(model: type[BaseModel], /) -> AlterSchemaSubInstructionFactory:
    return AlterSchemaSubInstructionFactory(model)
