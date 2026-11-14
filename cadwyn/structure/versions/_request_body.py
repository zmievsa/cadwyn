import email.message
import json
from typing import (
    Any,
    Union,
)

from fastapi import HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi._compat import (
    ModelField,
)
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import _get_scope_effective_route_context
from pydantic_core import (
    PydanticUndefined,
)

from cadwyn.exceptions import (
    CadwynError,
)


def _get_strict_content_type(request: FastapiRequest) -> bool:
    """Return the `strict_content_type` with which FastAPI has read the body of the request.

    We resolve it like FastAPI does for the route that serves the request, which isn't the versioned route that
    Cadwyn has generated. A route in a tree of included routers gets its handler from its effective route context,
    which merges the values of the route and the routers that include it, and any other route has its own value.
    FastAPI's request handler takes the value of a default placeholder.
    """
    served_route = request.scope["route"]
    effective_route_context = _get_scope_effective_route_context(request.scope)
    if effective_route_context is not None and effective_route_context.original_route is served_route:
        strict_content_type = effective_route_context.strict_content_type
    else:
        strict_content_type = served_route.strict_content_type
    # E.g. a router that a mounted router includes keeps the placeholder, because no app gives it a bool
    if isinstance(strict_content_type, DefaultPlaceholder):
        strict_content_type = strict_content_type.value
    if not isinstance(strict_content_type, bool):  # pragma: no cover # FastAPI only has bools here
        raise CadwynError(
            f"Cadwyn can't tell how FastAPI has read the request body: the route that serves the request has "
            f"strict_content_type={strict_content_type!r} instead of True or False. Please, open an issue on Cadwyn's "
            "GitHub."
        )
    return strict_content_type


# We use this instead of `.body()` to automatically guess body type and load the correct body, even if it's a form
async def _get_body(
    request: FastapiRequest, body_field: Union[ModelField, None], *, strict_content_type: bool
):  # pragma: no cover # This is from FastAPI
    is_body_form = body_field and isinstance(body_field.field_info, params.Form)
    try:
        body: Any = None
        if body_field:
            if is_body_form:
                # FastAPI has already parsed this form, so this returns its cached form. We close its files after the
                # response (`_UploadsToClose`), so background tasks can still read them
                body = await request.form()
            else:
                body_bytes = await request.body()
                if body_bytes:
                    json_body: Any = PydanticUndefined
                    content_type_value = request.headers.get("content-type")
                    if not content_type_value:
                        # Like FastAPI, we parse a body without a content type as JSON only if it's not strict
                        if not strict_content_type:
                            json_body = await request.json()
                    else:
                        message = email.message.Message()
                        message["content-type"] = content_type_value
                        if message.get_content_maintype() == "application":
                            subtype = message.get_content_subtype()
                            if subtype == "json" or subtype.endswith("+json"):
                                json_body = await request.json()
                    if json_body != PydanticUndefined:
                        body = json_body
                    else:
                        body = body_bytes
    except json.JSONDecodeError as e:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", e.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": e.msg},
                },
            ],
            body=e.doc,
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="There was an error parsing the body") from e
    return body
