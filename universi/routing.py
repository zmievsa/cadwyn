import datetime
import functools
import inspect
import typing
from collections.abc import Callable, Sequence
from copy import deepcopy
from enum import Enum
from pathlib import Path
from types import GenericAlias, MappingProxyType, ModuleType
from typing import (
    Any,
    TypeVar,
    _BaseGenericAlias,  # pyright: ignore[reportGeneralTypeIssues]
    cast,
    get_args,
    get_origin,
)

import fastapi.routing
from fastapi.dependencies.utils import (
    get_body_field,
    get_dependant,
    get_parameterless_sub_dependant,
)
from fastapi.params import Depends
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette._utils import is_async_callable
from starlette.routing import (
    BaseRoute,
    request_response,
)
from typing_extensions import Self, assert_never

from universi._utils import Sentinel, UnionType, get_another_version_of_cls
from universi.codegen import _get_package_path_from_module, _get_version_dir_path
from universi.exceptions import RouterGenerationError
from universi.structure import Version, VersionBundle
from universi.structure.common import Endpoint
from universi.structure.endpoints import (
    EndpointDidntExistInstruction,
    EndpointExistedInstruction,
    EndpointHadInstruction,
)
from universi.structure.versions import VersionChange

_T = TypeVar("_T", bound=Callable[..., Any])
AnnotationChanger = Callable[[Any, Path, "AnnotationChanger", frozenset[Path]], Any]


def same_definition_as_in(t: _T) -> Callable[[Callable], _T]:
    def decorator(f: Callable) -> _T:
        return f  # pyright: ignore[reportGeneralTypeIssues]

    return decorator


class VersionedAPIRouter(fastapi.routing.APIRouter):
    @same_definition_as_in(fastapi.routing.APIRouter.__init__)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._deleted_routes: list[APIRoute] = []

    # TODO: Raise error if endpoint was marked with this but was never used in any version
    def only_exists_in_older_versions(self, endpoint: _T) -> _T:
        index, route = _get_index_and_route_from_func(self.routes, endpoint)
        if index is None or route is None:
            raise LookupError(f"Route not found on endpoint: '{endpoint.__name__}'")
        self.routes.pop(index)
        self._deleted_routes.append(route)
        return endpoint

    def create_versioned_copies(
        self,
        versions: VersionBundle,
        *,
        latest_schemas_module: ModuleType | None,
    ) -> dict[datetime.date, Self]:
        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/fastapi do any comparisons.
        change_simple_annotation = functools.cache(_change_versions_of_a_non_container_annotation)
        if latest_schemas_module is not None:
            version_dirs = frozenset(
                [_get_package_path_from_module(latest_schemas_module)]
                + [_get_version_dir_path(latest_schemas_module, version.date) for version in versions],
            )
        else:
            version_dirs: frozenset[Path] = frozenset()
        _add_data_migrations_to_all_routes(self, versions)
        router = self
        routers = {}
        for version in versions:
            if latest_schemas_module:
                version_dir = _get_version_dir_path(latest_schemas_module, version.date)
                if not version_dir.is_dir():
                    raise RouterGenerationError(
                        f"Versioned schema directory '{version_dir}' does not exist.",
                    )
                for route in router.routes:
                    if not isinstance(route, APIRoute):
                        continue
                    if route.response_model is not None:
                        route.response_model = _change_versions_of_all_annotations(
                            route.response_model,
                            version_dir,
                            change_simple_annotation,
                            version_dirs,
                        )
                    route.dependencies = _change_versions_of_all_annotations(
                        route.dependencies,
                        version_dir,
                        change_simple_annotation,
                        version_dirs,
                    )
                    route.endpoint = _change_versions_of_all_annotations(
                        route.endpoint,
                        version_dir,
                        change_simple_annotation,
                        version_dirs,
                    )
                    route.dependant = get_dependant(
                        path=route.path_format,
                        call=route.endpoint,
                    )
                    route.body_field = get_body_field(
                        dependant=route.dependant,
                        name=route.unique_id,
                    )
                    for depends in route.dependencies[::-1]:
                        route.dependant.dependencies.insert(
                            0,
                            get_parameterless_sub_dependant(
                                depends=depends,
                                path=route.path_format,
                            ),
                        )
                    route.app = request_response(route.get_route_handler())

            routers[version.date] = router
            router = deepcopy(router)
            _apply_endpoint_changes_to_router(router, version)
        return routers


def _add_data_migrations_to_all_routes(router: VersionedAPIRouter, versions: VersionBundle):
    for route in router.routes + router._deleted_routes:
        if isinstance(route, APIRoute):
            if not is_async_callable(route.endpoint):
                raise RouterGenerationError("All versioned endpoints must be asynchronous.")
            route.endpoint = versions.versioned(route.response_model)(route.endpoint)


# TODO: This code is slow. It does a lot of unnecessary actions such as list seeks. Optimize it maybe?
# TODO: This code is very complex. But no matter how I try to refactor it, nothing really fits.
def _apply_endpoint_changes_to_router(router: VersionedAPIRouter, version: Version):
    routes = cast(list[APIRoute], router.routes)
    for version_change in version.version_changes:
        for instruction in version_change.alter_endpoint_instructions:
            original_routes = _get_routes(routes, instruction.endpoint_path, instruction.endpoint_methods)
            methods_to_which_we_applied_changes = set()
            methods_we_should_have_applied_changes_to = set(instruction.endpoint_methods)

            if isinstance(instruction, EndpointDidntExistInstruction):
                for original_route in original_routes:
                    methods_to_which_we_applied_changes |= original_route.methods
                    # OP
                    router.routes.remove(original_route)
                err = (
                    'Endpoint "{endpoint_methods} {endpoint_path}" you tried to delete in'
                    ' "{version_change_name}" doesn\'t exist in a newer version'
                )
            elif isinstance(instruction, EndpointExistedInstruction):
                if original_routes:
                    method_union = set()
                    for original_route in original_routes:
                        method_union |= original_route.methods
                    raise RouterGenerationError(
                        f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to re-create in'
                        f' "{version_change.__name__}" already existed in a newer version',
                    )
                deleted_routes = _get_routes(
                    router._deleted_routes,
                    instruction.endpoint_path,
                    instruction.endpoint_methods,
                )
                for deleted_route in deleted_routes:
                    methods_to_which_we_applied_changes |= deleted_route.methods
                    # OP
                    router._deleted_routes.remove(deleted_route)
                    router.routes.append(deleted_route)
                err = (
                    'Endpoint "{endpoint_methods} {endpoint_path}" you tried to re-create in'
                    ' "{version_change_name}" wasn\'t among the deleted routes'
                )
            elif isinstance(instruction, EndpointHadInstruction):
                for original_route in original_routes:
                    methods_to_which_we_applied_changes |= original_route.methods
                    # OP
                    _apply_endpoint_had_instruction(version_change, instruction, original_route)
                err = (
                    'Endpoint "{endpoint_methods} {endpoint_path}" you tried to change in'
                    ' "{version_change_name}" doesn\'t exist'
                )
            else:
                assert_never(instruction)
            method_diff = methods_we_should_have_applied_changes_to - methods_to_which_we_applied_changes
            if method_diff:
                # ERR
                raise RouterGenerationError(
                    err.format(
                        endpoint_methods=list(method_diff),
                        endpoint_path=instruction.endpoint_path,
                        version_change_name=version_change.__name__,
                    ),
                )


def _apply_endpoint_had_instruction(
    version_change: type[VersionChange],
    instruction: EndpointHadInstruction,
    original_route: APIRoute,
):
    for attr_name in instruction.attributes.__dataclass_fields__:
        attr = getattr(instruction.attributes, attr_name)
        if attr is not Sentinel:
            if getattr(original_route, attr_name) == attr:
                raise RouterGenerationError(
                    f'Expected attribute "{attr_name}" of endpoint'
                    f' "{list(original_route.methods)} {original_route.path}"'
                    f' to be different in "{version_change.__name__}", but it was the same.'
                    " It means that your version change has no effect on the attribute"
                    " and can be removed.",
                )
            setattr(original_route, attr_name, attr)


def _change_versions_of_all_annotations(
    annotation: Any,
    version_dir: Path,
    change_simple_annotation: AnnotationChanger,
    version_dirs: frozenset[Path],
) -> Any:
    """Recursively go through all annotations and if they were taken from any versioned package, change them to the
    annotations corresponding to the version_dir passed.

    So if we had a annotation "UserResponse" from "latest" version, and we passed version_dir of "v1_0_1", it would
    replace "UserResponse" with the the same class but from the "v1_0_1" version.

    """
    if isinstance(annotation, dict):
        return {
            _change_versions_of_all_annotations(
                key,
                version_dir,
                change_simple_annotation,
                version_dirs,
            ): _change_versions_of_all_annotations(value, version_dir, change_simple_annotation, version_dirs)
            for key, value in annotation.items()
        }

    elif isinstance(annotation, list | tuple):
        return type(annotation)(
            _change_versions_of_all_annotations(v, version_dir, change_simple_annotation, version_dirs)
            for v in annotation
        )
    else:
        return change_simple_annotation(annotation, version_dir, change_simple_annotation, version_dirs)


def _change_versions_of_a_non_container_annotation(
    annotation: Any,
    version_dir: Path,
    change_simple_annotation: AnnotationChanger,
    version_dirs: frozenset[Path],
) -> Any:
    if isinstance(annotation, _BaseGenericAlias | GenericAlias):
        return get_origin(annotation)[
            tuple(
                _change_versions_of_all_annotations(arg, version_dir, change_simple_annotation, version_dirs)
                for arg in get_args(annotation)
            )
        ]
    elif isinstance(annotation, Depends):
        return Depends(
            _change_versions_of_all_annotations(
                annotation.dependency,
                version_dir,
                change_simple_annotation,
                version_dirs,
            ),
            use_cache=annotation.use_cache,
        )
    elif isinstance(annotation, UnionType):
        getitem = typing.Union.__getitem__  # pyright: ignore[reportGeneralTypeIssues]
        return getitem(
            tuple(
                _change_versions_of_all_annotations(a, version_dir, change_simple_annotation, version_dirs)
                for a in get_args(annotation)
            ),
        )
    elif annotation is typing.Any or isinstance(annotation, typing.NewType):
        return annotation
    elif isinstance(annotation, type):
        if issubclass(annotation, BaseModel | Enum):
            return get_another_version_of_cls(annotation, version_dir, version_dirs)
        else:
            return annotation
    elif callable(annotation):
        if inspect.iscoroutinefunction(annotation):

            @functools.wraps(annotation)
            async def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                return await annotation(*args, **kwargs)

        else:

            @functools.wraps(annotation)
            def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                return annotation(*args, **kwargs)

        # Otherwise it will have the same signature as __wrapped__
        del new_callable.__wrapped__
        old_params = inspect.signature(annotation).parameters
        callable_annotations = new_callable.__annotations__

        new_callable: Any = cast(Any, new_callable)
        new_callable.__annotations__ = _change_versions_of_all_annotations(
            callable_annotations,
            version_dir,
            change_simple_annotation,
            version_dirs,
        )
        new_callable.__defaults__ = _change_versions_of_all_annotations(
            tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
            version_dir,
            change_simple_annotation,
            version_dirs,
        )
        new_callable.__signature__ = _generate_signature(new_callable, old_params)
        return new_callable
    else:
        return annotation


def _generate_signature(
    new_callable: Callable,
    old_params: MappingProxyType[str, inspect.Parameter],
):
    parameters = []
    default_counter = 0
    for param in old_params.values():
        if param.default is not inspect.Signature.empty:
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


def _get_routes(
    routes: Sequence[BaseRoute | APIRoute],
    endpoint_path: str,
    endpoint_methods: list[str],
) -> list[APIRoute]:
    found_routes = []
    endpoint_method_set = set(endpoint_methods)
    for _index, route in enumerate(routes):
        if (
            isinstance(route, APIRoute)
            and route.path == endpoint_path
            and set(route.methods).issubset(endpoint_method_set)
        ):
            found_routes.append(route)
    return found_routes


def _get_index_and_route_from_func(
    routes: Sequence[BaseRoute | APIRoute],
    endpoint: Endpoint,
) -> tuple[int, APIRoute] | tuple[None, None]:
    for index, route in enumerate(routes):
        if isinstance(route, APIRoute) and (
            route.endpoint == endpoint or getattr(route.endpoint, "func", None) == endpoint
        ):
            return index, route
    return None, None
