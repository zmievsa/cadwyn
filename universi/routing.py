import datetime
import functools
import inspect
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

from universi._utils import Sentinel, get_another_version_of_cls
from universi.codegen import _get_version_dir_path
from universi.exceptions import RouterGenerationError
from universi.structure.common import Endpoint
from universi.structure.endpoints import (
    EndpointDidntExistInstruction,
    EndpointExistedInstruction,
    EndpointHadInstruction,
)
from universi.structure.versions import VersionBundle

_T = TypeVar("_T", bound=Callable[..., Any])
AnnotationChanger = Callable[[Any, Path, "AnnotationChanger"], Any]


def same_definition_as_in(t: _T) -> Callable[[Callable], _T]:
    def decorator(f: Callable) -> _T:
        return f  # pyright: ignore[reportGeneralTypeIssues]

    return decorator


class VersionedAPIRouter(fastapi.routing.APIRouter):
    @same_definition_as_in(fastapi.routing.APIRouter.__init__)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._deleted_routes: list[APIRoute] = []

    def only_exists_in_older_versions(self, endpoint: _T) -> _T:
        index, route = _get_index_and_route(self.routes, endpoint)
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

        router = self
        for route in router.routes + router._deleted_routes:
            if isinstance(route, APIRoute):
                if not is_async_callable(route.endpoint):
                    raise TypeError(
                        "All versioned endpoints must be asynchronous.",
                    )
                route.endpoint = versions.versioned()(route.endpoint)
        routers = {}
        for version in versions.versions:
            if latest_schemas_module:
                version_dir = _get_version_dir_path(latest_schemas_module, version.date)
                if not version_dir.is_dir():
                    raise RouterGenerationError(
                        f"Versioned schema directory '{version_dir}' does not exist.",
                    )
                for route in router.routes:
                    if isinstance(route, APIRoute):
                        if route.response_model is not None:
                            route.response_model = _change_versions_of_all_annotations(
                                route.response_model,
                                version_dir,
                                change_simple_annotation,
                            )
                        route.dependencies = _change_versions_of_all_annotations(
                            route.dependencies,
                            version_dir,
                            change_simple_annotation,
                        )
                        route.endpoint = _change_versions_of_all_annotations(
                            route.endpoint,
                            version_dir,
                            change_simple_annotation,
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
            for version_change in version.version_changes:
                for instruction in version_change.alter_endpoint_instructions:
                    original_route_index, original_route = _get_index_and_route(
                        router.routes,
                        instruction.endpoint,
                    )
                    if isinstance(instruction, EndpointDidntExistInstruction):
                        if original_route_index is None:
                            raise RouterGenerationError(
                                f"Endpoint '{instruction.endpoint.__name__}' you tried to delete in"
                                f" '{version_change.__name__}' doesn't exist in new version",
                            )
                        router.routes.pop(original_route_index)
                    elif isinstance(instruction, EndpointExistedInstruction):
                        if original_route_index is not None:
                            raise RouterGenerationError(
                                f"Endpoint '{instruction.endpoint.__name__}' you tried to re-create in"
                                f" '{version_change.__name__}' already existed in newer versions",
                            )
                        deleted_route_index, _ = _get_index_and_route(router._deleted_routes, instruction.endpoint)
                        if deleted_route_index is None:
                            raise RouterGenerationError(
                                f"Endpoint '{instruction.endpoint.__name__}' you tried to re-create in"
                                f" '{version_change.__name__}' wasn't among the deleted routes",
                            )
                        router.routes.append(
                            router._deleted_routes.pop(deleted_route_index),
                        )
                    elif isinstance(instruction, EndpointHadInstruction):
                        if original_route_index is None or original_route is None:
                            raise RouterGenerationError(
                                f"Endpoint '{instruction.endpoint.__name__}' you tried to delete in"
                                f" '{version_change.__name__}' doesn't exist in new version",
                            )
                        for attr_name in instruction.attributes.__dataclass_fields__:
                            attr = getattr(instruction.attributes, attr_name)
                            if attr is not Sentinel:
                                if getattr(original_route, attr_name) == attr:
                                    raise RouterGenerationError(
                                        f"Expected attribute '{attr_name}' of endpoint"
                                        f" '{original_route.endpoint.__name__}' to be different in"
                                        f" '{version_change.__name__}', but it was the same."
                                        " It means that your version change has no effect on the attribute"
                                        " and can be removed.",
                                    )
                                setattr(original_route, attr_name, attr)
                    else:
                        assert_never(instruction)
        return routers


def _change_versions_of_all_annotations(
    annotation: Any,
    version_dir: Path,
    change_simple_annotation: AnnotationChanger,
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
            ): _change_versions_of_all_annotations(value, version_dir, change_simple_annotation)
            for key, value in annotation.items()
        }

    elif isinstance(annotation, list | tuple):
        return type(annotation)(
            _change_versions_of_all_annotations(v, version_dir, change_simple_annotation) for v in annotation
        )
    else:
        return change_simple_annotation(annotation, version_dir, change_simple_annotation)


def _change_versions_of_a_non_container_annotation(
    annotation: Any,
    version_dir: Path,
    change_simple_annotation: AnnotationChanger,
) -> Any:
    if isinstance(annotation, _BaseGenericAlias | GenericAlias):
        return _change_versions_of_all_annotations(get_origin(annotation), version_dir, change_simple_annotation)[
            tuple(
                _change_versions_of_all_annotations(arg, version_dir, change_simple_annotation)
                for arg in get_args(annotation)
            )
        ]
    elif isinstance(annotation, Depends):
        return Depends(
            _change_versions_of_all_annotations(annotation.dependency, version_dir, change_simple_annotation),
            use_cache=annotation.use_cache,
        )
    elif isinstance(annotation, type):
        if issubclass(annotation, BaseModel | Enum):
            return get_another_version_of_cls(annotation, version_dir)
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
        )
        new_callable.__defaults__ = _change_versions_of_all_annotations(
            tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
            version_dir,
            change_simple_annotation,
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


def _get_index_and_route(
    routes: Sequence[BaseRoute | APIRoute],
    endpoint: Endpoint,
) -> tuple[int, APIRoute] | tuple[None, None]:
    for index, route in enumerate(routes):
        if isinstance(route, APIRoute) and (
            route.endpoint == endpoint or getattr(route.endpoint, "func", None) == endpoint
        ):
            return index, route
    return None, None
