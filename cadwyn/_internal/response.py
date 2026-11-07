from collections.abc import Callable, Coroutine
from copy import copy
from types import new_class
from typing import Any, cast

from fastapi import Request, Response
from fastapi._compat import ModelField
from fastapi.routing import APIRoute, _effective_route_context_var, _EffectiveRouteContext
from typing_extensions import override


class _HeadResponseRoute(APIRoute):
    """Use the HEAD response field at runtime while keeping the versioned field for OpenAPI."""

    _head_response_field: ModelField

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        context = _effective_route_context_var.get()
        if context is not None and context.original_route is self:
            context = copy(context)
        else:
            context = _EffectiveRouteContext(original_route=self)
            for name in vars(context):
                if name != "original_route" and hasattr(self, name):
                    setattr(context, name, getattr(self, name))
        context.response_field = self._head_response_field
        token = _effective_route_context_var.set(context)
        try:
            return super().get_route_handler()
        finally:
            _effective_route_context_var.reset(token)


def _use_head_response_model(route: APIRoute, field: ModelField) -> None:
    route.__class__ = new_class(
        f"Cadwyn{type(route).__name__}",
        (_HeadResponseRoute, type(route)),  # ty: ignore[unsupported-dynamic-base]
    )
    cast("_HeadResponseRoute", route)._head_response_field = field
