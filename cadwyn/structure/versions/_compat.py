import dataclasses
import inspect
import re
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterator,
)
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    cast,
)

import fastapi.dependencies.utils
from fastapi._compat import (
    ModelField,
)
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from pydantic import BaseModel

if TYPE_CHECKING:
    from starlette.datastructures import FormData


def _get_omitted_body_marker() -> Any:
    """Return what FastAPI passes to `solve_dependencies` for a request without a body.

    It is `None` before FastAPI 0.151.0, and `PydanticUndefined` since then, because FastAPI 0.151.0 validates `None`
    as a JSON null body.
    """
    body_param = inspect.signature(solve_dependencies).parameters.get("body")
    if body_param is None or body_param.default is inspect.Parameter.empty:  # pragma: no cover
        raise ImportError(
            "Cadwyn does not support this version of FastAPI: `fastapi.dependencies.utils.solve_dependencies` has "
            "no default for its `body` parameter, which Cadwyn uses to pass requests without a body to FastAPI. "
            "Please, open an issue on Cadwyn's GitHub."
        )
    return body_param.default


_OMITTED_BODY: Any = _get_omitted_body_marker()
# Since FastAPI 0.158.2, FastAPI looks the dict body of a form model up under the param's aliases. We use its private
# functions for picking form models and their aliases, so that we pick exactly the same ones
_FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS = tuple(map(int, re.findall(r"\d+", fastapi.__version__)[:3])) >= (0, 158, 2)
_get_form_model = cast(
    "Callable[[ModelField], Union[type[BaseModel], None]]",
    getattr(fastapi.dependencies.utils, "_get_form_model", None),
)
_get_param_aliases = cast(
    "Callable[[ModelField], list[tuple[str, str]]]", getattr(fastapi.dependencies.utils, "_get_param_aliases", None)
)
_get_cached_model_fields = cast(
    "Callable[[type[BaseModel]], list[ModelField]]",
    getattr(fastapi.dependencies.utils, "get_cached_model_fields", None),
)
# Since FastAPI 0.159.1, FastAPI checks with this function whether the form supplies an optional form model
_is_form_model_supplied = cast(
    "Union[Callable[..., bool], None]", getattr(fastapi.dependencies.utils, "_is_form_model_supplied", None)
)
# Before FastAPI 0.160.0, a form model doesn't take the form fields that other params of the tree declare as its extra
# fields. FastAPI collects their names with this function
_get_request_param_names = cast(
    "Union[Callable[[Dependant, Any], dict[str, set[str]]], None]",
    getattr(fastapi.dependencies.utils, "_get_request_param_names", None),
)
_should_embed_body_fields = cast(
    "Callable[[list[ModelField]], bool]", getattr(fastapi.dependencies.utils, "_should_embed_body_fields", None)
)
_get_multidict_value = cast(
    "Callable[[ModelField, FormData, str], Any]", getattr(fastapi.dependencies.utils, "_get_multidict_value", None)
)
# Yields the alias and the value that FastAPI picks from a form for each field, e.g. the bytes of a `bytes` upload
_extract_form_body = cast(
    "Callable[[list[ModelField], FormData], AsyncIterator[tuple[str, Any, Union[ModelField, None]]]]",
    getattr(fastapi.dependencies.utils, "_extract_form_body", None),
)
# Since FastAPI 0.153.0, FastAPI normalizes the dict body of a union of form models with this function for each model.
# It takes the fields of the model from the dict with `_extract_form_items`
_normalize_form_model = cast(
    "Callable[..., dict[str, Any]]", getattr(fastapi.dependencies.utils, "_normalize_form_model", None)
)
# Yields the alias that FastAPI picks from a form for each field and the other keys of the form, without reading uploads
_extract_form_items = cast(
    "Callable[[list[ModelField], FormData], Iterator[tuple[str, Any, Union[ModelField, None]]]]",
    getattr(fastapi.dependencies.utils, "_extract_form_items", None),
)


def _can_be_called_with(  # pragma: no cover # FastAPI >= 0.153
    function: Union[Callable[..., Any], None], *args: Any, **kwargs: Any
) -> bool:
    try:
        inspect.signature(cast("Callable[..., Any]", function)).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return False
    return True


# We call these functions as FastAPI 0.153.0 to 0.162 do, so a change of their arguments must fail at import
if _normalize_form_model is not None and not (  # pragma: no cover
    _can_be_called_with(_normalize_form_model, None, model=None)
    and _can_be_called_with(_extract_form_items, None, None)
    and _can_be_called_with(_get_multidict_value, None, None, None)
    and _can_be_called_with(_get_cached_model_fields, None)
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: `_normalize_form_model`, `_extract_form_items`, "
        "`_get_multidict_value`, or `get_cached_model_fields` is missing from `fastapi.dependencies.utils` or takes "
        "other arguments. Cadwyn uses them to pass unions of form models to FastAPI. Please, open an issue on "
        "Cadwyn's GitHub."
    )


def _get_form_models_before_fastapi_0_161(field: ModelField) -> list[type[BaseModel]]:  # pragma: no cover
    model = _get_form_model(field)
    return [] if model is None else [model]


# Since FastAPI 0.161.0, unions of form models are form models too, and `_get_form_model` returns None for them
_get_form_models = cast(
    "Callable[[ModelField], list[type[BaseModel]]]",
    getattr(fastapi.dependencies.utils, "_get_form_models", None)
    or (_get_form_models_before_fastapi_0_161 if _get_form_model is not None else None),
)


def _get_unsupported_fastapi_functions() -> list[str]:  # pragma: no cover # FastAPI >= 0.158.2
    """Return the private FastAPI functions that are missing or that Cadwyn can't call as it does."""
    # Before FastAPI 0.160.0, FastAPI doesn't take the form fields of other params as extra fields of form models
    needs_known_param_names = (
        "known_param_names" in inspect.signature(fastapi.dependencies.utils.request_body_to_args).parameters
    )
    has_form_model_unions = getattr(fastapi.dependencies.utils, "_get_form_models", None) is not None
    arg = object()
    calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any], bool]] = [
        # The name, the function, the args, the kwargs, and whether it must be an async generator function
        ("_get_form_models" if has_form_model_unions else "_get_form_model", _get_form_models, (arg,), {}, False),
        ("_get_param_aliases", _get_param_aliases, (arg,), {}, False),
        ("get_cached_model_fields", _get_cached_model_fields, (arg,), {}, False),
        ("_should_embed_body_fields", _should_embed_body_fields, (arg,), {}, False),
        ("_extract_form_body", _extract_form_body, (arg, arg), {}, True),
        ("_extract_form_items", _extract_form_items, (arg, arg), {}, False),
        ("_get_multidict_value", _get_multidict_value, (arg, arg, arg), {}, False),
    ]
    if not has_form_model_unions:
        calls.append(("_get_form_model", _get_form_model, (arg,), {}, False))
    if _is_form_model_supplied is not None:
        calls.append(("_is_form_model_supplied", _is_form_model_supplied, (arg, arg), {}, False))
        if needs_known_param_names:
            calls.append(("_is_form_model_supplied", _is_form_model_supplied, (arg, arg, arg), {}, False))
    if needs_known_param_names:
        calls.append(("_get_request_param_names", _get_request_param_names, (arg, arg), {}, False))
    if has_form_model_unions:
        calls.append(("_normalize_form_model", _normalize_form_model, (arg,), {"model": arg}, False))
    return [
        name
        for name, function, args, kwargs, is_async_generator in calls
        if function is None
        or not _can_be_called_with(function, *args, **kwargs)
        or inspect.isasyncgenfunction(function) is not is_async_generator
    ]


if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS and (  # pragma: no cover
    _unsupported_fastapi_functions := _get_unsupported_fastapi_functions()
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: "
        f"{', '.join(f'`{name}`' for name in dict.fromkeys(_unsupported_fastapi_functions))} of "
        "`fastapi.dependencies.utils` is missing or takes other arguments. Cadwyn uses these functions to pass form "
        "models to FastAPI. Please, open an issue on Cadwyn's GitHub."
    )
if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS and not (  # pragma: no cover
    dataclasses.is_dataclass(Dependant)
    and all(isinstance(getattr(ModelField, name, None), property) for name in ("alias", "validation_alias"))
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: `Dependant` is not a dataclass, or `ModelField` stores its "
        "aliases instead of taking them from its `field_info`. Cadwyn copies them with other aliases to pass form "
        "models to FastAPI. Please, open an issue on Cadwyn's GitHub."
    )
# The key that FastAPI looks up the dict body of the head endpoint's form model under if another form param has its
# alias, e.g. a form field that only an override declares. No form field has this key, so the param keeps its value
_FORM_MODEL_BODY_KEY = "\x00cadwyn_form_model_body"
