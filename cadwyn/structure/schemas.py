from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union, cast

from issubclass import issubclass as lenient_issubclass
from pydantic import AliasChoices, AliasPath, BaseModel, Field
from pydantic._internal._decorators import PydanticDescriptorProxy, unwrap_wrapped_function
from pydantic.fields import FieldInfo

from cadwyn._utils import (
    DATACLASS_SLOTS,
    Sentinel,
    fully_unwrap_decorator,
    get_name_of_function_wrapped_in_pydantic_validator,
)
from cadwyn.exceptions import CadwynStructureError

from .common import _HiddenAttributeMixin

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny


PossibleFieldAttributes = Literal[
    "default",
    "alias",
    "alias_priority",
    "default_factory",
    "validation_alias",
    "serialization_alias",
    "title",
    "field_title_generator",
    "description",
    "examples",
    "exclude",
    "const",
    "deprecated",
    "frozen",
    "validate_default",
    "repr",
    "init",
    "init_var",
    "kw_only",
    "fail_fast",
    "gt",
    "ge",
    "lt",
    "le",
    "strict",
    "coerce_numbers_to_str",
    "multiple_of",
    "allow_inf_nan",
    "max_digits",
    "decimal_places",
    "min_length",
    "max_length",
    "union_mode",
    "allow_mutation",
    "pattern",
    "discriminator",
]


# TODO: Add json_schema_extra as a breaking change in a major version
@dataclass(**DATACLASS_SLOTS)
class FieldChanges:
    default: Any
    alias: Union[str, None]
    default_factory: Any
    alias_priority: Union[int, None]
    validation_alias: Union[str, AliasPath, AliasChoices, None]
    serialization_alias: Union[str, None]
    title: Union[str, None]
    field_title_generator: Union[Callable[[str, FieldInfo], str], None]
    description: str
    examples: Union[list[Any], None]
    exclude: "AbstractSetIntStr | MappingIntStrAny | Any"
    const: bool
    deprecated: bool
    frozen: Union[bool, None]
    validate_default: Union[bool, None]
    repr: bool
    init: Union[bool, None]
    init_var: Union[bool, None]
    kw_only: Union[bool, None]
    fail_fast: bool
    gt: float
    ge: float
    lt: float
    le: float
    strict: bool
    coerce_numbers_to_str: Union[bool, None]
    multiple_of: float
    allow_inf_nan: bool
    max_digits: int
    decimal_places: int
    min_length: int
    max_length: int
    union_mode: Literal["smart", "left_to_right"]
    allow_mutation: bool
    pattern: str
    discriminator: str


@dataclass(**DATACLASS_SLOTS)
class FieldHadInstruction(_HiddenAttributeMixin):
    schema: type[BaseModel]
    name: str
    type: type
    field_changes: FieldChanges
    new_name: str


@dataclass(**DATACLASS_SLOTS)
class FieldDidntHaveInstruction(_HiddenAttributeMixin):
    schema: type[BaseModel]
    name: str
    attributes: tuple[str, ...]


@dataclass(**DATACLASS_SLOTS)
class FieldDidntExistInstruction(_HiddenAttributeMixin):
    schema: type[BaseModel]
    name: str


@dataclass(**DATACLASS_SLOTS)
class FieldExistedAsInstruction(_HiddenAttributeMixin):
    schema: type[BaseModel]
    name: str
    field: FieldInfo


# TODO (https://github.com/zmievsa/cadwyn/issues/112): Add an ability to add extras
@dataclass(**DATACLASS_SLOTS)
class AlterFieldInstructionFactory:
    schema: type[BaseModel]
    name: str

    def had(
        self,
        *,
        name: str = Sentinel,
        type: Any = Sentinel,
        default: Any = Sentinel,
        alias: Union[str, None] = Sentinel,
        default_factory: Callable = Sentinel,
        alias_priority: Union[int, None] = Sentinel,
        validation_alias: Union[str, AliasPath, AliasChoices, None] = Sentinel,
        serialization_alias: Union[str, None] = Sentinel,
        title: Union[str, None] = Sentinel,
        field_title_generator: Union[Callable[[str, FieldInfo], str], None] = Sentinel,
        description: str = Sentinel,
        examples: Union[list[Any], None] = Sentinel,
        exclude: "AbstractSetIntStr | MappingIntStrAny | Any" = Sentinel,
        const: bool = Sentinel,
        deprecated: bool = Sentinel,
        frozen: Union[bool, None] = Sentinel,
        validate_default: Union[bool, None] = Sentinel,
        repr: bool = Sentinel,
        init: bool = Sentinel,
        init_var: bool = Sentinel,
        kw_only: bool = Sentinel,
        fail_fast: bool = Sentinel,
        gt: float = Sentinel,
        ge: float = Sentinel,
        lt: float = Sentinel,
        le: float = Sentinel,
        strict: bool = Sentinel,
        coerce_numbers_to_str: bool = Sentinel,
        multiple_of: float = Sentinel,
        allow_inf_nan: bool = Sentinel,
        max_digits: int = Sentinel,
        decimal_places: int = Sentinel,
        min_length: int = Sentinel,
        max_length: int = Sentinel,
        union_mode: Literal["smart", "left_to_right"] = Sentinel,
        allow_mutation: bool = Sentinel,
        pattern: str = Sentinel,
        discriminator: str = Sentinel,
    ) -> FieldHadInstruction:
        return FieldHadInstruction(
            is_hidden_from_changelog=False,
            schema=self.schema,
            name=self.name,
            type=type,
            new_name=name,
            field_changes=FieldChanges(
                default=default,
                default_factory=default_factory,
                alias_priority=alias_priority,
                alias=alias,
                validation_alias=validation_alias,
                serialization_alias=serialization_alias,
                title=title,
                field_title_generator=field_title_generator,
                description=description,
                examples=examples,
                exclude=exclude,
                const=const,
                deprecated=deprecated,
                frozen=frozen,
                validate_default=validate_default,
                repr=repr,
                init=init,
                init_var=init_var,
                kw_only=kw_only,
                fail_fast=fail_fast,
                gt=gt,
                ge=ge,
                lt=lt,
                le=le,
                strict=strict,
                coerce_numbers_to_str=coerce_numbers_to_str,
                multiple_of=multiple_of,
                allow_inf_nan=allow_inf_nan,
                max_digits=max_digits,
                decimal_places=decimal_places,
                min_length=min_length,
                max_length=max_length,
                union_mode=union_mode,
                allow_mutation=allow_mutation,
                pattern=pattern,
                discriminator=discriminator,
            ),
        )

    def didnt_have(self, *attributes: PossibleFieldAttributes) -> FieldDidntHaveInstruction:
        for attribute in attributes:
            if attribute not in FieldChanges.__dataclass_fields__:
                raise CadwynStructureError(
                    f"Unknown attribute {attribute!r}. Are you sure it's a valid field attribute?"
                )
        return FieldDidntHaveInstruction(
            is_hidden_from_changelog=False, schema=self.schema, name=self.name, attributes=attributes
        )

    @property
    def didnt_exist(self) -> FieldDidntExistInstruction:
        return FieldDidntExistInstruction(is_hidden_from_changelog=False, schema=self.schema, name=self.name)

    def existed_as(
        self,
        *,
        type: Any,
        info: Union[FieldInfo, Any, None] = None,
    ) -> FieldExistedAsInstruction:
        if info is None:
            info = cast(FieldInfo, Field())
        info.annotation = type
        return FieldExistedAsInstruction(is_hidden_from_changelog=False, schema=self.schema, name=self.name, field=info)


def _get_model_decorators(model: type[BaseModel]):
    return [
        *model.__pydantic_decorators__.validators.values(),
        *model.__pydantic_decorators__.field_validators.values(),
        *model.__pydantic_decorators__.root_validators.values(),
        *model.__pydantic_decorators__.field_serializers.values(),
        *model.__pydantic_decorators__.model_serializers.values(),
        *model.__pydantic_decorators__.model_validators.values(),
        *model.__pydantic_decorators__.computed_fields.values(),
    ]


@dataclass(**DATACLASS_SLOTS)
class ValidatorExistedInstruction:
    schema: type[BaseModel]
    validator: Union[Callable[..., Any], PydanticDescriptorProxy]


@dataclass(**DATACLASS_SLOTS)
class ValidatorDidntExistInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(**DATACLASS_SLOTS)
class AlterValidatorInstructionFactory:
    schema: type[BaseModel]
    func: Union[Callable[..., Any], PydanticDescriptorProxy]

    @property
    def existed(self) -> ValidatorExistedInstruction:
        return ValidatorExistedInstruction(self.schema, self.func)

    @property
    def didnt_exist(self) -> ValidatorDidntExistInstruction:
        return ValidatorDidntExistInstruction(
            self.schema, get_name_of_function_wrapped_in_pydantic_validator(self.func)
        )


AlterSchemaSubInstruction = Union[
    FieldHadInstruction,
    FieldDidntHaveInstruction,
    FieldDidntExistInstruction,
    FieldExistedAsInstruction,
    ValidatorExistedInstruction,
    ValidatorDidntExistInstruction,
]


@dataclass(**DATACLASS_SLOTS)
class SchemaHadInstruction(_HiddenAttributeMixin):
    schema: type[BaseModel]
    name: str


@dataclass(**DATACLASS_SLOTS)
class AlterSchemaInstructionFactory:
    schema: type[BaseModel]

    def field(self, name: str, /) -> AlterFieldInstructionFactory:
        return AlterFieldInstructionFactory(self.schema, name)

    def validator(
        self, func: "Union[Callable[..., Any], classmethod[Any, Any, Any], PydanticDescriptorProxy]", /
    ) -> AlterValidatorInstructionFactory:
        func = cast(Union[Callable[..., Any], PydanticDescriptorProxy], unwrap_wrapped_function(func))

        if not isinstance(func, PydanticDescriptorProxy):
            if hasattr(func, "__self__"):
                owner = func.__self__
                if lenient_issubclass(owner, BaseModel) and any(  # pragma: no branch
                    fully_unwrap_decorator(decorator.func, decorator.shim) == func
                    for decorator in _get_model_decorators(owner)
                ):
                    return AlterValidatorInstructionFactory(self.schema, func)
            raise CadwynStructureError("The passed function must be a pydantic validator")
        return AlterValidatorInstructionFactory(self.schema, func)

    def had(self, *, name: str) -> SchemaHadInstruction:
        return SchemaHadInstruction(is_hidden_from_changelog=False, schema=self.schema, name=name)


def schema(model: type[BaseModel], /) -> AlterSchemaInstructionFactory:
    return AlterSchemaInstructionFactory(model)
