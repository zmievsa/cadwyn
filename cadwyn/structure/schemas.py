import ast
import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from cadwyn._compat import PYDANTIC_V2
from cadwyn._utils import Sentinel
from cadwyn.codegen._asts import _ValidatorWrapper, get_validator_info_or_none
from cadwyn.exceptions import CadwynStructureError

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny


PossibleFieldAttributes = Literal[
    "default",
    "default_factory",
    "alias",
    "title",
    "description",
    "exclude",
    "include",
    "const",
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "allow_inf_nan",
    "max_digits",
    "decimal_places",
    "min_items",
    "max_items",
    "unique_items",
    "min_length",
    "max_length",
    "allow_mutation",
    "regex",
    "pattern",
    "discriminator",
    "repr",
]


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
class FieldHadInstruction:
    schema: type[BaseModel]
    name: str
    type: type
    field_changes: FieldChanges
    new_name: str


@dataclass(slots=True)
class FieldDidntHaveInstruction:
    schema: type[BaseModel]
    name: str
    attributes: tuple[str, ...]


@dataclass(slots=True)
class FieldDidntExistInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(slots=True)
class FieldExistedAsInstruction:
    schema: type[BaseModel]
    name: str
    type: type
    field: FieldInfo


# TODO (https://github.com/zmievsa/cadwyn/issues/112): Add an ability to add extras
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
    ) -> FieldHadInstruction:
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
        return FieldHadInstruction(
            schema=self.schema,
            name=self.name,
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

    def didnt_have(self, *attributes: PossibleFieldAttributes) -> FieldDidntHaveInstruction:
        for attribute in attributes:
            if attribute not in FieldChanges.__dataclass_fields__:
                raise CadwynStructureError(
                    f"Unknown attribute {attribute!r}. Are you sure it's a valid field attribute?"
                )
        return FieldDidntHaveInstruction(self.schema, self.name, attributes)

    @property
    def didnt_exist(self) -> FieldDidntExistInstruction:
        return FieldDidntExistInstruction(self.schema, name=self.name)

    def existed_as(
        self,
        *,
        type: Any,
        info: FieldInfo | None = None,
    ) -> FieldExistedAsInstruction:
        return FieldExistedAsInstruction(
            self.schema,
            name=self.name,
            type=type,
            field=info or Field(),
        )


@dataclass(slots=True)
class ValidatorExistedInstruction:
    schema: type[BaseModel]
    validator: Callable[..., Any]
    validator_info: _ValidatorWrapper = field(init=False)

    def __post_init__(self):
        source = textwrap.dedent(inspect.getsource(self.validator))
        validator_ast = ast.parse(source).body[0]
        if not isinstance(validator_ast, ast.FunctionDef):
            raise CadwynStructureError("The passed validator must be a function")

        validator_info = get_validator_info_or_none(validator_ast)
        if validator_info is None:
            raise CadwynStructureError("The passed function must be a pydantic validator")
        self.validator_info = validator_info


@dataclass(slots=True)
class ValidatorDidntExistInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(slots=True)
class AlterValidatorInstructionFactory:
    schema: type[BaseModel]
    func: Callable[..., Any]

    @property
    def existed(self) -> ValidatorExistedInstruction:
        return ValidatorExistedInstruction(self.schema, self.func)

    @property
    def didnt_exist(self) -> ValidatorDidntExistInstruction:
        return ValidatorDidntExistInstruction(self.schema, self.func.__name__)


AlterSchemaSubInstruction = (
    FieldHadInstruction
    | FieldDidntHaveInstruction
    | FieldDidntExistInstruction
    | FieldExistedAsInstruction
    | ValidatorExistedInstruction
    | ValidatorDidntExistInstruction
)


@dataclass(slots=True)
class SchemaHadInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(slots=True)
class AlterSchemaInstructionFactory:
    schema: type[BaseModel]

    def field(self, name: str, /) -> AlterFieldInstructionFactory:
        return AlterFieldInstructionFactory(self.schema, name)

    def validator(self, func: "Callable[..., Any] | classmethod[Any, Any, Any]", /) -> AlterValidatorInstructionFactory:
        if isinstance(func, classmethod):
            func = func.__wrapped__
        return AlterValidatorInstructionFactory(self.schema, func)

    def had(self, *, name: str) -> SchemaHadInstruction:
        return SchemaHadInstruction(self.schema, name)


def schema(model: type[BaseModel], /) -> AlterSchemaInstructionFactory:
    return AlterSchemaInstructionFactory(model)
