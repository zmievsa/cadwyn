import collections.abc
import contextlib
import copy
import functools
import inspect
import reprlib
import sys
import types
import typing
from collections.abc import Callable, Generator
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    NamedTuple,
    Union,
    cast,
    final,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic._internal._generics import PydanticGenericMetadata
from pydantic.fields import FieldInfo
from pydantic.functional_serializers import PlainSerializer, WrapSerializer
from pydantic.functional_validators import AfterValidator, BeforeValidator, PlainValidator, WrapValidator
from typing_extensions import (
    TypeVar,
    _AnnotatedAlias,
)

from cadwyn._asts import GenericAliasUnionArgs
from cadwyn._utils import (
    UnionType,
    _callable_name,
    _get_type_alias_value,
    lenient_issubclass,
)
from cadwyn.exceptions import SchemaGenerationError
from cadwyn.structure.versions._common import _DELETED_ROUTE_TAG

from ._annotation_evaluation import (
    _UNRESOLVED,
    _are_same_args,
    _contains_forward_refs,
    _contains_only_builtin_types,
    _copy_field_info,
    _eval_forward_refs,
    _evaluate_user_annotations,
    _get_annotations_without_evaluating,
    _get_extra_fields_annotation,
    _get_rebuilt_model_fields,
    _get_typevar_fallbacks,
    _get_value_of_type_alias_with_args,
    _has_undefined_names_in_lazy_annotations,
    _is_type_position_in_generic,
    _rebuild_generic_alias,
    _try_eval_type,
    _try_eval_type_for_discovery,
    _TypeAliasRefersToItselfError,
)
from ._callables import (
    _add_request_and_response_params,
    _AsyncCallableWrapper,
    _AsyncGeneratorCallableWrapper,
    _CallableKind,
    _CallableWrapper,
    _GeneratorCallableWrapper,
    _get_best_effort_globals,
    _get_function_behind_callable,
    _get_globals,
    _get_signature,
    _get_unevaluated_signature,
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
    is_regular_function,
)
from ._common import (
    _PYDANTIC_ALL_EXPORTED_NAMES,
    _T_ANY_MODEL,
    _get_deep_copy_error,
    _is_versionable_model,
    _unwrap_model,
)
from ._core_schema import (
    _get_classes_in_core_schema,
)
from ._generics import (
    _eval_generic_args,
    _get_typevar_uses,
)

if TYPE_CHECKING:
    import fastapi.params
    import fastapi.routing
    import fastapi.security.base

    from ._references import (
        _BareGenericCopier,
        _CycleCopier,
    )
    from ._schema_generator import (
        SchemaGenerator,
    )

_Call = TypeVar("_Call", bound=Callable[..., Any])


# Versioning works without FastAPI, so we only look for FastAPI's classes in annotations once FastAPI has imported
# them. Their objects can't exist before that.
# A module that is still being imported might not have defined the class yet.
def _get_fastapi_depends_classes() -> "tuple[type[fastapi.params.Depends], ...]":
    depends = getattr(sys.modules.get("fastapi.params"), "Depends", None)
    return () if depends is None else (depends,)


def _get_fastapi_security_base_classes() -> "tuple[type[fastapi.security.base.SecurityBase], ...]":
    security_base = getattr(sys.modules.get("fastapi.security.base"), "SecurityBase", None)
    return () if security_base is None else (security_base,)


@final
class _AnnotationTransformer:
    def __init__(
        self,
        generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier, _BareGenericCopier]",
        *,
        record_only: bool = False,
        expands_type_aliases: bool = False,
    ) -> None:
        self.generator = generator
        self.typevar_copies: dict[typing.TypeVar, typing.TypeVar] = {}
        self._typevars_being_copied: set[typing.TypeVar] = set()
        self._typevar_owners: list[Any] = []
        # The models whose own fields use the TypeVars of each namespace (see typevar_namespace)
        self._typevar_users: list[list[Any]] = []
        self._model_owners: list[type[BaseModel]] = []
        self._typevar_scope_model_depth = 0
        self._template_position_depth = 0
        self._typevar_fallback_depth = 0
        # Only request the models from the generator without rebuilding the annotations. This way, we can find
        # the models that an annotation refers to without failing on annotations that we cannot rebuild
        self.record_only = record_only
        # Record the values of type aliases with their args instead of the args themselves (record-only), so that
        # the args that the values do not use are not recorded (see SchemaGenerator.dependency_differs_from_head)
        self._expands_type_aliases = expands_type_aliases
        # The type aliases whose values we are recording right now (by their ids) with the args of each expansion,
        # to stop at the ones that refer to themselves
        self._type_aliases_being_expanded: dict[int, list[tuple[Any, ...]]] = {}
        # The ids of the type aliases, callables and containers that we are recording, to detect the ones that
        # lead back to themselves (e.g. a default factory whose default argument is a registry that contains it)
        self._ids_being_recorded: set[int] = set()
        # The ids of the type aliases that we are copying right now, to detect the ones that refer to themselves
        self._type_aliases_being_copied: set[int] = set()
        # The ids of the lists, tuples, and dicts that we are copying right now with whether they contain themselves
        self._containers_being_copied: dict[int, bool] = {}
        # How many classes and callables we have replaced with this version's copies, so that we can tell whether
        # anything within a container gets replaced. Typing objects, such as `list[int]`, get rebuilt even when
        # nothing within them gets replaced, so we cannot tell it by comparing the results with the originals.
        self._replacement_count = 0
        # How many containers that get deep copied as a whole we are in (see _change_version_of_container)
        self._only_checking_depth = 0
        # The places that we are versioning right now (e.g. fields), innermost last (see describing)
        self._locations: list[str] = []
        # Whether we are recording the routes, whose endpoints and dependencies copying reads like _get_signature
        self._records_routes = False
        # How many positions that need the classes themselves we are in right now (see is_in_hard_position)
        self._hard_position_depth = 0
        # How many metadata objects that pydantic uses we are in (see is_in_pydantic_metadata)
        self._pydantic_metadata_depth = 0
        # The copies of the parameters of the model that a _BareGenericCopier copies, by the parameters
        self.versioned_parameters: dict[Any, Any] = {}
        self.change_versions_of_a_non_container_annotation = self._change_version_of_a_non_container_annotation
        # This cache cannot be used until something is done regarding https://github.com/fastapi/fastapi/pull/14320
        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/FastAPI do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        # self.change_versions_of_a_non_container_annotation = functools.cache(
        #     self._change_version_of_a_non_container_annotation,
        # )

    @property
    def is_in_hard_position(self) -> bool:
        """Whether the annotation that we are versioning right now must use the classes themselves.

        In type positions, such as the annotations of fields and the args of list or Optional within them, pydantic
        also accepts forward refs to models that it resolves later. Everywhere else, e.g. in default factories,
        the metadata of Annotated, or the args of unknown generics, the classes themselves are needed. Copying
        reference cycles relies on this distinction (see _CycleCopier).
        """
        return self._hard_position_depth > 0

    @contextlib.contextmanager
    def hard_position(self) -> Generator[None, None, None]:
        self._hard_position_depth += 1
        try:
            yield
        finally:
            self._hard_position_depth -= 1

    def change_version_of_annotation(self, annotation: Any) -> Any:
        """Recursively go through all annotations and change them to annotations corresponding to the version passed.

        So if we had a annotation "UserResponse" from "head" version, and we passed version of "2022-11-16", it would
        replace "UserResponse" with the same class but from the "2022-11-16" version.

        """
        if isinstance(annotation, dict):
            if self.record_only:
                self._record_without_cycles(annotation, lambda: [*annotation.keys(), *annotation.values()])
                return annotation
            return self._change_version_of_container(annotation)

        elif isinstance(annotation, list | tuple):
            if self.record_only:
                self._record_without_cycles(annotation, lambda: annotation)
                return annotation
            return self._change_version_of_container(annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation)

    def _change_version_of_container(self, container: Union[dict[Any, Any], list[Any], tuple[Any, ...]]) -> Any:
        """Return this version's own copy of the container, with this version's copies of the classes within it.

        Containers include the defaults of fields and params, so every version must get its own copies of them, as
        pydantic gives every instance its own copy of a mutable default. Plain lists, tuples, and dicts get rebuilt
        with their versioned values, as Cadwyn always did. We cannot rebuild other containers (e.g. subclasses, named
        tuples, or containers that contain themselves) without changing them, so they get deep copies, which follow
        their own __deepcopy__ and __reduce__, if nothing within them gets replaced (see _replacement_count).
        Otherwise, we fail instead of using the latest classes or giving them another type.
        """
        if id(container) in self._containers_being_copied:
            # It contains itself, so we deep copy it as a whole once we know that nothing within it gets replaced
            self._containers_being_copied[id(container)] = True
            return container
        self._containers_being_copied[id(container)] = False
        replacement_count = self._replacement_count
        is_plain = type(container) in {dict, list, tuple}
        try:
            # We deep copy other containers as a whole, so that their own __deepcopy__ decides how to copy the values
            # within them (e.g. it can keep the ones that cannot be copied), and we only check these values
            with contextlib.nullcontext() if is_plain else self._only_checking_containers():
                # The (key, value) pairs of dicts
                values: list[Any] = (
                    [
                        (self.change_version_of_annotation(key), self.change_version_of_annotation(value))
                        for key, value in container.items()
                    ]
                    if isinstance(container, dict)
                    else [self.change_version_of_annotation(value) for value in container]
                )
        finally:
            contains_itself = self._containers_being_copied.pop(id(container))
        if is_plain and not contains_itself:
            if isinstance(container, dict):
                return dict(values)
            return values if isinstance(container, list) else tuple(values)
        if self._replacement_count != replacement_count:
            reason = (
                "it contains itself (directly or through other values)"
                if contains_itself
                else f"it is a {type(container).__qualname__}, not a plain list, tuple, or dict"
            )
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copy of {self._describe(container)} because {reason}, and it "
                "contains classes or functions that the version replaces with its own copies (e.g. models or enums "
                "with instructions, the models that use them, or functions). Cadwyn cannot rebuild it with these "
                "copies without changing it. Please use a plain list, tuple, or dict that does not contain itself."
            )
        if self._only_checking_depth:
            return container
        try:
            return copy.deepcopy(container)
        except Exception as error:
            raise _get_deep_copy_error(self._describe(container), error) from error

    def _describe(self, container: Any) -> str:
        return " in ".join([reprlib.repr(container), *self._locations[-1:]])

    @contextlib.contextmanager
    def _only_checking_containers(self) -> Generator[None, None, None]:
        """Check the containers within a container that gets deep copied as a whole instead of copying them"""
        self._only_checking_depth += 1
        try:
            yield
        finally:
            self._only_checking_depth -= 1

    @contextlib.contextmanager
    def describing(self, location: str) -> Generator[None, None, None]:
        """Name the place that we are versioning right now (e.g. a field) in the errors about the values within it"""
        self._locations.append(location)
        try:
            yield
        finally:
            self._locations.pop()

    def migrate_router_to_version(self, router: "fastapi.routing.APIRouter"):
        # Only route generation versions routes, so FastAPI is there
        import fastapi.routing

        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute) or _DELETED_ROUTE_TAG in route.tags:
                continue
            self.migrate_route_to_version(route)

    def migrate_route_to_version(self, route: "fastapi.routing.APIRoute", *, ignore_response_model: bool = False):
        import fastapi.routing
        import fastapi.utils

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

    def record_route(self, route: "fastapi.routing.APIRoute") -> None:
        """Request the models that migrate_route_to_version requests without changing the route (record-only)"""
        records_routes = self._records_routes
        self._records_routes = True
        try:
            self._record_route(route)
        finally:
            self._records_routes = records_routes

    def record_dependency(self, dependency: Callable[..., Any]) -> None:
        """Request the models that versioning the dependency requests without changing it (record-only)"""
        records_routes = self._records_routes
        self._records_routes = True
        try:
            self.change_versions_of_a_non_container_annotation(dependency, callable_kind=_CallableKind.DEPENDENCY)
        finally:
            self._records_routes = records_routes

    def _record_route(self, route: "fastapi.routing.APIRoute") -> None:
        import fastapi.routing

        if route.response_model is not None:
            self.change_version_of_annotation(route.response_model)
        self.change_version_of_annotation(route.dependencies)
        self.change_versions_of_a_non_container_annotation(route.endpoint, callable_kind=_CallableKind.ENDPOINT)
        for callback in route.callbacks or []:
            if isinstance(callback, fastapi.routing.APIRoute):
                self._record_route(callback)

    def _change_version_of_a_non_container_annotation(
        self, annotation: Any, *, callable_kind: _CallableKind = _CallableKind.OTHER
    ) -> Any:
        """callable_kind tells how FastAPI uses the annotation if it is a callable"""
        from typing_inspection.typing_objects import is_any, is_newtype, is_typealiastype

        if self._expands_type_aliases and self._record_expanded_type_alias(annotation):
            return annotation
        if isinstance(annotation, GenericAliasUnionArgs):
            return self._change_version_of_generic_alias(annotation)
        elif (
            isinstance(annotation, typing.TypeVar)
            and self._typevar_owners
            and not self._template_position_depth
            and self._typevar_scope_model_depth == len(self._model_owners)
        ):
            return self._change_version_of_typevar(annotation)
        elif is_typealiastype(annotation):
            if (
                annotation.__module__ is not None and (annotation.__module__.startswith("pydantic."))
            ) or annotation.__name__ in _PYDANTIC_ALL_EXPORTED_NAMES:
                return annotation
            # Pydantic resolves the forward refs in the values of type aliases in their own module
            with self.hard_position():
                if self.record_only:
                    self._record_type_alias(annotation)
                    return annotation
                return self._copy_type_alias(annotation)
        elif isinstance(annotation, _get_fastapi_depends_classes()):
            with self.hard_position():
                if self.record_only:
                    self.change_versions_of_a_non_container_annotation(
                        annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
                    )
                    return annotation
                # This also covers Security. Copying keeps all of their other fields (e.g. use_cache, scope, and
                # scopes) without calling __init__ which subclasses could have overridden with a different signature
                annotation_copy = copy.copy(annotation)
                dependency = self.change_versions_of_a_non_container_annotation(
                    annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
                )
            object.__setattr__(annotation_copy, "dependency", dependency)
            return annotation_copy
        elif isinstance(annotation, UnionType):  # pragma: no cover
            args = tuple(self.change_version_of_annotation(a) for a in get_args(annotation))
            return annotation if self.record_only else typing.Union[args]
        elif is_any(annotation) or is_newtype(annotation):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation)
        elif callable(annotation):
            return self._change_version_of_callable(annotation, callable_kind=callable_kind)
        else:
            return annotation

    @contextlib.contextmanager
    def template_position(self) -> Generator[None, None, None]:
        """Copy type references that do not themselves validate values, without binding their TypeVars."""
        self._template_position_depth += 1
        try:
            yield
        finally:
            self._template_position_depth -= 1

    @contextlib.contextmanager
    def typevar_namespace(self, owner: Any, *, users: Union[list[Any], None] = None) -> Generator[None, None, None]:
        """Version the TypeVars with their quoted fallbacks resolved in the namespace of the owner.

        The users are the models whose own fields use the TypeVars, by default the owner itself. They all resolve
        the fallbacks to the same classes (see SchemaGenerator._get_typevar_owner).
        """
        users = [owner] if users is None else users
        if (
            self._typevar_owners
            and self._typevar_owners[-1] is owner
            and self._typevar_users[-1] == users
            and self._typevar_scope_model_depth == len(self._model_owners)
        ):
            yield
            return
        self._typevar_owners.append(owner)
        self._typevar_users.append(users)
        saved_depth = self._typevar_scope_model_depth
        self._typevar_scope_model_depth = len(self._model_owners)
        saved_copies, saved_active = self.typevar_copies, self._typevars_being_copied
        self.typevar_copies, self._typevars_being_copied = {}, set()
        try:
            yield
        finally:
            self.typevar_copies, self._typevars_being_copied = saved_copies, saved_active
            self._typevar_scope_model_depth = saved_depth
            self._typevar_owners.pop()
            self._typevar_users.pop()

    def _change_version_of_typevar(self, param: typing.TypeVar) -> typing.TypeVar:
        if param in self.typevar_copies:
            return self.typevar_copies[param]
        if param in self._typevars_being_copied:
            if self.record_only:
                self._record_unknown_references()
                return param
            raise SchemaGenerationError(
                f"Cadwyn cannot version type parameter {param.__name__} because its bounds, constraints, or "
                "default refer back to it. Use explicit type arguments instead."
            )
        self._typevars_being_copied.add(param)
        try:
            copied = self._copy_typevar(param)
        finally:
            self._typevars_being_copied.remove(param)
        if not self.record_only:
            self.typevar_copies[param] = copied
        return copied

    def _copy_typevar(self, param: typing.TypeVar) -> typing.TypeVar:
        """Return the TypeVar whose bound, constraints, or default validate this version's models.

        The TypeVar stays the same when they refer back to the only model whose fields use it, as pydantic
        resolves such quoted references to the copy of the model, like in its other fields. When they validate
        the same models as in head in this version, the TypeVar stays the same too, so that the models that use
        it keep their names and behavior.
        """
        kind, values = _get_typevar_fallbacks(param)
        resolved_values = tuple(self._resolve_typevar_value(param, value) for value in values)
        collector = _ReferencedModelCollector()
        collector.annotation_transformer._typevar_owners = self._typevar_owners.copy()
        collector.annotation_transformer._typevar_users = self._typevar_users.copy()
        # Stops at the models and TypeVars that lead back to themselves, as in this transformer
        collector.annotation_transformer._ids_being_recorded = self._ids_being_recorded
        collector.annotation_transformer._typevars_being_copied = self._typevars_being_copied
        collector.annotation_transformer._change_version_of_typevar_values(resolved_values)
        # Pydantic resolves a quoted reference to a model to its copy only within that copy itself, and every other
        # name in the copy to its latest version. So the TypeVar can stay the same only if a single model uses it
        # and its fallbacks refer to no other model that changes in this version than that model
        users = {_unwrap_model(user) for user in self._typevar_users[-1]}
        generator = self.generator.schema_generator
        user = next(iter(users)) if len(users) == 1 else None
        if lenient_issubclass(user, BaseModel) and user in collector.models:
            other_models = (collector.models | collector.arg_models) - users
            if generator is not None and generator.validates_models_that_differ_from_head(other_models):
                raise SchemaGenerationError(
                    f"Cadwyn cannot version type parameter {param.__name__}: its bound, constraints, or default "
                    f"refer back to {user.__qualname__} and also to models that change in version "
                    f"{generator._version}. Use explicit type arguments instead."
                )
            return param
        if self.record_only:
            self._change_version_of_typevar_values(resolved_values)
            return param
        # Only the transformers of collectors, which only record, have no schema generator
        assert generator is not None  # noqa: S101
        if collector.is_complete and not generator.validates_models_that_differ_from_head(
            collector.models | collector.arg_models
        ):
            return param
        # One copy per version, so that e.g. `Page[T]` within `Outer[T]` stays the copy of the bare `Page`
        key = (param, resolved_values)
        try:
            return generator.typevar_copies[key]
        except (KeyError, TypeError):  # Unhashable values get a copy each time
            pass
        versioned_values = self._change_version_of_typevar_values(resolved_values)
        for value in versioned_values:
            self._check_typevar_value(generator, param, value)
        # Runtime reconstruction uses the third-party factory dynamically; static TypeVar syntax
        # cannot express bounds, constraints, and defaults discovered at runtime.
        factory = sys.modules["typing_extensions"].TypeVar
        variance = {
            "covariant": param.__covariant__,
            "contravariant": param.__contravariant__,
            "infer_variance": getattr(param, "__infer_variance__", False),
        }
        if kind == "constraints":
            copied = factory(param.__name__, *versioned_values, **variance)
        else:
            copied = factory(param.__name__, **{kind: versioned_values[0]}, **variance)
        copied.__module__ = param.__module__
        with contextlib.suppress(TypeError):
            generator.typevar_copies[key] = copied
        return copied

    def _change_version_of_typevar_values(self, values: tuple[Any, ...]) -> tuple[Any, ...]:
        # A TypeVar must be built before pydantic sees it, so cyclic bounds need the classes themselves.
        self._typevar_fallback_depth += 1
        try:
            with self.hard_position():
                return tuple(self.change_version_of_annotation(value) for value in values)
        finally:
            self._typevar_fallback_depth -= 1

    def _resolve_typevar_value(self, param: typing.TypeVar, value: Any) -> Any:
        if not _contains_forward_refs(value):
            return value
        owner = self._typevar_owners[-1]
        if lenient_issubclass(owner, BaseModel):
            # The copies of models can have other namespaces, e.g. in reference cycles
            value = _eval_forward_refs(value, _unwrap_model(owner))
        else:
            value = _try_eval_type(value, vars(sys.modules[owner.__module__]))
        if _contains_forward_refs(value):
            if self.record_only:
                self._record_unknown_references()
                return value
            raise SchemaGenerationError(
                f"Cadwyn cannot resolve {value!r} in type parameter {param.__name__}. "
                "Define the referenced types before generating versions, or use explicit type arguments."
            )
        return value

    @staticmethod
    def _check_typevar_value(generator: "SchemaGenerator", param: typing.TypeVar, value: Any) -> None:
        """Reject bounds/defaults whose copied generics still validate a changed HEAD model.

        Generic arguments can deliberately keep HEAD models to avoid reference cycles. Type parameters must
        not silently inherit that fallback, including when the generic was already copied and cached.
        """
        from typing_inspection.typing_objects import is_typealiastype

        values = [value]
        visited: set[int] = set()
        while values:
            current = values.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, type) and issubclass(current, BaseModel):
                for cls in _get_classes_in_core_schema(current.__pydantic_core_schema__):
                    if cls in generator._instructions_that_differ_from_head or (
                        issubclass(cls, BaseModel)
                        and any(generator._base_differs_from_head(cls, index) for index in range(1, len(cls.__mro__)))
                    ):
                        raise SchemaGenerationError(
                            f"Cadwyn cannot version type parameter {param.__name__}: its bound, constraints, or "
                            f"default still validate the latest {cls.__qualname__} in an older version. "
                            "Use explicit type arguments with models that Cadwyn can version."
                        )
            elif is_typealiastype(current):
                values.append(_get_type_alias_value(current))
            else:
                origin = get_origin(current)
                if is_typealiastype(origin):
                    values.append(origin)
                values.extend(get_args(current))

    def _copy_type_alias(self, alias: Any) -> Any:
        """Return the copy of the type alias whose value refers to this version's copies of models.

        A type alias that refers to itself (e.g. `type Json = dict[str, Json] | int`), directly or through other type
        aliases, cannot be copied because its copy would have to exist before its value. So we use the type alias
        itself if it only contains builtin types (see _contains_only_builtin_types), which are the same in every
        version, and fail otherwise.
        """
        if id(alias) in self._type_aliases_being_copied:
            raise _TypeAliasRefersToItselfError(alias)
        self._type_aliases_being_copied.add(id(alias))
        try:
            with self.typevar_namespace(alias):
                value = self.change_version_of_annotation(_get_type_alias_value(alias))
                type_params = tuple(self.typevar_copies.get(param, param) for param in alias.__type_params__)
        except _TypeAliasRefersToItselfError as error:
            if error.alias is not alias:
                raise
            if _contains_only_builtin_types(alias, visited_aliases=set()):
                return alias
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copies of the type alias {alias.__name__} because it refers "
                "to itself. Type aliases that refer to themselves are only supported when they only contain builtin "
                "types (e.g. `type JsonData = dict[str, JsonData] | list[JsonData] | str | int | None`) and other "
                "such type aliases. Classes (e.g. models and enums), forward refs, and Annotated are not supported."
            ) from None
        finally:
            self._type_aliases_being_copied.discard(id(alias))
        return type(alias)(name=alias.__name__, value=value, type_params=type_params)

    def _change_version_of_callable(self, annotation: Callable[..., Any], *, callable_kind: _CallableKind) -> Any:
        # Callables, such as default factories, are called with the classes themselves and their annotations are
        # not resolved by pydantic
        with (
            self.hard_position(),
            self.template_position() if callable_kind is _CallableKind.OTHER else contextlib.nullcontext(),
        ):
            if type(annotation).__module__.startswith(
                ("fastapi.", "pydantic.", "pydantic_core.", "starlette.")
            ) or isinstance(annotation, _get_fastapi_security_base_classes()):
                return annotation
            # Special forms such as typing.Self are callable but they are not functions
            if type(annotation).__module__ in {"typing", "typing_extensions"}:
                return annotation
            if callable_kind is _CallableKind.OTHER:
                try:
                    # A custom __signature__ can also fail when its parameters are read
                    _get_unevaluated_signature(annotation).parameters  # noqa: B018
                except (ValueError, TypeError):
                    # Callables without an inspectable signature such as `functools.partial(dict, count=1)`
                    # have no annotations or defaults to version
                    return annotation

            def modifier(annotation: Any):
                return self.change_version_of_annotation(annotation)

            if self.record_only:
                self._record_without_cycles(
                    annotation, lambda: self._get_annotations_and_defaults_to_record(annotation, callable_kind)
                )
                return annotation
            self._replacement_count += 1
            with self.describing(f"the parameters of {_callable_name(annotation)}"):
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

    def _record_expanded_type_alias(self, annotation: Any) -> bool:
        """Record the value of the type alias with its args, if the annotation is a type alias that we can expand.

        E.g. `Unused[Role]` with `type Unused[T] = str` records `str`, and `Alias` with `type Alias = Unused[Role]`
        records `str` too. A type alias that refers to itself is expanded once. Within its value, a reference to it
        with the same args (e.g. `Tree[Role]` within `type Tree[T] = str | list[Tree[T]]` for `Tree[Role]`) records
        nothing because the expansion that we are in already records all of its types. A reference to it with other
        args is recorded with these args, as usual, and so are the aliases that we cannot expand (e.g. with names
        that are not defined yet or with a TypeVarTuple).
        """
        value = _get_value_of_type_alias_with_args(annotation)
        # Pydantic's own type aliases (e.g. JsonValue) are never versioned (see the branch for them below)
        if value is _UNRESOLVED or (value.alias.__module__ or "").startswith("pydantic."):
            return False
        expanded_args = self._type_aliases_being_expanded.get(id(value.alias))
        if expanded_args is not None:
            return any(_are_same_args(value.args, args) for args in expanded_args)
        self._type_aliases_being_expanded[id(value.alias)] = [value.args]
        try:
            self.change_version_of_annotation(value.value)
        finally:
            del self._type_aliases_being_expanded[id(value.alias)]
        return True

    def _record_without_cycles(self, obj: Any, get_values_to_record: Callable[[], Any]) -> None:
        """Record the values within the object unless we are already recording it.

        Copying an object that leads back to itself (e.g. `type Json = dict[str, Json] | int`, or a default
        factory whose default argument is a registry that contains the factory) would recurse forever, as it does
        on main, so its references are unknown.
        """
        if id(obj) in self._ids_being_recorded:
            self._record_unknown_references()
            return
        self._ids_being_recorded.add(id(obj))
        try:
            values = get_values_to_record()
            if values is not _UNRESOLVED:
                for value in values:
                    self.change_version_of_annotation(value)
        finally:
            self._ids_being_recorded.discard(id(obj))

    def _get_annotations_and_defaults_to_record(self, call: Callable[..., Any], callable_kind: _CallableKind) -> Any:
        """Return the annotations and the defaults that _modify_callable_annotations versions, or _UNRESOLVED"""
        if callable_kind is _CallableKind.OTHER:
            # The annotations are read from the same wrapper as in _modify_callable_annotations
            wrapper = self._copy_function_through_class_based_wrapper(call, is_called_by_fastapi=False)
            if _has_undefined_names_in_lazy_annotations(wrapper):
                # Copying it fails on Python 3.14, as on main
                self._record_unknown_references()
                return _UNRESOLVED
            callable_annotations = _get_annotations_without_evaluating(wrapper)
            parameters = _get_unevaluated_signature(call).parameters
            call_globals = _get_globals(self._unwrap_callable(call))
            callable_annotations = {
                k: v if type(v) is not str else _try_eval_type_for_discovery(v, call_globals)
                for k, v in callable_annotations.items()
            }
            defaults = tuple(p.default for p in parameters.values() if p.default is not inspect.Signature.empty)
        else:
            # The models of the routes must include every model that versioning the routes requests (see
            # SchemaGenerator._add_route_models) so we read their endpoints and dependencies exactly like copying does,
            # which resolves the annotations one by one if some names are only defined for type checkers. Elsewhere,
            # we keep the decisions that main made about the models that refer to such callables
            get_signature = (
                _get_signature if self._records_routes else functools.partial(inspect.signature, eval_str=True)
            )
            signature = _evaluate_user_annotations(lambda: get_signature(call))
            if signature is _UNRESOLVED:
                # E.g. some names are not defined yet so copying could request other models once they get defined
                self._record_unknown_references()
                return _UNRESOLVED
            callable_annotations = self._get_annotations_of_callable_called_by_fastapi(
                call, signature, evaluate=_try_eval_type_for_discovery
            )
            defaults = tuple(
                param.default for param in signature.parameters.values() if param.default is not inspect.Signature.empty
            )
        # The string annotations that cannot be evaluated yet stay strings
        if any(type(value) is str for value in callable_annotations.values()):
            self._record_unknown_references()
        return [callable_annotations, defaults]

    def _record_type_alias(self, alias: Any) -> None:
        # The values of native type aliases are evaluated lazily since Python 3.12
        if _evaluate_user_annotations(lambda: alias.__value__) is _UNRESOLVED:
            self._record_unknown_references()
            return
        with self.typevar_namespace(alias):
            self._record_without_cycles(alias, lambda: [_get_type_alias_value(alias)])

    def _record_unknown_references(self) -> None:
        """Record that we cannot know every model that copying the annotation would request.

        E.g. because the annotation refers to names that are not defined yet, which could refer to any model
        once they get defined, or because copying it would never finish.
        """
        cast("_ReferencedModelCollector", self.generator).is_complete = False

    def _change_version_of_generic_alias(self, annotation: Any) -> Any:
        from typing_inspection.typing_objects import is_typealiastype

        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is ClassVar or origin is collections.abc.Callable:
            with self.template_position():
                new_args = tuple(
                    self._change_version_of_generic_arg(origin, index, arg) for index, arg in enumerate(args)
                )
            return annotation if self.record_only else _rebuild_generic_alias(annotation, new_args)
        if is_typealiastype(origin):
            versioned_args = self.change_version_of_annotation(args)
            versioned_origin = origin
            # The args replace these parameters, so only the other TypeVars in the value validate as fallbacks.
            # The value can also hold a bare generic even when none of the args contains a model
            supplied_params = origin.__type_params__[: len(args)]
            if self._typevar_fallback_depth or _get_typevar_uses(
                _get_type_alias_value(origin), ignored=frozenset(supplied_params)
            ):
                generator = self.generator.schema_generator
                if generator is None:
                    _change_version_of_alias_value(self, origin, supplied_params)
                else:
                    collector = _ReferencedModelCollector()
                    _change_version_of_alias_value(collector.annotation_transformer, origin, supplied_params)
                    if generator.validates_models_that_differ_from_head(collector.models | collector.arg_models):
                        versioned_origin = _change_version_of_alias_value(self, origin, supplied_params)
            if self.record_only:
                return annotation
            return versioned_origin[versioned_args[0] if len(versioned_args) == 1 else versioned_args]
        new_args = tuple(self._change_version_of_generic_arg(origin, index, arg) for index, arg in enumerate(args))
        if self.record_only:
            return annotation
        if origin is Annotated and self._has_dependency_that_differs_from_head(args):
            # typing caches Annotated[...] by "==" and "hash", and our dependency wrappers are equal to the originals
            # (so that dependency_overrides match them), so typing would usually return the alias that holds
            # the original dependency (#467). We bypass its cache only where the original dependency would validate
            # requests differently, so everywhere else the dependency keeps getting the original classes, as before
            if self._has_inferred_dependency(args) and not isinstance(args[0], type):
                # FastAPI uses the first arg as the dependency (e.g. `Annotated[get_roles, Depends()]`)
                with self.hard_position():
                    dependency = self.change_versions_of_a_non_container_annotation(
                        args[0], callable_kind=_CallableKind.DEPENDENCY
                    )
                new_args = (dependency, *new_args[1:])
            return _AnnotatedAlias(new_args[0], new_args[1:])
        return _rebuild_generic_alias(annotation, new_args)

    def _has_dependency_that_differs_from_head(self, annotated_args: tuple[Any, ...]) -> bool:
        generator = self.generator
        return (
            generator.schema_generator is generator
            and any(
                isinstance(value, _get_fastapi_depends_classes())
                # FastAPI infers the dependency from the type when it is not set (e.g. `Annotated[Params, Depends()]`)
                and generator.dependency_differs_from_head(
                    annotated_args[0] if value.dependency is None else value.dependency
                )
                for value in annotated_args[1:]
            )
        )

    @staticmethod
    def _has_inferred_dependency(annotated_args: tuple[Any, ...]) -> bool:
        return any(
            isinstance(value, _get_fastapi_depends_classes()) and value.dependency is None
            for value in annotated_args[1:]
        )

    def _change_version_of_generic_arg(self, origin: Any, index: int, arg: Any) -> Any:
        if _is_type_position_in_generic(origin, index):
            return self.change_version_of_annotation(arg)
        is_used_by_pydantic = origin is Annotated and _is_metadata_that_pydantic_uses(arg)
        self._pydantic_metadata_depth += is_used_by_pydantic
        try:
            with self.hard_position():
                return self.change_version_of_annotation(arg)
        finally:
            self._pydantic_metadata_depth -= is_used_by_pydantic

    @property
    def is_in_pydantic_metadata(self) -> bool:
        """Whether we are versioning the metadata of Annotated that pydantic validates or builds schemas with"""
        return self._pydantic_metadata_depth > 0

    def _change_version_of_type(self, annotation: type) -> type:
        if lenient_issubclass(annotation, (BaseModel, Enum)):
            if (
                self._template_position_depth or self._is_template_reference(annotation)
            ) and self.generator.schema_generator is not None:
                depth = self._template_position_depth
                self._template_position_depth = 0
                try:
                    versioned_class = self.generator._get_template(annotation)
                finally:
                    self._template_position_depth = depth
            else:
                versioned_class = self.generator[annotation]
            if versioned_class is not annotation:
                self._replacement_count += 1
            return versioned_class
        else:
            return annotation

    def _is_template_reference(self, annotation: type) -> bool:
        """Keep nested generics sharing the enclosing model's parameters as templates.

        Binding Inner[T] while copying Outer[T] would prevent Outer[int] from substituting int,
        and would inspect a bound which that concrete parametrization never validates.
        """
        if not self._model_owners or not _is_versionable_model(annotation) or not issubclass(annotation, BaseModel):
            return False
        parameters = self._model_owners[-1].__pydantic_generic_metadata__["parameters"]
        return bool(set(parameters).intersection(annotation.__pydantic_generic_metadata__["parameters"]))

    @classmethod
    def _remake_endpoint_dependencies(cls, route: "fastapi.routing.APIRoute") -> None:
        import fastapi.routing

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
            call_globals = _get_globals(cls._unwrap_callable(call))
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
            return_annotation = old_signature.return_annotation
            callable_annotations = cls._get_annotations_of_callable_called_by_fastapi(call, old_signature)
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            if return_annotation is not inspect.Signature.empty:
                # FastAPI validates the route's explicit response_model, which is versioned separately, and never
                # validates dependency returns. Copying these annotations can reject models that no route uses.
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

    @classmethod
    def _get_annotations_of_callable_called_by_fastapi(
        cls,
        call: Any,
        old_signature: inspect.Signature,
        *,
        evaluate: Union[Callable[[Any, dict[str, Any]], Any], None] = None,
    ) -> dict[str, Any]:
        """Return the annotations of an endpoint or a dependency that its versioned copy versions"""
        callable_annotations = {
            name: param.annotation
            for name, param in old_signature.parameters.items()
            if param.annotation is not inspect.Parameter.empty
        }
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
        evaluate = evaluate or _try_eval_type
        return {
            k: evaluate(v, call_globals) if k in annotations_written_as_strings else v
            for k, v in callable_annotations.items()
        }

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


@final
class _ReferencedModelCollector:
    """A generator that records the models requested from it instead of generating their copies"""

    __slots__ = "annotation_transformer", "arg_models", "base_models", "hard_models", "is_complete", "models"

    def __init__(self, *, expands_type_aliases: bool = False) -> None:
        self.annotation_transformer = _AnnotationTransformer(
            self, record_only=True, expands_type_aliases=expands_type_aliases
        )
        self.models: set[type[Union[BaseModel, Enum]]] = set()
        self.hard_models: set[type[Union[BaseModel, Enum]]] = set()
        self.arg_models: set[type[Union[BaseModel, Enum]]] = set()
        self.base_models: set[type[Union[BaseModel, Enum]]] = set()
        # Whether the annotations that we looked at only refer to names that are already defined
        self.is_complete = True

    @property
    def schema_generator(self) -> None:
        return None

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(model):
            original = _unwrap_model(model)
            self.models.add(original)
            # The versioned copies of bare generics are not generic, so we look at the original, as the generator
            if (
                issubclass(original, BaseModel)
                and original.__pydantic_generic_metadata__["parameters"]
                and not self.annotation_transformer._template_position_depth
                and not self.annotation_transformer._is_template_reference(model)
                and model not in self.annotation_transformer._model_owners
            ):
                transformer = self.annotation_transformer
                with transformer.typevar_namespace(original):
                    transformer._record_without_cycles(original, lambda: [tuple(_get_typevar_uses(original))])
            if self.annotation_transformer.is_in_hard_position:
                self.hard_models.add(_unwrap_model(model))
        return model

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(base):
            self.base_models.add(_unwrap_model(base))
        return base

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        generic_metadata = model.__pydantic_generic_metadata__
        arg_collector = _ReferencedModelCollector()
        args = _eval_generic_args(model)
        arg_collector.annotation_transformer.change_version_of_annotation(args)
        self.arg_models |= arg_collector.models
        # The quoted args that cannot be evaluated yet stay forward refs
        if not arg_collector.is_complete or any(_contains_forward_refs(arg) for arg in args):
            self.is_complete = False
        if generic_metadata["origin"] is not None:
            # A parametrized generic is a subclass of its origin
            self.change_version_of_base(generic_metadata["origin"])
        return generic_metadata

    def get_references(self) -> "_ModelReferences":
        return _ModelReferences(
            frozenset(self.models),
            frozenset(self.arg_models),
            frozenset(self.base_models),
            self.is_complete,
            frozenset(self.hard_models),
        )


class _ModelReferences(NamedTuple):
    # The models that are always versioned along with the model: in its fields, annotations, etc
    models: frozenset[type[Union[BaseModel, Enum]]]
    # The models in the type args of a parametrized generic (e.g. Item in Page[Item])
    arg_models: frozenset[type[Union[BaseModel, Enum]]]
    # The bases of the model and the origin of a parametrized generic (e.g. Page in Page[Item])
    base_models: frozenset[type[Union[BaseModel, Enum]]]
    # Whether we know all of the models that copying the model would request. We do not when some names in its
    # annotations (fields, class variables, quoted generic args, annotations of default factories, values of type
    # aliases, etc.) are not defined yet: once they get defined, copying the model can request any model. We do not
    # either when copying it would recurse forever through a type alias that refers to itself. So we always copy
    # such a model and never version the generic args that lead to it, even after the names get defined, which
    # keeps them exactly as on main.
    is_complete: bool = True
    # The models among `models` that are requested in positions that need the classes themselves rather than
    # forward refs to them, e.g. default factories (see _AnnotationTransformer.is_in_hard_position)
    hard_models: frozenset[type[Union[BaseModel, Enum]]] = frozenset()


class _GenerationAnalysis(NamedTuple):
    # Whether generating the copy of the model will not recurse forever
    terminates: bool
    # Whether the model or the models that it refers to have schema or enum instructions
    refers_to_changed_models: bool


_METADATA_THAT_PYDANTIC_USES = (
    FieldInfo,
    AfterValidator,
    BeforeValidator,
    PlainValidator,
    WrapValidator,
    PlainSerializer,
    WrapSerializer,
)


def _is_metadata_that_pydantic_uses(metadata: Any) -> bool:
    """Whether pydantic validates or builds schemas with the metadata of Annotated, unlike e.g. `[Model]`"""
    return (
        isinstance(metadata, _METADATA_THAT_PYDANTIC_USES)
        or hasattr(metadata, "__get_pydantic_core_schema__")
        or hasattr(metadata, "__get_pydantic_json_schema__")
    )


def _change_version_of_alias_value(
    transformer: _AnnotationTransformer, alias: Any, supplied_params: tuple[typing.TypeVar, ...]
) -> Any:
    """Version the type alias whose first parameters get args, keeping these parameters as they are"""
    with transformer.typevar_namespace(alias):
        transformer.typevar_copies.update({param: param for param in supplied_params})
        return transformer.change_version_of_annotation(alias)


def _get_models_validated_by_fields(model: type[BaseModel]) -> set[type[Union[BaseModel, Enum]]]:
    """Return the models and enums in the types of the resolved fields of the model, including typed extras"""
    fields = _get_rebuilt_model_fields(model)
    annotations = [field.annotation for field in (model.model_fields if fields is None else fields).values()]
    if model.model_config.get("extra") == "allow":
        annotations.append(_get_extra_fields_annotation(model))
    collector = _ReferencedModelCollector(expands_type_aliases=True)
    # Pydantic validates the TypeVars left in the resolved fields as their bounds, constraints, or defaults
    with collector.annotation_transformer.typevar_namespace(model):
        for annotation in annotations:
            collector.annotation_transformer.change_version_of_annotation(annotation)
    return collector.models | collector.arg_models
