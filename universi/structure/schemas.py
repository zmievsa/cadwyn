import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from universi.exceptions import UniversiStructureError

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
    discriminator: str
    repr: bool


@dataclass(slots=True)
class OldSchemaFieldHad:
    schema: type[BaseModel]
    field_name: str
    type: type
    field_changes: FieldChanges


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
            raise UniversiStructureError(
                f'Field "{self.field_name}" has "import_as" but not "import_from" which is prohibited',
            )


@dataclass(slots=True)
class AlterFieldInstructionFactory:
    schema: type[BaseModel]
    name: str

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
    ) -> OldSchemaFieldHad:
        return OldSchemaFieldHad(
            schema=self.schema,
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


@dataclass(slots=True)
class SchemaPropertyDidntExistInstruction:
    schema: type[BaseModel]
    name: str


@dataclass
class SchemaPropertyDefinitionInstruction:
    schema: type[BaseModel]
    name: str
    function: Callable

    def __post_init__(self):
        sig = inspect.signature(self.function)
        if len(sig.parameters) != 1:
            raise UniversiStructureError(
                f"Property '{self.name}' must have one argument and it has {len(sig.parameters)}",
            )
        functools.update_wrapper(self, self.function)

    def __call__(self, __parsed_schema: BaseModel):
        return self.function(__parsed_schema)


@dataclass(slots=True)
class AlterPropertyInstructionFactory:
    schema: type[BaseModel]
    name: str
    if TYPE_CHECKING:
        was = staticmethod
    else:

        def was(self, function: Callable) -> SchemaPropertyDefinitionInstruction:
            return SchemaPropertyDefinitionInstruction(self.schema, self.name, function)

    @property
    def didnt_exist(self) -> SchemaPropertyDidntExistInstruction:
        return SchemaPropertyDidntExistInstruction(self.schema, self.name)


AlterSchemaSubInstruction = (
    OldSchemaFieldHad
    | OldSchemaFieldDidntExist
    | OldSchemaFieldExistedWith
    | SchemaPropertyDidntExistInstruction
    | SchemaPropertyDefinitionInstruction
)


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

    def property(self, name: str, /) -> AlterPropertyInstructionFactory:
        return AlterPropertyInstructionFactory(self.schema, name)


def schema(model: type[BaseModel], /) -> AlterSchemaSubInstructionFactory:
    return AlterSchemaSubInstructionFactory(model)
