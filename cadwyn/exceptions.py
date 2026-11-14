from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Self, TypeVar, Union

from typing_extensions import override

if TYPE_CHECKING:
    from fastapi.routing import APIRoute, APIWebSocketRoute


_ExceptionT = TypeVar("_ExceptionT", bound=BaseException)


def _restore_exception(cls: type[_ExceptionT], args: tuple[Any, ...]) -> _ExceptionT:
    exception = cls.__new__(cls, *args)
    exception.args = args
    return exception


class CadwynRenderError(Exception):
    pass


class CadwynError(Exception):
    pass


class CadwynHeadRequestValidationError(CadwynError):
    def __init__(self, errors: list[Any], body: Any, version: str) -> None:
        self.errors = errors
        self.body = body
        self.version = version
        super().__init__(
            f"We failed to migrate the request with version={self.version}. "
            "This means that there is some error in your migrations or schema structure that makes it impossible "
            "to migrate the request of that version to latest.\n"
            f"body={self.body}\n\nerrors={self.errors}"
        )

    @override
    def __reduce__(self) -> tuple[Callable[..., Self], tuple[type[Self], tuple[Any, ...]], dict[str, Any]]:
        # Pickling and copying would call the class with the message, which `__init__` doesn't take, e.g. when a worker
        # process raises the error. We restore its message, attributes, and notes without calling `__init__`.
        return _restore_exception, (type(self), self.args), self.__dict__


class LintingError(CadwynError):
    pass


class SchemaGenerationError(CadwynError):
    pass


class ModuleIsNotAvailableAsTextError(SchemaGenerationError):
    pass


class InvalidGenerationInstructionError(SchemaGenerationError):
    pass


class RouterGenerationError(CadwynError):
    pass


class RouterPathParamsModifiedError(RouterGenerationError):
    pass


class RouteResponseBySchemaConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteRequestBySchemaConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteByPathConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteAlreadyExistsError(RouterGenerationError):
    def __init__(self, *routes: "Union[APIRoute, APIWebSocketRoute]") -> None:
        self.routes = routes
        super().__init__(f"The following routes are duplicates of each other: {routes}")


class CadwynStructureError(CadwynError):
    pass


class ModuleIsNotVersionedError(ValueError):
    pass


class ImportFromStringError(CadwynError):
    pass
