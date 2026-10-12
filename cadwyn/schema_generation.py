import copy
import dataclasses
import functools
import inspect
import sys
import types
import typing
from collections.abc import Callable, Sequence
from datetime import date
from enum import Enum, auto
from functools import cache
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    Generic,
    TypeAlias,
    TypeGuard,
    Union,
    cast,
    get_args,
    get_origin,
)

import fastapi.params
import fastapi.security.base
import fastapi.utils
import pydantic
import pydantic._internal._decorators
from fastapi import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, RootModel
from pydantic._internal import _decorators
from pydantic._internal._decorators import (
    FieldSerializerDecoratorInfo,
    FieldValidatorDecoratorInfo,
    ModelSerializerDecoratorInfo,
    ModelValidatorDecoratorInfo,
    RootValidatorDecoratorInfo,
    ValidatorDecoratorInfo,
)
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic._internal._typing_extra import try_eval_type as pydantic_try_eval_type
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import (
    Any,
    Doc,
    TypeVar,
    _AnnotatedAlias,
    assert_never,
    final,
    overload,
    override,
)

from cadwyn._asts import GenericAliasUnionArgs
from cadwyn._utils import (
    DATACLASS_KW_ONLY,
    DATACLASS_SLOTS,
    Sentinel,
    UnionType,
    fully_unwrap_decorator,
    get_name_of_function_wrapped_in_pydantic_validator,
    is_sqlalchemy_model,
    lenient_issubclass,
)
from cadwyn.exceptions import CadwynError, InvalidGenerationInstructionError
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import ResponseInfo
from cadwyn.structure.enums import AlterEnumSubInstruction, EnumDidntHaveMembersInstruction, EnumHadMembersInstruction
from cadwyn.structure.schemas import (
    AlterSchemaSubInstruction,
    FieldDidntExistInstruction,
    FieldDidntHaveInstruction,
    FieldExistedAsInstruction,
    FieldHadInstruction,
    SchemaHadInstruction,
    ValidatorDidntExistInstruction,
    ValidatorExistedInstruction,
    _get_model_decorators,
)
from cadwyn.structure.versions import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME, VersionBundle

from ._utils import iscoroutinefunction

if TYPE_CHECKING:
    from cadwyn.structure.versions import HeadVersion, Version


_Call = TypeVar("_Call", bound=Callable[..., Any])

_FieldName: TypeAlias = str
_T_ANY_MODEL = TypeVar("_T_ANY_MODEL", bound=Union[BaseModel, Enum])
_T_ENUM = TypeVar("_T_ENUM", bound=Enum)

_T_PYDANTIC_MODEL = TypeVar("_T_PYDANTIC_MODEL", bound=BaseModel)
PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP: dict[type[_decorators.DecoratorInfo], Callable[..., Any]] = {
    ValidatorDecoratorInfo: pydantic.validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    FieldValidatorDecoratorInfo: pydantic.field_validator,
    FieldSerializerDecoratorInfo: pydantic.field_serializer,
    RootValidatorDecoratorInfo: pydantic.root_validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    ModelValidatorDecoratorInfo: pydantic.model_validator,
    ModelSerializerDecoratorInfo: pydantic.model_serializer,
    ComputedFieldInfo: pydantic.computed_field,
}
_PYDANTIC_ALL_EXPORTED_NAMES = set(pydantic.__all__)
_DEFAULT_PYDANTIC_CLASSES = (BaseModel, RootModel)

try:
    from pydantic_settings import BaseSettings

    _DEFAULT_PYDANTIC_CLASSES = (*_DEFAULT_PYDANTIC_CLASSES, BaseSettings)
except ImportError:  # pragma: no cover
    pass


VALIDATOR_CONFIG_KEY = "__validators__"
_all_field_arg_names = sorted(
    [
        name
        for name, param in inspect.signature(Field).parameters.items()
        if param.kind in {inspect._ParameterKind.KEYWORD_ONLY, inspect._ParameterKind.POSITIONAL_OR_KEYWORD}
    ],
)
EXTRA_FIELD_NAME = "json_schema_extra"


_empty_field_info = Field()
dict_of_empty_field_info = {k: getattr(_empty_field_info, k) for k in FieldInfo.__slots__}


@dataclasses.dataclass(**DATACLASS_SLOTS)
class PydanticFieldWrapper:
    """We DO NOT maintain field.metadata at all"""

    init_model_field: dataclasses.InitVar[FieldInfo]

    annotation: Any
    name_from_newer_version: str

    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)

    def __post_init__(self, init_model_field: FieldInfo) -> None:
        self.passed_field_attributes = _extract_passed_field_attributes(init_model_field)

    def update_attribute(self, *, name: str, value: Any) -> None:
        self.passed_field_attributes[name] = value

    def delete_attribute(self, *, name: str) -> None:
        self.passed_field_attributes.pop(name)

    def generate_field_copy(self, generator: "SchemaGenerator") -> pydantic.fields.FieldInfo:
        return pydantic.Field(
            **generator.annotation_transformer.change_version_of_annotation(self.passed_field_attributes)
        )


def _extract_passed_field_attributes(field_info: FieldInfo) -> dict[str, object]:
    return {
        k: v
        for k, v in (field_info._attributes_set | collect_known_metadata(field_info.metadata)[0]).items()
        if k in _all_field_arg_names and not (k == "frozen" and v is None)
    }


@dataclasses.dataclass(**DATACLASS_SLOTS)
class _ModelBundle:
    enums: dict[type[Enum], "_EnumWrapper[Enum]"]
    schemas: dict[type[BaseModel], "_PydanticModelWrapper[BaseModel]"]


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _RuntimeSchemaGenContext:
    version_bundle: "VersionBundle"
    current_version: "Union[Version, HeadVersion]"
    models: _ModelBundle
    latest_version: "Version" = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.latest_version = max(self.version_bundle.versions, key=lambda v: v.value)


def migrate_response_body(
    versions: "VersionBundle",
    latest_response_model: type[pydantic.BaseModel],
    *,
    latest_body: Any,
    version: Union[VersionType, date],
) -> Any:
    """Convert the data to a specific version

    Apply all version changes from latest until the passed version in reverse order
    and wrap the result in the correct version of latest_response_model
    """
    if isinstance(version, date):
        version = version.isoformat()
        version = versions._get_closest_lesser_version(version)
    if version not in versions._version_values_set:
        raise CadwynError(f"Version {version} not found in version bundle")
    response = ResponseInfo(Response(status_code=200), body=latest_body)
    migrated_response = versions._migrate_response(
        response,
        current_version=version,
        head_response_model=latest_response_model,
        head_route=None,
    )

    versioned_response_model: type[pydantic.BaseModel] = generate_versioned_models(versions)[str(version)][
        latest_response_model
    ]
    return versioned_response_model.model_validate(migrated_response.body)


def _unwrap_model(model: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
    while hasattr(model, "__cadwyn_original_model__"):
        model = cast("type[_T_ANY_MODEL]", model.__cadwyn_original_model__)
    return model


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _ValidatorWrapper:
    kwargs: dict[str, Any]
    func: Callable[..., object]
    decorator: Callable[..., Callable[..., object]]
    is_deleted: bool = False
    is_classmethod: bool = False


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _PerFieldValidatorWrapper(_ValidatorWrapper):
    fields: list[str] = dataclasses.field(default_factory=list)


def _wrap_validator(
    func: "_decorators.DecoratedType[object]",
    is_pydantic_v1_style_validator: Any,
    decorator_info: _decorators.DecoratorInfo,
):
    # This is only for pydantic v1 style validators
    func = fully_unwrap_decorator(func, is_pydantic_v1_style_validator)
    is_classmethod = False
    if inspect.ismethod(func):
        is_classmethod = True
        func = func.__func__
    kwargs = dataclasses.asdict(decorator_info)
    decorator_fields = kwargs.pop("fields", None)

    # wrapped_property is not accepted by computed_field()
    if isinstance(decorator_info, ComputedFieldInfo):
        kwargs.pop("wrapped_property", None)

    actual_decorator = PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP[type(decorator_info)]
    if is_pydantic_v1_style_validator:
        # There's an inconsistency in their interfaces so we gotta resort to this
        mode = kwargs.pop("mode", "after")
        kwargs["pre"] = mode != "after"
        if (
            isinstance(decorator_info, RootValidatorDecoratorInfo) and decorator_info.mode == "after"
        ):  # pragma: no cover # TODO
            kwargs["skip_on_failure"] = True
    if decorator_fields is not None:
        return _PerFieldValidatorWrapper(
            func=func,
            fields=list(decorator_fields),
            decorator=actual_decorator,
            kwargs=kwargs,
            is_classmethod=is_classmethod,
        )
    else:
        return _ValidatorWrapper(func=func, decorator=actual_decorator, kwargs=kwargs, is_classmethod=is_classmethod)


def _is_dunder(attr_name: str):
    return attr_name.startswith("__") and attr_name.endswith("__")


def _wrap_pydantic_model(model: type[_T_PYDANTIC_MODEL]) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
    # In case we have a forwardref within one of the fields
    # For example, when "from __future__ import annotations" is used in the file with the schema
    if model is not BaseModel:
        model.model_rebuild(raise_errors=False)
    decorators = _get_model_decorators(model)
    validators = {}
    for decorator_wrapper in decorators:
        if decorator_wrapper.cls_var_name not in model.__dict__:
            continue

        wrapped_validator = _wrap_validator(decorator_wrapper.func, decorator_wrapper.shim, decorator_wrapper.info)
        validators[decorator_wrapper.cls_var_name] = wrapped_validator

    def _rebuild_annotated(name: str):
        if field_info := model.model_fields.get(name):
            if not field_info.metadata:
                return field_info.annotation

            return _AnnotatedAlias(field_info.annotation, tuple(field_info.metadata))
        return model.__annotations__[name]  # pragma: no cover

    annotations = {
        name: value if not isinstance(value, str) else _rebuild_annotated(name)
        for name, value in model.__annotations__.items()
    }

    defined_fields = model.__annotations__
    fields = {
        field_name: PydanticFieldWrapper(
            model.model_fields[field_name],
            annotations[field_name],
            field_name,
        )
        for field_name in model.__annotations__
        if field_name in defined_fields and field_name in model.model_fields
    }

    main_attributes = fields | validators
    other_attributes = {
        attr_name: attr_val
        for attr_name, attr_val in model.__dict__.items()
        if attr_name not in main_attributes
        and not (_is_dunder(attr_name) or attr_name in {"_abc_impl", "model_fields", "model_computed_fields"})
    }

    other_attributes |= {
        "model_config": model.model_config,
        "__module__": model.__module__,
        "__qualname__": model.__qualname__,
    }
    return _PydanticModelWrapper(
        model,
        name=model.__name__,
        doc=model.__doc__,
        fields=fields,
        other_attributes=other_attributes,
        validators=validators,
        annotations=annotations,
    )


@final
@dataclasses.dataclass(**DATACLASS_SLOTS)
class _PydanticModelWrapper(Generic[_T_PYDANTIC_MODEL]):
    cls: type[_T_PYDANTIC_MODEL] = dataclasses.field(repr=False)
    name: str
    doc: Union[str, None] = dataclasses.field(repr=False)
    fields: Annotated[
        dict["_FieldName", PydanticFieldWrapper],
        Doc(
            "Fields that belong to this model, not to its parents. "
            "I.e. The ones that were either defined or overridden "
        ),
    ] = dataclasses.field(repr=False)
    validators: dict[str, Union[_PerFieldValidatorWrapper, _ValidatorWrapper]] = dataclasses.field(repr=False)
    other_attributes: dict[str, Any] = dataclasses.field(repr=False)
    annotations: dict[str, Any] = dataclasses.field(repr=False)
    _parents: Union[list["_PydanticModelWrapper[BaseModel]"], None] = dataclasses.field(
        init=False, default=None, repr=False
    )

    def __post_init__(self) -> None:
        # This isn't actually supposed to run, it's just a precaution
        while hasattr(self.cls, "__cadwyn_original_model__"):  # pragma: no cover
            self.cls = cast("type[_T_PYDANTIC_MODEL]", self.cls.__cadwyn_original_model__)

        for k, annotation in self.annotations.items():
            if get_origin(annotation) == Annotated:
                sub_annotations = get_args(annotation)
                # Annotated cannot be copied and is cached based on "==" and "hash", while annotated_types.Interval are
                # frozen and so are consistently hashed
                self.annotations[k] = _AnnotatedAlias(
                    copy.deepcopy(sub_annotations[0]), tuple(copy.deepcopy(sub_ann) for sub_ann in sub_annotations[1:])
                )

    def __deepcopy__(self, memo: dict[int, Any]) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
        result = _PydanticModelWrapper(
            self.cls,
            name=self.name,
            doc=self.doc,
            fields=copy.deepcopy(self.fields),
            validators=copy.deepcopy(self.validators),
            other_attributes=copy.deepcopy(self.other_attributes),
            annotations=copy.deepcopy(self.annotations),
        )
        memo[id(self)] = result
        return result

    @override
    def __hash__(self) -> int:
        return hash(id(self))

    def _get_parents(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> "list[_PydanticModelWrapper[BaseModel]]":
        if self._parents is not None:
            return self._parents
        parents: list[_PydanticModelWrapper[BaseModel]] = []
        for base in self.cls.mro()[1:]:
            if not issubclass(base, BaseModel):
                continue
            if base in schemas:
                parents.append(schemas[base])
            else:
                parents.append(_wrap_pydantic_model(base))
        self._parents = parents
        return parents

    def _get_defined_fields_through_mro(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> dict[str, PydanticFieldWrapper]:
        fields = {}

        for parent in reversed(self._get_parents(schemas)):
            fields |= parent.fields

        return fields | self.fields

    def _get_defined_annotations_through_mro(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> dict[str, Any]:
        annotations = {}

        for parent in reversed(self._get_parents(schemas)):
            annotations |= parent.annotations

        return annotations | self.annotations

    def generate_model_copy(self, generator: "SchemaGenerator") -> type[_T_PYDANTIC_MODEL]:
        per_field_validators = {}
        root_validators = {}
        for name, validator in self.validators.items():
            if validator.is_deleted:
                continue
            func = classmethod(validator.func) if validator.is_classmethod else validator.func
            if isinstance(validator, _PerFieldValidatorWrapper):
                per_field_validators[name] = validator.decorator(*validator.fields, **validator.kwargs)(func)
            else:
                root_validators[name] = validator.decorator(**validator.kwargs)(func)
        fields = {name: field.generate_field_copy(generator) for name, field in self.fields.items()}

        model_copy = type(self.cls)(
            self.name,
            tuple(generator[cast("type[BaseModel]", base)] for base in self.cls.__bases__ if base is not Generic),
            self.other_attributes
            | per_field_validators
            | root_validators
            | fields
            | {
                "__annotations__": generator.annotation_transformer.change_version_of_annotation(
                    self.annotations,
                ),
                "__doc__": self.doc,
                "__qualname__": self.cls.__qualname__.removesuffix(self.cls.__name__) + self.name,
            },
            __pydantic_generic_metadata__=self.cls.__pydantic_generic_metadata__,
        )

        model_copy.__cadwyn_original_model__ = self.cls  # ty: ignore[unresolved-attribute]
        return cast("type[_T_PYDANTIC_MODEL]", model_copy)


def is_regular_function(call: object) -> TypeGuard[Union[types.FunctionType, types.MethodType]]:
    return isinstance(call, types.FunctionType | types.MethodType)


def _function_globals(call: Union[types.FunctionType, types.MethodType]) -> dict[str, Any]:
    return call.__globals__  # ty: ignore[unresolved-attribute]  # MethodType forwards function attributes at runtime.


def _unwrap_partial(call: Any) -> Any:
    while isinstance(call, functools.partial):
        call = call.func
    return call


def _get_function_behind_callable(call: Any) -> Any:
    """Return the function that runs when the callable is called. It decides how FastAPI must call the callable.

    Partials only pre-fill arguments so we unwrap them. We do not follow decorators (__wrapped__) because the outer
    decorator is what runs. Classes are returned as is because calling them constructs an instance.
    """
    call = _unwrap_partial(call)
    if is_regular_function(call) or inspect.isclass(call):
        return call
    return call.__call__


def _get_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its postponed (string) annotations evaluated, like FastAPI does.

    inspect.signature() itself evaluates them in the globals of whichever function it reads the signature from,
    which is the only reliable way to pick the right globals for every kind of wrapper (partials, decorators, etc).
    It evaluates them only once so the forward references within them (e.g. `body: "Model"` or `list["Model"]`
    with postponed annotations) are left.
    """
    try:
        return inspect.signature(call, eval_str=True)
    # This is deliberately narrow: an annotation may use a name that only exists for type checkers
    # (e.g. one imported under "if TYPE_CHECKING:"). FastAPI handles this the same way. The parameter annotations
    # that can be resolved individually are resolved later in _modify_callable_annotations
    except NameError:
        return _get_unevaluated_signature(call)


def _get_unevaluated_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its annotations as written, e.g. strings for postponed ones"""
    if sys.version_info >= (3, 14):
        from annotationlib import Format

        # Unresolvable names in lazily evaluated annotations become ForwardRefs instead of raising
        return inspect.signature(call, annotation_format=Format.FORWARDREF)
    return inspect.signature(call)


def _get_best_effort_globals(call: Any) -> dict[str, Any]:
    """Return the globals of the callable for the string annotations that _get_signature() left.

    Like FastAPI, we take them from the unwrapped callable. Unlike FastAPI, we also unwrap partials that do not
    carry globals themselves. For callable class instances, __globals__ is on the __call__ method instead
    """
    unwrapped_call = inspect.unwrap(call)
    while True:
        unwrapped_call_globals = getattr(unwrapped_call, "__globals__", None)
        if isinstance(unwrapped_call_globals, dict):
            return unwrapped_call_globals
        if not isinstance(unwrapped_call, functools.partial):
            break
        unwrapped_call = inspect.unwrap(unwrapped_call.func)
    dunder_call = inspect.getattr_static(unwrapped_call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


def _copy_field_info(field_info: FieldInfo) -> FieldInfo:
    # The same as pydantic's FieldInfo._copy() (which does not exist in older versions of pydantic):
    # the mutable attributes must not be shared with the original either
    field_info_copy = copy.copy(field_info)
    for attr_name in ("metadata", "_attributes_set", "_qualifiers"):
        setattr(field_info_copy, attr_name, copy.copy(getattr(field_info, attr_name)))
    return field_info_copy


class _CallableKind(Enum):
    ENDPOINT = auto()
    DEPENDENCY = auto()
    # Callables that FastAPI never calls itself, e.g. pydantic's default factories or callables in Annotated metadata
    OTHER = auto()


class _CallableWrapper:
    __annotations__: dict[str, Any]
    __defaults__: Union[tuple[Any, ...], None]
    __signature__: inspect.Signature
    # Whether it wraps an endpoint or a dependency rather than a callable that FastAPI never calls itself
    # (e.g. pydantic's default factories). The latter are wrapped exactly the way Cadwyn always did
    _is_called_by_fastapi = False

    # __eq__ and __hash__ are needed to make sure that dependency overrides work correctly.
    # They are based on putting dependencies (functions) as keys for the dictionary so if we want to be able to
    # override the wrapper, we need to make sure that it is equivalent to the original in __hash__ and __eq__

    def __init__(self, original_callable: Callable[..., Any], *, is_called_by_fastapi: bool) -> None:
        super().__init__()
        self._original_callable = original_callable
        if is_called_by_fastapi:
            self._is_called_by_fastapi = True
            # FastAPI follows __wrapped__ to find out how to call a dependency so it must lead to the same place
            # as it would for the original. We only copy the attributes of functions because copying the ones of
            # classes and their instances would make us look like them to FastAPI
            wrapped = _unwrap_partial(original_callable)
            functools.update_wrapper(
                self, wrapped, updated=functools.WRAPPER_UPDATES if is_regular_function(wrapped) else ()
            )
        else:
            if not is_regular_function(original_callable):
                original_callable = original_callable.__call__

            functools.update_wrapper(self, original_callable)

    @property
    def __globals__(self) -> dict[str, Any]:  # pragma: no cover
        # FastAPI uses __globals__ to resolve forward references in type hints
        # It's supposed to be an attribute on the function but we use it as property to prevent python
        # from trying to pickle globals when we deepcopy this wrapper
        if is_regular_function(self._original_callable):
            return _function_globals(self._original_callable)
        dunder_call = self._original_callable.__call__
        if is_regular_function(dunder_call):
            return _function_globals(dunder_call)
        return {}

    def __deepcopy__(self, memo: dict[int, Any]) -> "_CallableWrapper":
        if not self._is_called_by_fastapi:
            # The same as the default deepcopy, which is what Cadwyn always did for the other callables
            wrapper_copy = object.__new__(type(self))
            memo[id(self)] = wrapper_copy
            wrapper_copy.__dict__.update(copy.deepcopy(self.__dict__, memo))
            return wrapper_copy
        # Copying would also copy the original dependency (e.g. a class instance or a partial) so it would stop
        # matching the keys of app.dependency_overrides and changes to its state would not be visible in the copy.
        # Wrappers are never mutated after creation and every version creates its own wrappers anyway.
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._original_callable(*args, **kwargs)

    @override
    def __hash__(self) -> int:
        return hash(self._original_callable)

    @override
    def __eq__(self, value: object) -> bool:
        return self._original_callable == value


class _AsyncCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._original_callable(*args, **kwargs)


class _GeneratorCallableWrapper(_CallableWrapper):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> typing.Iterator[Any]:
        # "yield from" forwards send(), throw(), and close() so the wrapped generator handles exceptions itself
        yield from self._original_callable(*args, **kwargs)


class _AsyncGeneratorCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> typing.AsyncGenerator[Any, Any]:
        # There is no "yield from" for async generators so we forward asend() and athrow() by hand.
        # Otherwise, exceptions (e.g. the ones raised in the endpoint) would never reach the wrapped generator
        # so its "except" and "finally" blocks would get skipped until it gets garbage collected.
        # aclose() is forwarded too because it throws GeneratorExit into us.
        # The exception is forwarded outside of the "except" block. Otherwise, it would still be the one being handled
        # while the wrapped generator runs which would change sys.exc_info() and the __context__ of its exceptions.
        generator = self._original_callable(*args, **kwargs)
        try:
            value = await generator.__anext__()
            while True:
                sent_value = thrown_exception = None
                try:
                    sent_value = yield value
                except BaseException as e:  # noqa: BLE001
                    thrown_exception = e
                if thrown_exception is None:
                    value = await generator.asend(sent_value)
                else:
                    value = await generator.athrow(thrown_exception)
        except StopAsyncIteration:
            return


@final
class _AnnotationTransformer:
    def __init__(self, generator: "SchemaGenerator") -> None:
        self.generator = generator
        self.change_versions_of_a_non_container_annotation = self._change_version_of_a_non_container_annotation
        # This cache cannot be used until something is done regarding https://github.com/fastapi/fastapi/pull/14320
        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/FastAPI do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        # self.change_versions_of_a_non_container_annotation = functools.cache(
        #     self._change_version_of_a_non_container_annotation,
        # )

    def change_version_of_annotation(self, annotation: Any) -> Any:
        """Recursively go through all annotations and change them to annotations corresponding to the version passed.

        So if we had a annotation "UserResponse" from "head" version, and we passed version of "2022-11-16", it would
        replace "UserResponse" with the the same class but from the "2022-11-16" version.

        """
        if isinstance(annotation, dict):
            return {
                self.change_version_of_annotation(key): self.change_version_of_annotation(value)
                for key, value in annotation.items()
            }

        elif isinstance(annotation, list | tuple):
            return type(annotation)(self.change_version_of_annotation(v) for v in annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation)

    def migrate_router_to_version(self, router: fastapi.routing.APIRouter):
        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(route)

    def migrate_route_to_version(self, route: fastapi.routing.APIRoute, *, ignore_response_model: bool = False):
        if route.response_model is not None and not ignore_response_model:
            route.response_model = self.change_version_of_annotation(route.response_model)
            route.response_field = fastapi.utils.create_model_field(
                name="Response_" + route.unique_id,
                type_=route.response_model,
                mode="serialization",
            )
        route.dependencies = self.change_version_of_annotation(route.dependencies)
        route.endpoint = self.change_versions_of_a_non_container_annotation(
            route.endpoint, callable_kind=_CallableKind.ENDPOINT
        )
        for callback in route.callbacks or []:
            if not isinstance(callback, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(callback, ignore_response_model=ignore_response_model)
        self._remake_endpoint_dependencies(route)

    def _change_version_of_a_non_container_annotation(
        self, annotation: Any, *, callable_kind: _CallableKind = _CallableKind.OTHER
    ) -> Any:
        """callable_kind tells how FastAPI uses the annotation if it is a callable"""
        from typing_inspection.typing_objects import is_any, is_newtype, is_typealiastype

        if isinstance(annotation, GenericAliasUnionArgs):
            origin = get_origin(annotation)
            args = get_args(annotation)
            # Classvar does not support generic tuple arguments
            if origin is ClassVar:
                return ClassVar[self.change_version_of_annotation(args[0])]
            if origin is Annotated:
                # Annotated[...] is cached based on "==" and "hash" while our dependency wrappers are equal to the
                # originals so it could return the alias that holds the dependency of another version
                return _AnnotatedAlias(
                    self.change_version_of_annotation(args[0]),
                    tuple(self.change_version_of_annotation(arg) for arg in args[1:]),
                )
            return origin[tuple(self.change_version_of_annotation(arg) for arg in get_args(annotation))]
        elif is_typealiastype(annotation):
            if (
                annotation.__module__ is not None and (annotation.__module__.startswith("pydantic."))
            ) or annotation.__name__ in _PYDANTIC_ALL_EXPORTED_NAMES:
                return annotation
            else:
                return type(annotation)(
                    name=annotation.__name__,
                    value=self.change_version_of_annotation(annotation.__value__),
                    type_params=self.change_version_of_annotation(annotation.__type_params__),
                )
        elif isinstance(annotation, fastapi.params.Depends):
            # This also covers Security. Copying keeps all of their other fields (e.g. use_cache, scope, and scopes)
            # without calling __init__ which subclasses could have overridden with a different signature
            annotation_copy = copy.copy(annotation)
            dependency = self.change_versions_of_a_non_container_annotation(
                annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
            )
            object.__setattr__(annotation_copy, "dependency", dependency)
            return annotation_copy
        elif isinstance(annotation, UnionType):  # pragma: no cover
            return typing.Union[tuple(self.change_version_of_annotation(a) for a in get_args(annotation))]
        elif is_any(annotation) or is_newtype(annotation):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation)
        elif callable(annotation):
            if type(annotation).__module__.startswith(
                ("fastapi.", "pydantic.", "pydantic_core.", "starlette.")
            ) or isinstance(annotation, fastapi.security.base.SecurityBase):
                return annotation

            def modifier(annotation: Any):
                return self.change_version_of_annotation(annotation)

            return self._modify_callable_annotations(
                annotation,
                modifier,
                modifier,
                annotation_modifying_wrapper_factory=functools.partial(
                    self._copy_function_through_class_based_wrapper,
                    is_called_by_fastapi=callable_kind is not _CallableKind.OTHER,
                ),
                callable_kind=callable_kind,
            )
        else:
            return annotation

    def _change_version_of_type(self, annotation: type) -> type:
        if lenient_issubclass(annotation, (BaseModel, Enum)):
            return self.generator[annotation]
        else:
            return annotation

    @classmethod
    def _remake_endpoint_dependencies(cls, route: fastapi.routing.APIRoute) -> None:
        # Unlike get_dependant, APIRoute is the public API of FastAPI and it's (almost) guaranteed to be stable.

        route_copy = fastapi.routing.APIRoute(
            route.path, route.endpoint, dependencies=route.dependencies, response_model=route.response_model
        )
        route.dependant = route_copy.dependant
        route.body_field = route_copy.body_field
        _add_request_and_response_params(route)

    @classmethod
    def _modify_callable_annotations(  # pragma: no branch # because of lambdas
        cls,
        call: _Call,
        modify_annotations: Callable[[dict[str, Any]], dict[str, Any]] = lambda a: a,
        modify_defaults: Callable[[tuple[Any, ...]], tuple[Any, ...]] = lambda a: a,
        *,
        annotation_modifying_wrapper_factory: Callable[[_Call], _CallableWrapper],
        callable_kind: _CallableKind,
    ) -> _Call:
        annotation_modifying_wrapper = annotation_modifying_wrapper_factory(call)
        if callable_kind is _CallableKind.OTHER:
            # FastAPI never evaluates the annotations of such callables (e.g. pydantic's default factories) and they
            # may not even be evaluable at runtime (e.g. pandas.Series[int]) so this is exactly what Cadwyn always did
            old_params = inspect.signature(call).parameters
            if sys.version_info >= (3, 14):  # pragma: no cover
                from annotationlib import get_annotations

                callable_annotations = get_annotations(annotation_modifying_wrapper)
            else:
                callable_annotations = annotation_modifying_wrapper.__annotations__
            original_call = cls._unwrap_callable(call)
            # For callable class instances, __globals__ is on the __call__ method, not on the instance itself
            if is_regular_function(original_call):
                call_globals = _function_globals(original_call)
            else:
                dunder_call = inspect.getattr_static(original_call, "__call__", None)
                call_globals = _function_globals(dunder_call) if is_regular_function(dunder_call) else {}
            callable_annotations = {
                k: v if type(v) is not str else _try_eval_type(v, call_globals) for k, v in callable_annotations.items()
            }
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            annotation_modifying_wrapper.__defaults__ = modify_defaults(
                tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
            )
        else:
            # FastAPI evaluates the annotations of endpoints and dependencies the same way so we cannot fail where
            # FastAPI would not. Unlike __annotations__, the signature also follows functools.partial and __signature__
            old_signature = _get_signature(call)
            old_params = old_signature.parameters
            callable_annotations = {
                name: param.annotation
                for name, param in old_params.items()
                if param.annotation is not inspect.Parameter.empty
            }
            return_annotation = old_signature.return_annotation
            if callable_kind is _CallableKind.ENDPOINT and return_annotation is not inspect.Signature.empty:
                # FastAPI infers the response model of endpoints from their return annotations
                callable_annotations["return"] = return_annotation
            # Cadwyn always evaluated the annotations that were written as strings (e.g. postponed ones) recursively
            # before versioning them. inspect.signature() evaluates them only once so forward references can be left
            # within them (e.g. `body: "Model"` or `list["Model"]` with postponed annotations) or they can stay strings
            # (e.g. if they could not be evaluated or if they come from __signature__). FastAPI would evaluate some of
            # them itself but in the globals that lead to the head models so we finish evaluating them here
            unevaluated_signature = _get_unevaluated_signature(call)
            annotations_written_as_strings = {
                name for name, param in unevaluated_signature.parameters.items() if type(param.annotation) is str
            }
            if type(unevaluated_signature.return_annotation) is str:
                annotations_written_as_strings.add("return")
            call_globals = _get_best_effort_globals(cls._unwrap_callable(call))
            callable_annotations = {
                k: _try_eval_type(v, call_globals) if k in annotations_written_as_strings else v
                for k, v in callable_annotations.items()
            }
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            if callable_kind is _CallableKind.DEPENDENCY and return_annotation is not inspect.Signature.empty:
                # FastAPI never looks at the return annotations of dependencies so we neither evaluate nor version them.
                # Versioning them would generate their models for nothing, which can even fail
                # (e.g. for default factories without a signature such as time.time before Python 3.13)
                annotation_modifying_wrapper.__annotations__["return"] = return_annotation
            annotation_modifying_wrapper.__defaults__ = modify_defaults(
                tuple(
                    # FastAPI mutates the FieldInfo objects in defaults (e.g. their annotation) so every version
                    # (and app) must have its own copies. Otherwise, all of them would use the models of whichever
                    # got built last
                    _copy_field_info(p.default) if isinstance(p.default, FieldInfo) else p.default
                    for p in old_params.values()
                    if p.default is not inspect.Signature.empty
                ),
            )
        annotation_modifying_wrapper.__signature__ = cls._generate_signature(
            annotation_modifying_wrapper,
            old_params,
        )

        return cast("_Call", annotation_modifying_wrapper)

    @staticmethod
    def _generate_signature(
        new_callable: _CallableWrapper,
        old_params: types.MappingProxyType[str, inspect.Parameter],
    ) -> inspect.Signature:
        parameters = []
        default_counter = 0
        for param in old_params.values():
            if param.default is not inspect.Signature.empty:
                assert new_callable.__defaults__ is not None, (  # noqa: S101
                    "Defaults cannot be None here. If it is, you have found a bug in Cadwyn. "
                    "Please, report it in our issue tracker."
                )
                default = new_callable.__defaults__[default_counter]
                default_counter += 1
            else:
                default = inspect.Signature.empty
            parameters.append(
                inspect.Parameter(
                    param.name,
                    param.kind,
                    default=default,
                    annotation=new_callable.__annotations__.get(
                        param.name,
                        inspect.Signature.empty,
                    ),
                ),
            )
        return inspect.Signature(
            parameters=parameters,
            return_annotation=new_callable.__annotations__.get(
                "return",
                inspect.Signature.empty,
            ),
        )

    @classmethod
    def _copy_function_through_class_based_wrapper(cls, call: _Call, *, is_called_by_fastapi: bool) -> _CallableWrapper:
        """Separate from copy_endpoint because endpoints MUST be functions in FastAPI, they cannot be cls instances"""
        call = cls._unwrap_callable(call)
        if is_called_by_fastapi:
            actual_call = _get_function_behind_callable(call)
            if inspect.isclass(actual_call):
                # Like FastAPI, we call classes synchronously because calling them constructs an instance
                return _CallableWrapper(call, is_called_by_fastapi=True)
        elif not is_regular_function(call):
            # This means that the callable is actually an instance of a regular class
            actual_call = call.__call__
        else:
            actual_call = call
        if is_async_gen_callable(actual_call):
            return _AsyncGeneratorCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        elif is_coroutine_callable(actual_call):
            return _AsyncCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        elif is_gen_callable(actual_call):
            return _GeneratorCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        else:
            return _CallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)

    @staticmethod
    def _unwrap_callable(call: Any) -> Any:
        while hasattr(call, "_original_callable"):
            call = call._original_callable

        return call


def is_gen_callable(call: Callable[..., object]) -> bool:
    # Copied from fastapi.dependencies.models
    if inspect.isgeneratorfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isgeneratorfunction(dunder_call)


def is_async_gen_callable(call: Callable[..., object]) -> bool:
    # Copied from fastapi.dependencies.models
    if inspect.isasyncgenfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isasyncgenfunction(dunder_call)


def is_coroutine_callable(call: Callable[..., object]) -> bool:  # pragma: no cover
    # Copied from fastapi.dependencies.models
    # ty reports the deprecation only on Python 3.11/3.12. Repeating unused-ignore-comment
    # handles the other targets: https://github.com/astral-sh/ty/issues/3393.
    if inspect.isroutine(call):
        return iscoroutinefunction(call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.
    if inspect.isclass(call):
        return False
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return iscoroutinefunction(dunder_call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.


def _add_request_and_response_params(route: APIRoute):
    if not route.dependant.request_param_name:
        route.dependant.request_param_name = _CADWYN_REQUEST_PARAM_NAME
    if not route.dependant.response_param_name:
        route.dependant.response_param_name = _CADWYN_RESPONSE_PARAM_NAME


@final
class SchemaGenerator:
    __slots__ = "annotation_transformer", "concrete_models", "model_bundle"

    def __init__(self, model_bundle: _ModelBundle) -> None:
        self.annotation_transformer = _AnnotationTransformer(self)
        self.model_bundle = model_bundle
        self.concrete_models = {}
        self.concrete_models = {
            k: wrapper.generate_model_copy(self)
            for k, wrapper in (self.model_bundle.schemas | self.model_bundle.enums).items()
        }

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if (
            not isinstance(model, type)
            or not lenient_issubclass(model, (BaseModel, Enum))
            or model in _DEFAULT_PYDANTIC_CLASSES
            or is_sqlalchemy_model(model)
        ):
            return model
        model = _unwrap_model(model)

        if model in self.concrete_models:
            model_copy = self.concrete_models[model]
        else:
            wrapper = self._get_wrapper_for_model(model)
            model_copy = wrapper.generate_model_copy(self)
            self.concrete_models[model] = model_copy
        return cast("type[_T_ANY_MODEL]", model_copy)

    @overload
    def _get_wrapper_for_model(self, model: type[BaseModel]) -> "_PydanticModelWrapper[BaseModel]": ...
    @overload
    def _get_wrapper_for_model(self, model: type[Enum]) -> "_EnumWrapper[Enum]": ...

    def _get_wrapper_for_model(
        self, model: type[Union[BaseModel, Enum]]
    ) -> "Union[_PydanticModelWrapper[BaseModel], _EnumWrapper[Enum]]":
        model = _unwrap_model(model)

        if issubclass(model, BaseModel):
            if model in self.model_bundle.schemas:
                return self.model_bundle.schemas[model]
            # TODO: My god, what if one of its fields is in our concrete schemas and we don't use it? :O
            # TODO: Add an argument with our concrete schemas for _wrap_pydantic_model
            wrapper = _wrap_pydantic_model(model)
            self.model_bundle.schemas[model] = wrapper
        elif issubclass(model, Enum):
            if model in self.model_bundle.enums:
                return self.model_bundle.enums[model]
            wrapper = _EnumWrapper(model)
            self.model_bundle.enums[model] = wrapper
        else:
            assert_never(model)
        return wrapper


@cache
def generate_versioned_models(versions: "VersionBundle") -> "dict[str, SchemaGenerator]":
    models = _create_model_bundle(versions)

    version_to_context_map = {}
    context = _RuntimeSchemaGenContext(current_version=versions.head_version, models=models, version_bundle=versions)
    _migrate_classes(context)

    for version in versions.versions:
        context = _RuntimeSchemaGenContext(current_version=version, models=models, version_bundle=versions)
        version_to_context_map[str(version.value)] = SchemaGenerator(copy.deepcopy(models))
        # note that the last migration will not contain any version changes so we don't need to save the results
        _migrate_classes(context)

    return version_to_context_map


def _create_model_bundle(versions: "VersionBundle"):
    return _ModelBundle(
        enums={enum: _EnumWrapper(enum) for enum in versions.versioned_enums.values()},
        schemas={schema: _wrap_pydantic_model(schema) for schema in versions.versioned_schemas.values()},
    )


def _migrate_classes(context: _RuntimeSchemaGenContext) -> None:
    for version_change in context.current_version.changes:
        _apply_alter_schema_instructions(
            context.models.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
        )
        _apply_alter_enum_instructions(
            context.models.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )


def _apply_alter_schema_instructions(
    modified_schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
    alter_schema_instructions: Sequence[Union[AlterSchemaSubInstruction, SchemaHadInstruction]],
    version_change_name: str,
) -> None:
    for alter_schema_instruction in alter_schema_instructions:
        schema_info = modified_schemas[alter_schema_instruction.schema]
        if isinstance(alter_schema_instruction, FieldExistedAsInstruction):
            _add_field_to_model(schema_info, modified_schemas, alter_schema_instruction, version_change_name)
        elif isinstance(alter_schema_instruction, FieldHadInstruction | FieldDidntHaveInstruction):
            _change_field_in_model(
                schema_info,
                modified_schemas,
                alter_schema_instruction,
                version_change_name,
            )
        elif isinstance(alter_schema_instruction, FieldDidntExistInstruction):
            _delete_field_from_model(schema_info, alter_schema_instruction.name, version_change_name)
        elif isinstance(alter_schema_instruction, ValidatorExistedInstruction):
            validator_name = get_name_of_function_wrapped_in_pydantic_validator(alter_schema_instruction.validator)
            raw_validator = cast(
                "pydantic._internal._decorators.PydanticDescriptorProxy[object]",
                alter_schema_instruction.validator,
            )
            schema_info.validators[validator_name] = _wrap_validator(
                raw_validator.wrapped,
                is_pydantic_v1_style_validator=raw_validator.shim,
                decorator_info=raw_validator.decorator_info,
            )
        elif isinstance(alter_schema_instruction, ValidatorDidntExistInstruction):
            if alter_schema_instruction.name not in schema_info.validators:
                raise InvalidGenerationInstructionError(
                    f'You tried to delete a validator "{alter_schema_instruction.name}" from "{schema_info.name}" '
                    f'in "{version_change_name}" but it doesn\'t have such a validator.',
                )
            if schema_info.validators[alter_schema_instruction.name].is_deleted:
                raise InvalidGenerationInstructionError(
                    f'You tried to delete a validator "{alter_schema_instruction.name}" from "{schema_info.name}" '
                    f'in "{version_change_name}" but it is already deleted.',
                )
            schema_info.validators[alter_schema_instruction.name].is_deleted = True
        elif isinstance(alter_schema_instruction, SchemaHadInstruction):
            _change_model(schema_info, alter_schema_instruction, version_change_name)
        else:
            assert_never(alter_schema_instruction)


def _apply_alter_enum_instructions(
    enums: "dict[type[Enum], _EnumWrapper[Enum]]",
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
    version_change_name: str,
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = enums[alter_enum_instruction.enum]
        if isinstance(alter_enum_instruction, EnumDidntHaveMembersInstruction):
            for member in alter_enum_instruction.members:
                if member not in enum.members:
                    raise InvalidGenerationInstructionError(
                        f'You tried to delete a member "{member}" from "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but it doesn\'t have such a member.',
                    )
                enum.members.pop(member)
        elif isinstance(alter_enum_instruction, EnumHadMembersInstruction):
            for member, member_value in alter_enum_instruction.members.items():
                if member in enum.members and enum.members[member] == member_value:
                    raise InvalidGenerationInstructionError(
                        f'You tried to add a member "{member}" to "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but there is already a member with that name and value.',
                    )
                enum.members[member] = member_value
        else:
            assert_never(alter_enum_instruction)


def _change_model(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: SchemaHadInstruction,
    version_change_name: str,
):
    if alter_schema_instruction.name == model.name:
        raise InvalidGenerationInstructionError(
            f'You tried to change the name of "{model.name}" in "{version_change_name}" '
            "but it already has the name you tried to assign.",
        )

    model.name = alter_schema_instruction.name


def _add_field_to_model(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    alter_schema_instruction: FieldExistedAsInstruction,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    # Special handling for ClassVar fields
    if get_origin(alter_schema_instruction.field.annotation) is ClassVar:
        # ClassVar fields should not be in model.fields, only in annotations and other_attributes
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.field.annotation
        # Set the actual ClassVar value in other_attributes
        if alter_schema_instruction.field.default is not PydanticUndefined:
            model.other_attributes[alter_schema_instruction.name] = alter_schema_instruction.field.default
    else:
        # Regular field handling
        field = PydanticFieldWrapper(
            alter_schema_instruction.field, alter_schema_instruction.field.annotation, alter_schema_instruction.name
        )
        model.fields[alter_schema_instruction.name] = field
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.field.annotation


def _change_field_in_model(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    alter_schema_instruction: Union[FieldHadInstruction, FieldDidntHaveInstruction],
    version_change_name: str,
):
    defined_annotations = model._get_defined_annotations_through_mro(schemas)
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name not in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to change the field "{alter_schema_instruction.name}" from '
            f'"{model.name}" in "{version_change_name}" but it doesn\'t have such a field.',
        )

    field = defined_fields[alter_schema_instruction.name]
    model.fields[alter_schema_instruction.name] = field
    model.annotations[alter_schema_instruction.name] = defined_annotations[alter_schema_instruction.name]

    if isinstance(alter_schema_instruction, FieldHadInstruction):
        # TODO: This naming sucks
        _change_field(
            model,
            alter_schema_instruction,
            version_change_name,
            defined_annotations,
            field,
            model.annotations[alter_schema_instruction.name],
        )
    else:
        _delete_field_attributes(
            model,
            alter_schema_instruction,
            version_change_name,
            field,
            model.annotations[alter_schema_instruction.name],
        )


def _change_field(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation: Union[Any, None],
):
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type:
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.type

    if alter_schema_instruction.new_name is not Sentinel:
        if alter_schema_instruction.new_name == alter_schema_instruction.name:
            raise InvalidGenerationInstructionError(
                f'You tried to change the name of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already has that name.",
            )
        model.fields[alter_schema_instruction.new_name] = model.fields.pop(alter_schema_instruction.name)
        model.annotations[alter_schema_instruction.new_name] = model.annotations.pop(
            alter_schema_instruction.name,
            defined_annotations[alter_schema_instruction.name],
        )

    for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
        attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
        if attr_value is not Sentinel:
            if field.passed_field_attributes.get(attr_name, Sentinel) == attr_value:
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            field.update_attribute(name=attr_name, value=attr_value)


def _delete_field_attributes(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    annotation: Any,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        deleted = False

        if attr_name in field.passed_field_attributes:
            field.delete_attribute(name=attr_name)
            deleted = True
        if get_origin(annotation) == Annotated and any(  # pragma: no branch
            hasattr(sub_ann, attr_name) for sub_ann in get_args(annotation)
        ):
            for sub_ann in get_args(annotation):
                if hasattr(sub_ann, attr_name):
                    object.__setattr__(sub_ann, attr_name, None)
                    deleted = True
        if not deleted:
            raise InvalidGenerationInstructionError(
                f'You tried to delete the attribute "{attr_name}" of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already doesn't have that attribute.",
            )


def _delete_field_from_model(model: _PydanticModelWrapper[BaseModel], field_name: str, version_change_name: str):
    if field_name in model.fields:
        model.fields.pop(field_name)
        model.annotations.pop(field_name)
        for validator_name, validator in model.validators.copy().items():
            if isinstance(validator, _PerFieldValidatorWrapper) and field_name in validator.fields:
                validator.fields.remove(field_name)
                # TODO: This behavior doesn't feel natural
                if not validator.fields:
                    model.validators[validator_name].is_deleted = True

    elif (
        field_name in model.validators
        and isinstance(model.validators[field_name], _ValidatorWrapper)
        and hasattr(model.validators[field_name], "decorator")
        and model.validators[field_name].decorator == pydantic.computed_field
    ):
        validator = model.validators[field_name]
        model.validators[field_name].is_deleted = True
        model.annotations.pop(field_name, None)
    elif field_name in model.annotations and get_origin(model.annotations[field_name]) is ClassVar:
        # Handle ClassVar fields - they exist in annotations but not in model.fields
        model.annotations.pop(field_name)
        # Also remove the attribute from other_attributes if it exists there
        model.other_attributes.pop(field_name, None)
    else:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )


class _DummyEnum(Enum):
    pass


@final
class _EnumWrapper(Generic[_T_ENUM]):
    __slots__ = "cls", "members", "name"

    def __init__(self, cls: type[_T_ENUM]) -> None:
        self.cls = _unwrap_model(cls)
        self.name = cls.__name__
        self.members = {member.name: member.value for member in cls}

    def __deepcopy__(self, memo: Any) -> "_EnumWrapper[_T_ENUM]":
        result = _EnumWrapper(self.cls)
        result.members = self.members.copy()
        memo[id(self)] = result
        return result

    def generate_model_copy(self, generator: "SchemaGenerator") -> type[_T_ENUM]:
        enum_dict = Enum.__prepare__(self.name, self.cls.__bases__)

        raw_member_map = {k: v.value if isinstance(v, Enum) else v for k, v in self.members.items()}
        initialization_namespace = self._get_initialization_namespace_for_enum(self.cls) | raw_member_map
        for attr_name, attr in initialization_namespace.items():
            enum_dict[attr_name] = attr
        enum_dict["__doc__"] = self.cls.__doc__
        model_copy = cast("type[_T_ENUM]", type(self.name, self.cls.__bases__, enum_dict))
        model_copy.__cadwyn_original_model__ = self.cls  # ty: ignore[unresolved-attribute]
        return model_copy

    @staticmethod
    def _get_initialization_namespace_for_enum(enum_cls: type[Enum]) -> dict[str, Any]:
        mro_without_the_class_itself = enum_cls.mro()[1:]

        mro_dict = {}
        for cls in reversed(mro_without_the_class_itself):
            mro_dict.update(cls.__dict__)

        return {
            k: v
            for k, v in enum_cls.__dict__.items()
            if k not in enum_cls._member_names_
            and k not in _DummyEnum.__dict__
            and (k not in mro_dict or mro_dict[k] is not v)
        }


def _try_eval_type(value: Any, globals: dict[str, Any]) -> Any:
    new_value, success = pydantic_try_eval_type(value, globals)
    if success:
        return new_value
    else:  # pragma: no cover # Can't imagine when this would happen
        return value
