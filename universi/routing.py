import datetime
import functools
import inspect
import typing
import warnings
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
        self.deleted_routes: list[APIRoute] = []

    def only_exists_in_older_versions(self, endpoint: _T) -> _T:
        index, route = _get_index_and_route_from_func(self.routes, endpoint)
        if index is None or route is None:
            raise LookupError(f"Route not found on endpoint: '{endpoint.__name__}'")
        self.routes.pop(index)
        self.deleted_routes.append(route)
        return endpoint

    def create_versioned_copies(
        self,
        versions: VersionBundle,
        *,
        latest_schemas_module: ModuleType | None,
    ) -> dict[datetime.date, Self]:
        _add_data_migrations_to_all_routes(self, versions)
        return _EndpointTransformer(self, versions, latest_schemas_module).transform()


class _EndpointTransformer:
    def __init__(
        self,
        parent_router: VersionedAPIRouter,
        versions: VersionBundle,
        latest_schemas_module: ModuleType | None,
    ) -> None:
        self.parent_router = parent_router
        self.versions = versions
        if latest_schemas_module is not None:
            self.annotation_transformer = _AnnotationTransformer(latest_schemas_module, versions)
        else:
            self.annotation_transformer = None

        self.routes_that_never_existed = parent_router.deleted_routes.copy()

    def transform(self):
        router = self.parent_router
        routers = {}
        for version in self.versions:
            if self.annotation_transformer:
                self.annotation_transformer.migrate_router_to_version(router, version)

            routers[version.value] = router
            router = deepcopy(router)
            self._apply_endpoint_changes_to_router(router, version)
        if self.routes_that_never_existed:
            raise RouterGenerationError(
                "Every route you mark with "
                f"@VersionedAPIRouter.{VersionedAPIRouter.only_exists_in_older_versions.__name__} "
                "must be restored in one of the older versions. Otherwise you just need to delete it altogether. "
                "The following routes have been marked with that decorator but were never restored: "
                f"{self.routes_that_never_existed}",
            )
        return routers

    def _apply_endpoint_changes_to_router(self, router: VersionedAPIRouter, version: Version):  # noqa: C901
        routes = cast(list[APIRoute], router.routes)
        for version_change in version.version_changes:
            for instruction in version_change.alter_endpoint_instructions:
                original_routes = _get_routes(routes, instruction.endpoint_path, instruction.endpoint_methods)
                methods_to_which_we_applied_changes = set()
                methods_we_should_have_applied_changes_to = set(instruction.endpoint_methods)

                if isinstance(instruction, EndpointDidntExistInstruction):
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        router.routes.remove(original_route)
                        router.deleted_routes.append(original_route)
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
                            f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to restore in'
                            f' "{version_change.__name__}" already existed in a newer version',
                        )
                    deleted_routes = _get_routes(
                        router.deleted_routes,
                        instruction.endpoint_path,
                        instruction.endpoint_methods,
                    )
                    for deleted_route in deleted_routes:
                        methods_to_which_we_applied_changes |= deleted_route.methods
                        router.deleted_routes.remove(deleted_route)
                        router.routes.append(deleted_route)

                        if deleted_route in self.routes_that_never_existed:
                            self.routes_that_never_existed.remove(deleted_route)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to restore in'
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


class _AnnotationTransformer:
    __slots__ = (
        "latest_schemas_module",
        "version_dirs",
        "template_version_dir",
        "latest_version_dir",
        "change_versions_of_a_non_container_annotation",
    )

    def __init__(self, latest_schemas_module: ModuleType, versions: VersionBundle) -> None:
        if not hasattr(latest_schemas_module, "__path__"):
            raise RouterGenerationError(
                f'The latest schemas module must be a package. "{latest_schemas_module.__name__}" is not a package.',
            )
        if not latest_schemas_module.__name__.endswith(".latest"):
            raise RouterGenerationError(
                'The name of the latest schemas module must be "latest". '
                f'Received "{latest_schemas_module.__name__}" instead.',
            )
        self.latest_schemas_module = latest_schemas_module
        self.version_dirs = frozenset(
            [_get_package_path_from_module(latest_schemas_module)]
            + [_get_version_dir_path(latest_schemas_module, version.value) for version in versions],
        )
        # Okay, the naming is confusing, I know. Essentially template_version_dir is a dir of
        # latest_schemas_module while latest_version_dir is a version equivalent to latest but
        # with its own directory. Pick a better naming and make a PR, I am at your mercy.
        self.template_version_dir = min(self.version_dirs)  # "latest" < "v0000_00_00"
        self.latest_version_dir = max(self.version_dirs)  # "v2005_11_11" > "v2000_11_11"

        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/fastapi do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        self.change_versions_of_a_non_container_annotation = functools.cache(
            self._change_versions_of_a_non_container_annotation,
        )

    def migrate_router_to_version(self, router: VersionedAPIRouter, version: Version):
        version_dir = _get_version_dir_path(self.latest_schemas_module, version.value)
        if not version_dir.is_dir():
            raise RouterGenerationError(
                f"Versioned schema directory '{version_dir}' does not exist.",
            )
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.response_model is not None:
                route.response_model = self._change_version_of_annotations(route.response_model, version_dir)
            route.dependencies = self._change_version_of_annotations(route.dependencies, version_dir)
            route.endpoint = self._change_version_of_annotations(route.endpoint, version_dir)
            route.dependant = get_dependant(path=route.path_format, call=route.endpoint)
            route.body_field = get_body_field(dependant=route.dependant, name=route.unique_id)
            for depends in route.dependencies[::-1]:
                route.dependant.dependencies.insert(
                    0,
                    get_parameterless_sub_dependant(depends=depends, path=route.path_format),
                )
            route.app = request_response(route.get_route_handler())

    def _change_versions_of_a_non_container_annotation(self, annotation: Any, version_dir: Path) -> Any:
        if isinstance(annotation, _BaseGenericAlias | GenericAlias):
            return get_origin(annotation)[
                tuple(self._change_version_of_annotations(arg, version_dir) for arg in get_args(annotation))
            ]
        elif isinstance(annotation, Depends):
            return Depends(
                self._change_version_of_annotations(annotation.dependency, version_dir),
                use_cache=annotation.use_cache,
            )
        elif isinstance(annotation, UnionType):
            getitem = typing.Union.__getitem__  # pyright: ignore[reportGeneralTypeIssues]
            return getitem(
                tuple(self._change_version_of_annotations(a, version_dir) for a in get_args(annotation)),
            )
        elif annotation is typing.Any or isinstance(annotation, typing.NewType):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation, version_dir)
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
            new_callable.__annotations__ = self._change_version_of_annotations(
                callable_annotations,
                version_dir,
            )
            new_callable.__defaults__ = self._change_version_of_annotations(
                tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
                version_dir,
            )
            new_callable.__signature__ = _generate_signature(new_callable, old_params)
            return new_callable
        else:
            return annotation

    def _change_version_of_annotations(self, annotation: Any, version_dir: Path) -> Any:
        """Recursively go through all annotations and if they were taken from any versioned package, change them to the
        annotations corresponding to the version_dir passed.

        So if we had a annotation "UserResponse" from "latest" version, and we passed version_dir of "v1_0_1", it would
        replace "UserResponse" with the the same class but from the "v1_0_1" version.

        """
        if isinstance(annotation, dict):
            return {
                self._change_version_of_annotations(key, version_dir): self._change_version_of_annotations(
                    value,
                    version_dir,
                )
                for key, value in annotation.items()
            }

        elif isinstance(annotation, (list, tuple)):
            return type(annotation)(self._change_version_of_annotations(v, version_dir) for v in annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation, version_dir)

    def _change_version_of_type(self, annotation: type, version_dir: Path):
        if issubclass(annotation, BaseModel | Enum):
            if version_dir == self.latest_version_dir:
                source_file = inspect.getsourcefile(annotation)
                if source_file is None:  # pragma: no cover # I am not even sure how to cover this
                    warnings.warn(
                        f'Failed to find where the type annotation "{annotation}" is located.'
                        "Please, double check that it's located in the right directory",
                        stacklevel=7,
                    )
                else:
                    template_dir = str(self.template_version_dir)
                    dir_with_versions = str(self.template_version_dir.parent)

                    # So if it is somewhere close to version dirs (either within them or next to them),
                    # but not located in "latest",
                    # but also not located in any other version dir
                    if (
                        source_file.startswith(dir_with_versions)
                        and not source_file.startswith(template_dir)
                        and any(source_file.startswith(str(d)) for d in self.version_dirs)
                    ):
                        raise RouterGenerationError(
                            f'"{annotation}" is not defined in "{self.template_version_dir}" even though it must be. '
                            f'It is defined in "{Path(source_file).parent}". '
                            "It probably means that you used a specific version of the class in fastapi dependencies "
                            'or pydantic schemas instead of "latest".',
                        )
            return get_another_version_of_cls(annotation, version_dir, self.version_dirs)
        else:
            return annotation


def _add_data_migrations_to_all_routes(router: VersionedAPIRouter, versions: VersionBundle):
    for route in router.routes + router.deleted_routes:
        if isinstance(route, APIRoute):
            if not is_async_callable(route.endpoint):
                raise RouterGenerationError("All versioned endpoints must be asynchronous.")
            route.endpoint = versions.versioned(route.response_model)(route.endpoint)


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
