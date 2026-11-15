"""The classes of Starlette and FastAPI that cadwyn-core names in annotations, as they are at runtime.

cadwyn-core imports Starlette's and FastAPI's classes only for type checking, which sees the real ones. Runtime
annotation introspection, e.g. `typing.get_type_hints()`, resolves the names in the namespaces of the modules, so the
modules get these instead:

* Starlette's classes, if Starlette is installed. Without it, the classes of the objects that Cadwyn gives instead of
  Starlette's, or stand-ins for the classes whose objects Cadwyn can't get without it.
* Stand-ins for FastAPI's classes, which cadwyn-core doesn't import. Cadwyn only gets FastAPI's objects through the
  FastAPI integration, which replaces the stand-ins with FastAPI's classes everywhere when it's imported.
"""

from typing import Any, NoReturn

from cadwyn_core.exceptions import CadwynError

# The stand-ins, with the modules and names of the classes they stand for
_STOOD_FOR: dict[type, tuple[str, str]] = {}


def _make_stand_in(module_name: str, class_name: str) -> Any:
    stand_in = type(
        class_name,
        (),
        {
            "__module__": __name__,
            "__doc__": f"Stands for `{module_name}.{class_name}` in annotations, which can't be resolved without it",
            "__slots__": (),
        },
    )
    _STOOD_FOR[stand_in] = (module_name, class_name)
    return stand_in


def get_class_stood_for(value: object) -> "tuple[str, str] | None":
    """Return the module and name of the class that a stand-in stands for, or None if it's not a stand-in"""
    return _STOOD_FOR.get(value) if isinstance(value, type) else None


class _HeadersWithoutStarlette:
    """What migrations get for headers without Starlette: whatever they do with them fails, naming Starlette"""

    __slots__ = ()

    def _fail(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise CadwynError(
            "A migration has used headers, which need Starlette, and so do the cookies of responses. Install it, "
            "e.g. with `pip install starlette`."
        )

    __getitem__ = __setitem__ = __delitem__ = __contains__ = __iter__ = __len__ = __bool__ = _fail

    def __getattr__(self, name: str) -> NoReturn:
        # Copying and pickling look up special methods, e.g. when a migration copies the request
        if name.startswith("__"):
            raise AttributeError(name)
        self._fail()


class _ResponseWithoutStarlette:
    __slots__ = ("headers", "status_code")

    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code
        self.headers = _HeadersWithoutStarlette()

    def set_cookie(self, *args: Any, **kwargs: Any) -> NoReturn:
        self.headers._fail()

    def delete_cookie(self, *args: Any, **kwargs: Any) -> NoReturn:
        self.headers._fail()


try:
    from starlette.datastructures import MutableHeaders, UploadFile
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute
except ModuleNotFoundError as exc:
    if exc.name is None or exc.name.partition(".")[0] != "starlette":  # pragma: no cover # A broken Starlette
        raise
    MutableHeaders = _HeadersWithoutStarlette
    Response = _ResponseWithoutStarlette
    UploadFile = _make_stand_in("starlette.datastructures", "UploadFile")
    Request = _make_stand_in("starlette.requests", "Request")
    BaseRoute = _make_stand_in("starlette.routing", "BaseRoute")

ModelField = _make_stand_in("fastapi._compat", "ModelField")
Dependant = _make_stand_in("fastapi.dependencies.models", "Dependant")
Depends = _make_stand_in("fastapi.params", "Depends")
APIRoute = _make_stand_in("fastapi.routing", "APIRoute")
APIRouter = _make_stand_in("fastapi.routing", "APIRouter")
SecurityBase = _make_stand_in("fastapi.security.base", "SecurityBase")
