from collections.abc import Callable
from dataclasses import dataclass
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar, Union, cast

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

from .common import _validate_schema_is_versionable

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
    "json_schema_extra",
]

_ValidatorReturnT = TypeVar("_ValidatorReturnT")
_ValidatorReference: TypeAlias = Union[Callable[..., object], PydanticDescriptorProxy[object]]


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
    json_schema_extra: Union[dict[str, Any], Callable[[dict[str, Any]], None], None]


@dataclass(**DATACLASS_SLOTS)
class FieldHadInstruction:
    schema: type[BaseModel]
    name: str
    type: type
    field_changes: FieldChanges
    new_name: str


@dataclass(**DATACLASS_SLOTS)
class FieldDidntHaveInstruction:
    schema: type[BaseModel]
    name: str
    attributes: tuple[str, ...]


@dataclass(**DATACLASS_SLOTS)
class FieldDidntExistInstruction:
    schema: type[BaseModel]
    name: str


@dataclass(**DATACLASS_SLOTS)
class FieldExistedAsInstruction:
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
        default_factory: Callable[[], object] = Sentinel,
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
        json_schema_extra: Union[dict[str, Any], Callable[[dict[str, Any]], None], None] = Sentinel,
    ) -> FieldHadInstruction:
        return FieldHadInstruction(
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
                json_schema_extra=json_schema_extra,
            ),
        )

    def didnt_have(self, *attributes: PossibleFieldAttributes) -> FieldDidntHaveInstruction:
        for attribute in attributes:
            if attribute not in FieldChanges.__dataclass_fields__:
                raise CadwynStructureError(
                    f"Unknown attribute {attribute!r}. Are you sure it's a valid field attribute?"
                )
        return FieldDidntHaveInstruction(schema=self.schema, name=self.name, attributes=attributes)

    @property
    def didnt_exist(self) -> FieldDidntExistInstruction:
        return FieldDidntExistInstruction(schema=self.schema, name=self.name)

    def existed_as(
        self,
        *,
        type: Any,
        info: Union[FieldInfo, Any, None] = None,
    ) -> FieldExistedAsInstruction:
        if info is None:
            info = cast("FieldInfo", Field())
        info.annotation = type
        return FieldExistedAsInstruction(schema=self.schema, name=self.name, field=info)


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
    validator: _ValidatorReference


@dataclass(**DATACLASS_SLOTS)
class ValidatorDidntExistInstruction:
    schema: type[BaseModel]
    name: str


def _get_validator_names(model: type[BaseModel], func: object) -> list[str]:
    decorators = model.__pydantic_decorators__
    return [
        name
        for name, decorator in (decorators.field_validators | decorators.model_validators).items()
        if _validator_functions_match(decorator.func, func)
    ]


def _validator_functions_match(registered: object, reference: object) -> bool:
    if registered is reference:
        return True
    if isinstance(reference, classmethod | staticmethod):
        return _validator_functions_match(registered, reference.__func__)
    if not isinstance(registered, MethodType):
        return False
    if not isinstance(reference, MethodType):
        return registered.__func__ is reference
    return registered.__func__ is reference.__func__ and registered.__self__ is reference.__self__


def _raise_for_ambiguous_validator(model: type[BaseModel], names: list[str]) -> None:
    raise CadwynStructureError(
        f'The passed function is used by multiple validators on "{model.__name__}": '
        f"{', '.join(names)}. Pass the attribute name instead, e.g. "
        f'schema({model.__name__}).validator("{names[0]}").didnt_exist.'
    )


@dataclass(**DATACLASS_SLOTS)
class AlterValidatorInstructionFactory:
    schema: type[BaseModel]
    func: Union[_ValidatorReference, str]

    @property
    def existed(self) -> ValidatorExistedInstruction:
        if isinstance(self.func, str):
            raise CadwynStructureError(
                "Adding validators by attribute name is not supported. Pass a decorated validator function instead."
            )
        return ValidatorExistedInstruction(self.schema, self.func)

    @property
    def didnt_exist(self) -> ValidatorDidntExistInstruction:
        if isinstance(self.func, str):
            return ValidatorDidntExistInstruction(self.schema, self.func)
        name = get_name_of_function_wrapped_in_pydantic_validator(self.func)
        # Preserve name-based instructions that already work, including decorated functions
        # passed separately from the model. Resolve assigned classmethods only when needed.
        if not isinstance(self.func, PydanticDescriptorProxy) and not any(
            decorator.cls_var_name == name for decorator in _get_model_decorators(self.schema)
        ):
            names = _get_validator_names(self.schema, self.func)
            if len(names) > 1:
                _raise_for_ambiguous_validator(self.schema, names)
            if names:
                name = names[0]
        return ValidatorDidntExistInstruction(self.schema, name)


AlterSchemaSubInstruction = Union[
    FieldHadInstruction,
    FieldDidntHaveInstruction,
    FieldDidntExistInstruction,
    FieldExistedAsInstruction,
    ValidatorExistedInstruction,
    ValidatorDidntExistInstruction,
]


@dataclass(**DATACLASS_SLOTS)
class SchemaHadInstruction:
    schema: type[BaseModel]
    name: str = Sentinel
    # The docstring of the model (`__doc__`), which pydantic uses as its description. None means no description
    description: Union[str, None] = Sentinel


@dataclass(**DATACLASS_SLOTS)
class AlterSchemaInstructionFactory:
    schema: type[BaseModel]

    def __post_init__(self) -> None:
        _validate_schema_is_versionable(self.schema)

    def field(self, name: str, /) -> AlterFieldInstructionFactory:
        return AlterFieldInstructionFactory(self.schema, name)

    def validator(
        self,
        func: "Union[str, Callable[..., Any], classmethod[Any, Any, Any], PydanticDescriptorProxy[_ValidatorReturnT]]",
        /,
    ) -> AlterValidatorInstructionFactory:
        # Match stored callables before unwrapping partials or descriptors. Ordinary
        # bound Python methods retain their legacy name-based instructions.
        names = _get_validator_names(self.schema, func)
        if names and not (isinstance(func, MethodType) and isinstance(func.__func__, FunctionType)):
            if len(names) > 1:
                _raise_for_ambiguous_validator(self.schema, names)
            return AlterValidatorInstructionFactory(self.schema, names[0])
        func = unwrap_wrapped_function(func)

        if isinstance(func, PydanticDescriptorProxy):
            return AlterValidatorInstructionFactory(self.schema, func)
        if hasattr(func, "__self__"):
            owner = func.__self__
            if (
                isinstance(owner, type)
                and issubclass(owner, BaseModel)
                and any(  # pragma: no branch
                    fully_unwrap_decorator(decorator.func, decorator.shim) == func
                    for decorator in _get_model_decorators(owner)
                )
            ):
                return AlterValidatorInstructionFactory(self.schema, cast("Callable[..., object]", func))

        decorators = self.schema.__pydantic_decorators__
        validators = decorators.field_validators | decorators.model_validators
        if isinstance(func, str):
            if func not in validators:
                raise CadwynStructureError(f'"{self.schema.__name__}" has no field or model validator named "{func}".')
            return AlterValidatorInstructionFactory(self.schema, func)

        if len(names) == 1:
            return AlterValidatorInstructionFactory(self.schema, names[0])
        if names:
            _raise_for_ambiguous_validator(self.schema, names)
        raise CadwynStructureError("The passed function must be a pydantic validator")

    def had(self, *, name: str = Sentinel, description: Union[str, None] = Sentinel) -> SchemaHadInstruction:
        if name is Sentinel and description is Sentinel:
            raise CadwynStructureError(
                f"You must pass at least one of the arguments of schema({self.schema.__name__}).had()"
            )
        if isinstance(description, str) and not description.strip():
            # Pydantic and Python treat such docstrings differently so `cadwyn render` could not render them
            raise CadwynStructureError(
                f"The description that you passed to schema({self.schema.__name__}).had() is blank. "
                "Pass description=None to remove the description instead."
            )
        return SchemaHadInstruction(schema=self.schema, name=name, description=description)


def schema(model: type[BaseModel], /) -> AlterSchemaInstructionFactory:
    return AlterSchemaInstructionFactory(model)
