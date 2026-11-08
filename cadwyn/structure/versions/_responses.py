from typing import (
    Any,
)

from fastapi._compat import (
    ModelField,
)
from pydantic import BaseModel

from ._aliases import _get_restorer_spec, _is_dumped_from_models, _rescue_body


def _prepare_response_content(
    res: Any,
    *,
    exclude_unset: bool,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
) -> Any:
    """Serialize Pydantic models to dicts for response processing.

    It is much easier to alter dicts and lists than Pydantic models in request/response migrations
    """
    if isinstance(res, BaseModel):
        return res.model_dump(
            by_alias=True,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
    elif isinstance(res, list):
        return [
            _prepare_response_content(
                item,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
            for item in res
        ]
    elif isinstance(res, dict):
        return {
            k: _prepare_response_content(
                v,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
            for k, v in res.items()
        }
    return res


def _rescue_response(response_field: ModelField, head_response_model: object, content: Any, data: Any) -> Any:
    """Rescue the migrated `_prepare_response_content` of `content` if it surely fails validation due to dumped keys.

    That's the case when a required field, or a model that forbids extra keys, gets a dumped key instead of
    a validation key. We restore its validation aliases and let FastAPI validate the restored response, so its
    validators run once, like in FastAPI without Cadwyn. The response fails as it is anyway, or silently gets
    the defaults that the core schemas of the types of some fields give them (see `_get_failing_keys`), so if
    the restored response fails, FastAPI raises its errors. Otherwise, FastAPI validates the response as it is,
    as it always did, and responses that pass stay exactly as they are, even if a field gets its default instead
    of its value.
    """
    restored = _rescue_body(response_field, data)
    if restored is data:
        return data
    # We only restore the dumps of the models of the head response model that the endpoint returned. FastAPI validates
    # the dicts that the endpoint returns as they are, and the dump of another model can have the dumped keys of
    # a response model with values that it doesn't accept.
    head_spec = _get_restorer_spec(head_response_model)
    if head_spec is None or not _is_dumped_from_models(content, head_spec):
        return data
    return restored
