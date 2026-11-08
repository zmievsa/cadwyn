import copy
import dataclasses
import functools
import weakref
from collections import OrderedDict, defaultdict, deque
from collections.abc import (
    Mapping,
)
from enum import Enum
from types import MappingProxyType
from typing import (
    Annotated,
    Any,
    NamedTuple,
    Union,
    get_args,
    get_origin,
)

from fastapi import Request as FastapiRequest
from fastapi import params
from fastapi._compat import (
    ModelField,
)
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import AliasChoices, AliasPath, BaseModel, BeforeValidator
from pydantic_core import (
    PydanticUndefined,
)
from starlette.datastructures import FormData

from cadwyn._utils import (
    lenient_issubclass,
)
from cadwyn.exceptions import (
    CadwynError,
)

from ._compat import (
    _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    _FORM_MODEL_BODY_KEY,
    _extract_form_body,
    _extract_form_items,
    _get_cached_model_fields,
    _get_form_models,
    _get_multidict_value,
    _get_param_aliases,
    _get_request_param_names,
    _is_form_model_supplied,
    _normalize_form_model,
    _should_embed_body_fields,
)
from ._dependencies import _iter_dependants_that_fastapi_solves
from ._request_body import _dump_request_body
from ._uploads import _get_uploads_to_close


async def _fastapi_got_no_body(request: FastapiRequest, route: APIRoute, *, embed_body_fields: bool) -> bool:
    """Return whether FastAPI has solved the versioned route without a body, given that Cadwyn sees its body as None.

    Like FastAPI, we only look at the raw request: FastAPI passes a body if the route has a body field and the request
    has a form or non-empty bytes.
    """
    if route.body_field is None:
        return True
    is_form = isinstance(route.body_field.field_info, params.Form)
    # FastAPI always passes forms. It has already read other bodies, so this returns the cached bytes
    if not is_form and not await request.body():
        return True
    # Embedded body fields get None only if `_get_body` has parsed a body without a content type as JSON null, which
    # it does only with `strict_content_type=False`.
    # FastAPI rejects a JSON null for them, so it has got these raw bytes instead, which it ignores like no body
    return embed_body_fields and not is_form


class _FormModelOmission(Enum):
    """Whether FastAPI has given the form model body param its default because the form doesn't supply it."""

    NOT_OMITTED = "not_omitted"
    # Every body param of the tree is an optional form model that the form doesn't supply
    OMITTED = "omitted"
    # Other body params of the tree, e.g. the form fields of a dependency override, can read the form
    OMITTED_NEXT_TO_OTHER_FORM_PARAMS = "omitted_next_to_other_form_params"


async def _get_form_model_omission(request: FastapiRequest, route: APIRoute) -> _FormModelOmission:
    """Return whether FastAPI has given the form model body param its default because the form doesn't supply it.

    Since FastAPI 0.158.2, FastAPI does so for an optional form model if the form has none of its fields. Cadwyn sees
    that default as None, and the head would reject None as a form body, so we omit the body for the head instead.
    """
    if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS:  # pragma: no cover # FastAPI >= 0.158.2
        return await _get_omission_of_optional_form_model(request, route)
    return _FormModelOmission.NOT_OMITTED


async def _get_omission_of_optional_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest, route: APIRoute
) -> _FormModelOmission:
    """Return whether the body param is a form model that FastAPI has given its default, and what else reads the form.

    The body param is the only body param of the endpoint, whatever its name or alias. FastAPI only skips the form
    models (or unions of them) that can be None if the form doesn't supply any of them. Dependencies and their
    overrides can have such form models too.
    """
    body_param = route.dependant.body_params[0]
    if (
        not _get_form_models(body_param)
        or body_param.field_info.is_required()
        or type(None) not in get_args(body_param.field_info.annotation)
    ):
        return _FormModelOmission.NOT_OMITTED
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    known_param_names = _get_known_form_param_names(route.dependant, route)
    if any(_form_supplies_form_model(model, form, known_param_names) for model in _get_form_models(body_param)):
        return _FormModelOmission.NOT_OMITTED
    for dependant in _iter_dependants_that_fastapi_solves(route.dependant, route):
        for field in dependant.body_params:
            field_models = _get_form_models(field)
            if (
                not field_models
                or type(None) not in get_args(field.field_info.annotation)
                or any(_form_supplies_form_model(model, form, known_param_names) for model in field_models)
            ):
                return _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS
    return _FormModelOmission.OMITTED


async def _get_form_for_omitted_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest,
    head_dependant: Dependant,
    head_route: APIRoute,
    migrated_body: Any,
    *,
    embed_body_fields: bool,
) -> FormData:
    """Return the form for the head when its form model is omitted, but other params of the tree can read the form.

    Either no migration has set the body, or the migration has set a dict without fields of the form model, so the
    head gives the form model its default, and every other param the value that FastAPI gives it from the form that the
    client would have sent as the migrated request: the request's form with the keys of the migrated body, where a
    collection is a form item per value. That is only so if this form doesn't supply the form model in the head either.
    """
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    # We pass the form's uploads to the head, so we close them after the request like those of other forms
    _get_uploads_to_close(request).add(form.multi_items())
    if migrated_body is not None:
        virtual_form = await _get_virtual_form(request, migrated_body, None, is_migrated=True)
        items: list[tuple[str, Any]] = []
        for key in virtual_form.keys:
            values = (
                _get_form_values_of_migrated_value(
                    head_dependant, head_route, key, migrated_body[key], embed_body_fields=embed_body_fields
                )
                if key in migrated_body
                else virtual_form.form.getlist(key)
            )
            items.extend((key, value) for value in values)
        form = FormData(items)
    body_param = head_dependant.body_params[0]
    known_param_names = _get_known_form_param_names(head_dependant, head_route)
    if any(_form_supplies_form_model(model, form, known_param_names) for model in _get_form_models(body_param)):
        raise CadwynError(
            f"Cadwyn can't pass the form of this request to the latest version: the request has none of the fields of "
            f"the form model `{body_param.name}` in its version, so the form model gets its default, but the form has "
            "fields of the form model in the latest version, and other params of the endpoint, of its dependencies, "
            "or of their overrides read the form as well. Give these params other names or aliases than the fields "
            "of the form model."
        )
    return form


def _get_form_values_of_migrated_value(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, key: str, value: Any, *, embed_body_fields: bool
) -> list[Any]:
    """Return the form values that the client would send for the value that a migration has set under the key.

    A client sends a collection as a form item per value, and FastAPI gives a param that takes a collection all of
    them. A form can't have a dict or a collection in a collection, so we fail for them instead of passing a value
    that no client could send.
    """
    values = list(value) if isinstance(value, _FORM_VALUE_COLLECTIONS) else [value]
    if not any(isinstance(item, (Mapping, *_FORM_VALUE_COLLECTIONS)) for item in values):
        return values
    params = _get_body_lookups(
        head_dependant,
        head_route,
        set(_get_form_models(head_dependant.body_params[0])),
        embed_body_fields=embed_body_fields,
    )
    other_params = [param for param in params if param.kind is not _BodyParamKind.SAME_FORM_MODEL]
    reader = next(
        (
            param
            for param in other_params
            if key in param.aliases
            or (
                param.kind is _BodyParamKind.OTHER_FORM_MODEL
                and key in _get_field_aliases_of_models(set(_get_form_models(param.field)))
            )
        ),
        (other_params or params)[0],
    )
    raise _cant_pass_form_error(
        reader,
        f"the migration has set `{key}` to a value that can't be sent as form items: a dict or a collection in a "
        "collection. Set it to a single value or to a collection of single values",
    )


def _migrated_body_supplies_omitted_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, body: Any, *, embed_body_fields: bool
) -> bool:
    """Return whether the body that a migration has set for an omitted form model supplies the form model in the head.

    Like FastAPI does for a form, the form model stays omitted if the dict body has none of its fields. Other params
    read the form too, so we can only pass a dict body on.
    """
    # We only know how the lookups of these dicts behave, while a subclass can override them
    if type(body) not in (dict, OrderedDict, defaultdict):
        params = _get_body_lookups(
            head_dependant,
            head_route,
            set(_get_form_models(head_dependant.body_params[0])),
            embed_body_fields=embed_body_fields,
        )
        raise _cant_pass_form_error(
            params[0], "the body that the migration has set for the omitted form model isn't a dict"
        )
    known_param_names = _get_known_form_param_names(head_dependant, head_route)
    return any(
        _form_supplies_form_model(model, FormData(body), known_param_names)
        for model in _get_form_models(head_dependant.body_params[0])
    )


def _form_supplies_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    model: type[BaseModel], form: FormData, known_param_names: Union[set[str], None] = None
) -> bool:
    """Return whether FastAPI validates the optional form model from the form instead of giving it its default.

    Before FastAPI 0.160.0, FastAPI doesn't take the keys of `known_param_names` as extra fields of the form model.
    """
    if _is_form_model_supplied is not None:
        if known_param_names is None:
            return _is_form_model_supplied(model, form)
        return _is_form_model_supplied(model, form, known_param_names)
    # Before FastAPI 0.159.1, FastAPI checks the same inline
    own_names = {alias for field in _get_cached_model_fields(model) for alias, _ in _get_param_aliases(field)}
    return bool(
        own_names.intersection(form)
        or (model.model_config.get("extra") in ("allow", "forbid") and form.keys() - (known_param_names or own_names))
    )


def _get_known_form_param_names(  # pragma: no cover # FastAPI >= 0.158.2
    dependant: Dependant, route: APIRoute
) -> Union[set[str], None]:
    """Return the names of the form params that FastAPI doesn't take as extra fields of form models, if it has them.

    Before FastAPI 0.160.0, FastAPI collects them from the tree that it solves, with the overrides of dependencies.
    """
    if _get_request_param_names is None:
        return None
    return _get_request_param_names(dependant, route.dependency_overrides_provider)["form"]


def _body_is_in_kwargs(route: APIRoute, body_field_alias: str, *, embed_body_fields: bool) -> bool:
    """Return whether we take the body from the kwarg of the body param instead of reading it from the request.

    When the body is embedded (wrapped into a separate field with the "body" alias, e.g. for files and forms), no
    kwarg holds the whole body: a kwarg named "body" is a param of its own (a file, a query param, a dependency, etc.),
    so we read the body from the request. Otherwise, the kwarg of the body param holds the body. We take it from
    there if the body's alias is the body param's name or the name of a dependency, and read the request otherwise.
    """
    if embed_body_fields:
        return False
    return body_field_alias == route.dependant.body_params[0].name or any(
        dependency.name == body_field_alias for dependency in route.dependant.dependencies
    )


class _FormModelLookup(NamedTuple):
    """The dependant to solve for the head, and how to pass the dict body of its form model to FastAPI."""

    dependant: Dependant
    # The keys to put the dict body of the form model under
    aliases: tuple[str, ...] = ()
    # The form models that read the dict body of the head's union of form models, if FastAPI validates the models of
    # the union from it as if it were form data
    form_union_readers: tuple["_FormModelReader", ...] = ()
    # Whether the union is a form model, as it is since FastAPI 0.161.0
    form_union_is_form_model: bool = False
    # The values that FastAPI gives other params for the migrated request, e.g. the form fields that only a dependency
    # override declares, under the keys that these params look them up under in the dict body
    form_values: Mapping[str, Any] = MappingProxyType({})
    # Whether the other keys of the body are next to the form model, as in the last release
    spreads_body: bool = True


class _FormModelReader(NamedTuple):
    """A form model that reads the dict body of a union of form models."""

    model: type[BaseModel]
    # Whether FastAPI validates the model from the dict as if it were form data, as it does for a model of a union
    is_validated_as_form_data: bool


async def _get_form_model_lookup(
    request: FastapiRequest,
    head_dependant: Dependant,
    head_route: APIRoute,
    body: Any,
    dumped_body: Union[BaseModel, None],
    *,
    embed_body_fields: bool,
    is_migrated: bool,
) -> _FormModelLookup:
    """Return the dependant to solve for the head, and how to pass the dict body of its form model to FastAPI.

    Before FastAPI 0.158.2, FastAPI validates the dict body of a single form model as it is. Since then, the endpoint
    and every dependency that shares the form model look it up under their own aliases instead, and the other body
    params, e.g. the form fields and form models that only a dependency override declares, look their values up in the
    same dict.

    Every such param must get what FastAPI would give it if the client had sent the migrated request, so we work out
    that value for each of them like FastAPI does (see `_get_virtual_form`). We pass the body as the last release did
    if that gives every param exactly its value. Otherwise, we pass these values under their keys, and solve a copy of
    the head's dependant whose form model looks the body up under a key that no form param has. If neither does, or we
    can't work out the value of a param, e.g. of a param that takes the whole body, we fail instead of giving a param
    another value.
    """
    body_field = head_dependant.body_params[0]
    form_models = _get_form_models(body_field) if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS else []
    form_union_readers = _get_form_union_readers(
        head_dependant, head_route, is_form_model=bool(form_models), embed_body_fields=embed_body_fields
    )
    if form_models:  # pragma: no cover # FastAPI >= 0.158.2
        params = _get_body_lookups(head_dependant, head_route, set(form_models), embed_body_fields=embed_body_fields)
        lookup = _FormModelLookup(
            head_dependant,
            _get_form_model_aliases(params, own_key=False),
            form_union_readers,
            form_union_is_form_model=True,
        )
        if all(param.kind is _BodyParamKind.SAME_FORM_MODEL for param in params):
            return lookup
        virtual_form = await _get_virtual_form(request, body, dumped_body, is_migrated=is_migrated)
        expected_values = await _get_expected_values_of_body_params(
            params, virtual_form, _get_known_form_param_names(head_dependant, head_route)
        )
        for index, param in enumerate(params):
            if expected_values.get(index) is _FormValueMarker.UNKNOWN:
                raise _cant_pass_form_error(param, _describe_unknown_value(param))
        if _get_param_with_other_value(lookup, params, expected_values, body, own_key=False) is None:
            return lookup
        form_values: dict[str, Any] = {}
        for expected_value in expected_values.values():
            if isinstance(expected_value, _ExpectedFormValue):
                form_values.setdefault(expected_value.alias, expected_value.value)
        lookup = _FormModelLookup(
            _with_form_models_under_own_key(head_dependant),
            _get_form_model_aliases(params, own_key=True),
            form_union_readers,
            form_union_is_form_model=True,
            form_values=form_values,
            spreads_body=False,
        )
        mismatching_param = _get_param_with_other_value(lookup, params, expected_values, body, own_key=True)
        if mismatching_param is None:
            return lookup
        raise _cant_pass_form_error(
            mismatching_param,
            "it has the same key as a form model or another param. Give them other names or aliases",
        )
    return _FormModelLookup(head_dependant, form_union_readers=form_union_readers)


def _get_form_union_readers(
    head_dependant: Dependant, head_route: APIRoute, *, is_form_model: bool, embed_body_fields: bool
) -> tuple[_FormModelReader, ...]:
    """Return the form models that read the dict body of the head's union of form models.

    We return them only if FastAPI validates the models of the union from the dict as if it were form data. Before
    FastAPI 0.161.0, the union isn't a form model, so only a body param that isn't embedded gets the whole
    body. Since then, every form model of the tree that FastAPI solves reads the body under its param's alias.
    """
    body_field = head_dependant.body_params[0]
    if not _get_models_validated_as_form_data(body_field):
        return ()
    if is_form_model:  # pragma: no cover # FastAPI >= 0.161
        fields = [
            field
            for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
            for field in dependant.body_params
            if _get_form_models(field)
        ]
    elif embed_body_fields:  # pragma: no cover # FastAPI >= 0.153
        return ()
    else:  # pragma: no cover # FastAPI >= 0.153
        fields = [body_field]
    readers: dict[_FormModelReader, None] = {}  # pragma: no cover # FastAPI >= 0.153
    for field in fields:  # pragma: no cover # FastAPI >= 0.153
        models = _get_models_validated_as_form_data(field)
        for model in models or _get_form_models(field):
            readers[_FormModelReader(model, is_validated_as_form_data=bool(models))] = None
    return tuple(readers)  # pragma: no cover # FastAPI >= 0.153


def _get_models_validated_as_form_data(field: ModelField) -> list[type[BaseModel]]:
    """Return the models of a union of form models that FastAPI validates from a dict as if it were form data.

    Since FastAPI 0.153.0, FastAPI adds a validator that does this to each of them.
    """
    if _normalize_form_model is None:
        return []
    return [  # pragma: no cover # FastAPI >= 0.153
        validator.func.keywords["model"]
        for arg in get_args(field.field_info.annotation)
        if get_origin(arg) is Annotated
        for validator in arg.__metadata__
        if isinstance(validator, BeforeValidator)
        and isinstance(validator.func, functools.partial)
        and validator.func.func is _normalize_form_model
        and lenient_issubclass(validator.func.keywords.get("model"), BaseModel)
    ]


def _cant_pass_form_error(  # pragma: no cover # FastAPI >= 0.158.2
    param: "_BodyParamLookup", reason: str
) -> CadwynError:
    return CadwynError(
        "Cadwyn can't pass the form of this request to the latest version: FastAPI looks the form models and the "
        f"other form params up in the same dict body, so the param {_describe_body_param(param)} would get another "
        f"value than the one that FastAPI gives it for the migrated request, because {reason}."
    )


def _describe_unknown_value(param: "_BodyParamLookup") -> str:  # pragma: no cover # FastAPI >= 0.158.2
    if param.kind is _BodyParamKind.WHOLE_BODY:
        return (
            "it takes the whole body, which has the form model's data under the key of the form model. Read the form "
            "from the `Request` in that dependency instead"
        )
    return (
        "it reads a key of the form that the form model has taken, and Cadwyn can't tell its value after the request "
        "migrations, e.g. because the body of the form model isn't a dict, the form model has taken the key under "
        "another name (e.g. through `AliasChoices`), or a migration has put a collection (e.g. a list, a set, or a "
        "tuple) into the body that a union of form models gets. Give the param another name or alias"
    )


def _get_omitted_form_model_lookup(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, body: Any, *, embed_body_fields: bool
) -> _FormModelLookup:
    """Return how to pass the body that a migration has set for an omitted optional form model to the head.

    We put a dict body under the aliases of the params that have the same form models as the endpoint's body param,
    e.g. those of the dependencies and overrides that share it, so that other form models still get their defaults.
    We look at the tree that FastAPI solves for the head, which can have dependencies that the request's version lacks.
    In every other case we pass the body as it is, like the last release did: if the body isn't a dict, an
    `OrderedDict`, or a `defaultdict`, if it has a key with such an alias (e.g. a migration has put the form model's
    data under it), if another param of the tree has such an alias or gets the whole body, if, like a form, it supplies
    none of the form models, so that the param gets its default as FastAPI gives it, if a form model that allows or
    forbids extra fields would take a key of another param as an extra field, which FastAPI doesn't do for a form
    before 0.160.0, or if FastAPI would validate a model of a union from changed values.
    """
    # We only know how the lookups of these dicts behave, while a subclass can override them
    if type(body) not in (dict, OrderedDict, defaultdict):
        return _FormModelLookup(head_dependant)
    form_models = set(_get_form_models(head_dependant.body_params[0]))
    aliases: dict[str, None] = {}
    other_aliases: set[str] = set()
    # The keys that the other params of the tree read, including the fields of their form models
    other_keys: set[str] = set()
    for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route):
        # Like FastAPI, we give a single body param that isn't embedded the whole body
        is_embedded = embed_body_fields or _should_embed_body_fields(dependant.body_params)
        for field in dependant.body_params:
            field_form_models = set(_get_form_models(field))
            if not field_form_models and not is_embedded:
                return _FormModelLookup(head_dependant)
            field_aliases = [alias for alias, _ in _get_param_aliases(field)]
            if field_form_models == form_models:
                aliases.update(dict.fromkeys(field_aliases))
            else:
                other_aliases.update(field_aliases)
                other_keys.update(field_aliases, _get_field_aliases_of_models(field_form_models))
    if (
        not aliases.keys().isdisjoint(body)
        or not aliases.keys().isdisjoint(other_aliases)
        or not any(_form_supplies_form_model(model, FormData(body)) for model in form_models)
        or (
            any(model.model_config.get("extra") in ("allow", "forbid") for model in form_models)
            and not (other_keys - _get_field_aliases_of_models(form_models)).isdisjoint(body)
        )
    ):
        return _FormModelLookup(head_dependant)
    lookup = _FormModelLookup(head_dependant, tuple(aliases))
    if len(form_models) > 1:
        readers = tuple(_FormModelReader(model, is_validated_as_form_data=True) for model in form_models)
        lookup = lookup._replace(form_union_readers=readers, form_union_is_form_model=True)
        try:
            form_model_body = _get_form_union_body(body, readers)
        except _FormUnionValueError:
            return _FormModelLookup(head_dependant)
        if _form_model_union_changes_body(form_model_body, form_models):
            return _FormModelLookup(head_dependant)
    return lookup


def _get_field_aliases_of_models(  # pragma: no cover # FastAPI >= 0.158.2
    models: set[type[BaseModel]],
) -> set[str]:
    return {
        alias for model in models for field in _get_cached_model_fields(model) for alias, _ in _get_param_aliases(field)
    }


def _form_model_union_changes_body(  # pragma: no cover # FastAPI >= 0.161
    body: dict[str, Any], form_models: set[type[BaseModel]]
) -> bool:
    """Return whether FastAPI would validate a model of the union from another dict than the body.

    FastAPI validates each model of the union from the body as if it were form data, e.g. a field that takes a single
    value gets one value of a list, any other key gets the only value of a list as it is, and a field with an empty
    string gets its default. A model can read any key, e.g. through an `AliasPath`, a root model, or a validator,
    so we compare the whole dict.
    """
    return any(_normalize_form_model(body, model=model) != body for model in form_models)


def _with_form_models_under_own_key(dependant: Dependant) -> Dependant:  # pragma: no cover # FastAPI >= 0.158.2
    """Return a copy of the dependant whose own form models FastAPI looks up under `_FORM_MODEL_BODY_KEY`.

    If the app has dependency overrides, FastAPI builds the dependants of all dependencies again while solving, so the
    form models of dependencies keep their aliases.
    """
    return dataclasses.replace(
        dependant,
        body_params=[
            _with_form_model_under_own_key(field) if _get_form_models(field) else field
            for field in dependant.body_params
        ],
    )


def _with_form_model_under_own_key(field: ModelField) -> ModelField:  # pragma: no cover # FastAPI >= 0.158.2
    # The field keeps its type adapter, so it validates the body exactly like the original
    field_info = copy.copy(field.field_info)
    field_info.alias = _FORM_MODEL_BODY_KEY
    field_info.validation_alias = None
    field = copy.copy(field)
    field.field_info = field_info
    return field


class _BodyParamKind(Enum):  # pragma: no cover # FastAPI >= 0.158.2
    # A form model param with the same form models as the endpoint's body param, which gets the body
    SAME_FORM_MODEL = "same_form_model"
    # A form model param with other form models
    OTHER_FORM_MODEL = "other_form_model"
    # A param that FastAPI looks up in the body by its aliases, e.g. a form field or `Body(embed=True)`
    FORM_FIELD = "form_field"
    # A param that gets the whole body, e.g. the only `Body()` param of a dependency override
    WHOLE_BODY = "whole_body"


class _BodyParamLookup(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """A body param of the tree that FastAPI solves for the head, and the aliases that FastAPI looks it up under."""

    field: ModelField
    aliases: list[str]
    kind: _BodyParamKind
    # The endpoint, dependency, or override that declares the param
    call: Any
    # Whether the head's dependant declares it itself, so that a copy of the dependant can change its alias
    is_own: bool


def _get_body_lookups(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, form_models: set[type[BaseModel]], *, embed_body_fields: bool
) -> list[_BodyParamLookup]:
    """Return how the tree that FastAPI solves for the head reads a dict body."""
    lookups: list[_BodyParamLookup] = []
    for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route):
        # Like FastAPI, we give a single body param that isn't embedded the whole body
        is_embedded = embed_body_fields or _should_embed_body_fields(dependant.body_params)
        for field in dependant.body_params:
            field_form_models = _get_form_models(field)
            if field_form_models:
                kind = (
                    _BodyParamKind.SAME_FORM_MODEL
                    if set(field_form_models) == form_models
                    else _BodyParamKind.OTHER_FORM_MODEL
                )
            else:
                kind = _BodyParamKind.FORM_FIELD if is_embedded else _BodyParamKind.WHOLE_BODY
            aliases = [alias for alias, _ in _get_param_aliases(field)]
            lookups.append(_BodyParamLookup(field, aliases, kind, dependant.call, dependant is head_dependant))
    return lookups


def _get_lookup_aliases(
    param: _BodyParamLookup, *, own_key: bool
) -> list[str]:  # pragma: no cover # FastAPI >= 0.158.2
    """Return the aliases that FastAPI looks the param up under, given whether the own form models use their own key."""
    if own_key and param.is_own and param.kind in (_BodyParamKind.SAME_FORM_MODEL, _BodyParamKind.OTHER_FORM_MODEL):
        return [_FORM_MODEL_BODY_KEY]
    return param.aliases


def _get_form_model_aliases(  # pragma: no cover # FastAPI >= 0.158.2
    params: list[_BodyParamLookup], *, own_key: bool
) -> tuple[str, ...]:
    """Return the keys to put the body under.

    The last release put it under the aliases of every form model. In the copy of the dependant, only the form models
    that share the endpoint's form models get it, and the other ones get their own values.
    """
    kinds = (
        (_BodyParamKind.SAME_FORM_MODEL,)
        if own_key
        else (_BodyParamKind.SAME_FORM_MODEL, _BodyParamKind.OTHER_FORM_MODEL)
    )
    return tuple(
        dict.fromkeys(
            alias for param in params if param.kind in kinds for alias in _get_lookup_aliases(param, own_key=own_key)
        )
    )


class _ExpectedFormValue(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """The value that a param gets in the head, and the key that it gets it under."""

    alias: str
    value: Any
    # Whether the value is FastAPI's from the form, so it is a string, an upload, bytes, False, or a list of them
    is_from_form: bool


class _FormValueMarker(Enum):  # pragma: no cover # FastAPI >= 0.158.2
    # The param gets its default because FastAPI finds none of its aliases
    DEFAULT = "default"
    # The migrated request doesn't have the key, e.g. because a migration has removed it from the form model's body
    ABSENT = "absent"
    # We can't know the value of the key in the migrated request, or the value of the param
    UNKNOWN = "unknown"


class _VirtualForm(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """The form that the client would have sent as the migrated request."""

    # The keys whose values are the client's, which FastAPI reads from the form
    form: FormData
    # The values of the other keys: those of the migrated body, or markers
    overlay: dict[str, Union[_ExpectedFormValue, _FormValueMarker]]
    # Every key, in the order of the form, with the keys that only the migrated body has after them
    keys: list[str]


async def _get_virtual_form(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest, body: Any, dumped_body: Union[BaseModel, None], *, is_migrated: bool
) -> _VirtualForm:
    """Return the form that the client would have sent as the migrated request.

    If no request migration has run, it is the request's form. Otherwise, every key of the migrated body has its value
    from the body, and so do the keys that the request's form model has taken from the form: FastAPI's pick of each of
    its fields (e.g. the first alias of `AliasChoices` that the form has) and its extra fields. An omitted form model
    has taken no keys, so every original form key that the migration leaves out keeps its value. A taken key that the
    body no longer has is absent if the dump of the form model had it, and unknown otherwise, because the form model
    has dumped its value under another key. Every other key keeps the value of the form.
    """
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    form_keys = list(dict.fromkeys(form.keys()))
    if not is_migrated:
        return _VirtualForm(form, {}, form_keys)
    taken_keys: set[str] = set()
    if dumped_body is not None:
        taken_keys.update(
            alias
            for alias, _, field in _extract_form_items(_get_cached_model_fields(type(dumped_body)), form)
            if field is not None
        )
        taken_keys.update(dumped_body.__pydantic_extra__ or {})
    overlay: dict[str, Union[_ExpectedFormValue, _FormValueMarker]] = {}
    if isinstance(body, dict):
        # `_embed_form_model` puts the same keys of the body next to the form model
        overlay = {key: _ExpectedFormValue(key, value, is_from_form=False) for key, value in {**body}.items()}
    # The same dump as the body's, so that we know which keys the client's version of the body had
    dump = _dump_request_body(dumped_body) if dumped_body is not None else {}
    for key in taken_keys:
        if key not in overlay:
            overlay[key] = (
                _FormValueMarker.ABSENT
                if isinstance(body, dict) and isinstance(dump, dict) and key in dump
                else _FormValueMarker.UNKNOWN
            )
    return _VirtualForm(
        FormData([(key, value) for key, value in form.multi_items() if key not in overlay]),
        overlay,
        [*form_keys, *(key for key in overlay if key not in form)],
    )


async def _get_expected_values_of_body_params(  # pragma: no cover # FastAPI >= 0.158.2
    params: list[_BodyParamLookup], virtual_form: _VirtualForm, known_param_names: Union[set[str], None]
) -> dict[int, Union[_ExpectedFormValue, _FormValueMarker]]:
    """Return what FastAPI would give each param other than the endpoint's form models for the migrated request.

    FastAPI solves a cached dependency once, so we work out its values once too, which reads its uploads once.
    """
    expected_values: dict[int, Union[_ExpectedFormValue, _FormValueMarker]] = {}
    picked_values: dict[tuple[int, str], Union[_ExpectedFormValue, _FormValueMarker]] = {}
    for index, param in enumerate(params):
        if param.kind is _BodyParamKind.SAME_FORM_MODEL:
            continue
        key = (id(param.call), param.field.name)
        if key not in picked_values:
            if param.kind is _BodyParamKind.FORM_FIELD:
                picked_values[key] = await _pick_form_value(param.field, param.aliases, virtual_form)
            elif param.kind is _BodyParamKind.OTHER_FORM_MODEL:
                picked_values[key] = await _get_expected_form_model_value(param, virtual_form, known_param_names)
            else:
                # A param that takes the whole body would see the form model's data under the form model's key
                picked_values[key] = _FormValueMarker.UNKNOWN
        expected_values[index] = picked_values[key]
    return expected_values


async def _pick_form_value(  # pragma: no cover # FastAPI >= 0.158.2
    field: ModelField, aliases: list[str], virtual_form: _VirtualForm
) -> Union[_ExpectedFormValue, _FormValueMarker]:
    """Return the alias and the value that FastAPI would pick for the field from the virtual form, if any.

    Like FastAPI, we take the first alias that has a value, and read only that value, e.g. not an upload under a later
    alias, which a dependency may have closed. FastAPI reads the keys of the form with its own function.
    """
    overlay = virtual_form.overlay
    # FastAPI gives a missing boolean form field False before 0.162.0, but only if the form has none of its aliases
    has_any_alias = any(
        alias in virtual_form.form or overlay.get(alias, _FormValueMarker.ABSENT) is not _FormValueMarker.ABSENT
        for alias in aliases
    )
    # The aliases after the last one of the overlay, which FastAPI looks up in the form
    form_aliases: list[str] = []
    for alias in [*aliases, None]:
        if alias is not None and alias not in overlay:
            form_aliases.append(alias)
            continue
        if form_aliases and (not has_any_alias or not virtual_form.form.keys().isdisjoint(form_aliases)):
            form = FormData([(key, value) for key, value in virtual_form.form.multi_items() if key in form_aliases])
            form_value = await _extract_form_value(field, form)
            if form_value is not None:
                return form_value
        if alias is None:
            return _FormValueMarker.DEFAULT
        if overlay[alias] is not _FormValueMarker.ABSENT:
            return overlay[alias]
        form_aliases = []
    return _FormValueMarker.DEFAULT


async def _get_expected_form_model_value(  # pragma: no cover # FastAPI >= 0.158.2
    param: _BodyParamLookup, virtual_form: _VirtualForm, known_param_names: Union[set[str], None]
) -> Union[_ExpectedFormValue, _FormValueMarker]:
    """Return the dict that FastAPI would validate the form model param from for the migrated request.

    Like FastAPI, an optional form model that the form doesn't supply gets its default. A single form model gets the
    pick of each of its fields and the other keys of the form, e.g. for its extra fields or validators. Before FastAPI
    0.160.0, the other keys don't include the form fields of other params. A union of form models gets every key.
    """
    if _FormValueMarker.UNKNOWN in virtual_form.overlay.values():
        return _FormValueMarker.UNKNOWN
    keys = [key for key in virtual_form.keys if virtual_form.overlay.get(key) is not _FormValueMarker.ABSENT]
    models = _get_form_models(param.field)
    if type(None) in get_args(param.field.field_info.annotation) and not any(
        _form_supplies_form_model(model, FormData([(key, "") for key in keys]), known_param_names) for model in models
    ):
        return _FormValueMarker.DEFAULT
    value: dict[str, Any] = {}
    field_aliases: set[str] = set()
    own_names: set[str] = set()
    if len(models) == 1:
        for model_field in _get_cached_model_fields(models[0]):
            aliases = [alias for alias, _ in _get_param_aliases(model_field)]
            own_names.update(aliases)
            picked_value = await _pick_form_value(model_field, aliases, virtual_form)
            # FastAPI visits aliases only through the first one with a value. Later aliases remain available
            # as extra fields, even when another param also declares them.
            for alias in aliases:
                field_aliases.add(alias)
                if isinstance(picked_value, _ExpectedFormValue) and alias == picked_value.alias:
                    break
            if isinstance(picked_value, _ExpectedFormValue):
                value[picked_value.alias] = picked_value.value
            elif picked_value is _FormValueMarker.UNKNOWN:
                return picked_value
    for key in keys:
        if key in field_aliases or (
            len(models) == 1 and known_param_names is not None and key in known_param_names and key not in own_names
        ):
            continue
        overlay_value = virtual_form.overlay.get(key)
        if isinstance(overlay_value, _ExpectedFormValue):
            # FastAPI would validate the models of the union from a collection of the body as if it were repeated form
            # values, so a model could get only part of it
            if len(models) > 1 and isinstance(overlay_value.value, _FORM_VALUE_COLLECTIONS):
                return _FormValueMarker.UNKNOWN
            value[key] = overlay_value.value
        else:
            items = virtual_form.form.getlist(key)
            value[key] = items[0] if len(items) == 1 else items
    return _ExpectedFormValue(param.aliases[0], value, is_from_form=False)


async def _extract_form_value(  # pragma: no cover # FastAPI >= 0.158.2
    field: ModelField, form: FormData
) -> Union[_ExpectedFormValue, None]:
    """Return the alias and the value that FastAPI picks for the field from the form, if any."""
    # FastAPI yields the other keys of the form too, so we go through all of them to close the generator
    values = [
        _ExpectedFormValue(alias, value, is_from_form=True)
        async for alias, value, item_field in _extract_form_body([field], form)
        if item_field is not None
    ]
    return values[0] if values else None


def _get_param_with_other_value(  # pragma: no cover # FastAPI >= 0.158.2
    lookup: _FormModelLookup,
    params: list[_BodyParamLookup],
    expected_values: dict[int, Union[_ExpectedFormValue, _FormValueMarker]],
    body: Any,
    *,
    own_key: bool,
) -> Union[_BodyParamLookup, None]:
    """Return a param that FastAPI would give another value than the expected one from the embedded body, if any.

    Like FastAPI, we take the value of the first alias of the param that the body has. Every param has an expected
    value: the endpoint's form models get the body, and the other params their values for the migrated request.
    """
    embedded_body = _embed_form_model(body, lookup)
    form_model_body = embedded_body[lookup.aliases[0]]
    for index, param in enumerate(params):
        found_alias = next(
            (alias for alias in _get_lookup_aliases(param, own_key=own_key) if alias in embedded_body), None
        )
        expected = expected_values.get(index)
        if param.kind is _BodyParamKind.SAME_FORM_MODEL:
            is_expected = found_alias is not None and embedded_body[found_alias] is form_model_body
        elif not isinstance(expected, _ExpectedFormValue):
            is_expected = expected is _FormValueMarker.DEFAULT and found_alias is None
        elif found_alias != expected.alias:
            is_expected = False
        else:
            found_value = embedded_body[found_alias]
            is_expected = (
                found_value is expected.value
                # FastAPI's values from the form can't run user code when compared, unlike the values of the body
                or (
                    expected.is_from_form
                    and found_alias in lookup.form_values
                    and found_alias not in lookup.aliases
                    and found_value == expected.value
                )
                or (param.kind is _BodyParamKind.OTHER_FORM_MODEL and _is_same_dict(found_value, expected.value))
            )
        if not is_expected:
            return param
    return None


def _is_same_dict(found: Any, expected: dict[str, Any]) -> bool:  # pragma: no cover # FastAPI >= 0.158.2
    """Return whether the dict has the same keys in the same order, and the very same values."""
    return (
        type(found) is dict
        and list(found) == list(expected)
        and all(found[key] is value for key, value in expected.items())
    )


def _describe_body_param(param: _BodyParamLookup) -> str:  # pragma: no cover # FastAPI >= 0.158.2
    call_name = getattr(param.call, "__qualname__", None) or repr(param.call)
    return f"`{param.field.name}` of `{call_name}`"


def _embed_form_model(body: Any, form_model_lookup: _FormModelLookup) -> Any:
    """Return the body with the form models embedded under their aliases.

    Other form fields, e.g. the ones that only a dependency override declares, look their values up in the body itself,
    as they did before FastAPI 0.158.2, or in the values that FastAPI gives them from the form. A form model of an
    override wins over a form field with the same alias.

    FastAPI takes the dict body of a union of form models as form data, so we pass the union a copy that FastAPI turns
    back into the body's values. If there's no such copy, then since FastAPI 0.161.0, we raise, because the last
    release failed for this body too. Before FastAPI 0.161.0, the last release passed the body as it is, and FastAPI
    gave one of the models only part of a value. That works for some apps, e.g. if that model rejects the body anyway
    because of another field, so we keep it.
    """
    form_model_body = body
    if form_model_lookup.form_union_readers and isinstance(body, dict):  # pragma: no cover # FastAPI >= 0.153
        try:
            form_model_body = _get_form_union_body(body, form_model_lookup.form_union_readers)
        except _FormUnionValueError:
            if form_model_lookup.form_union_is_form_model:
                raise
            return body
    if not form_model_lookup.aliases:
        return form_model_body
    return {  # pragma: no cover # FastAPI >= 0.158.2
        **(body if isinstance(body, dict) and form_model_lookup.spreads_body else {}),
        **form_model_lookup.form_values,
        **dict.fromkeys(form_model_lookup.aliases, form_model_body),
    }


class _FormUnionValueError(CadwynError):
    pass


_FORM_VALUE_COLLECTIONS = (list, set, frozenset, tuple, deque)


def _get_form_union_body(  # pragma: no cover # FastAPI >= 0.153
    body: dict[str, Any], readers: tuple[_FormModelReader, ...]
) -> dict[str, Any]:
    """Return a copy of the body that FastAPI turns back into the body's values for every form model that reads it.

    FastAPI validates every model of a union of form models from the dict body as if it were form data: it takes a
    list as repeated form values, so a field that takes a single value gets only the last of them, and any other value
    as a single form value, so a field that takes repeated values gets the whole set or tuple as one of them. So for
    every list, set, frozenset, tuple, or deque, we pass its items as repeated form values, or the whole value as a
    single form value (a list in a list). Form models outside the union validate the dict as it is.

    We start with what the fields of each key take, and check the whole copy with FastAPI's own function, because the
    keys affect each other: a field takes the first of its aliases that has a value, and FastAPI takes the others as
    extra fields. While some keys come out wrong, we switch a single key, first a wrong one, then another alias of a
    field of a wrong one, and keep the switch if fewer keys come out wrong and no other one does. If no switch helps,
    a model would silently get a wrong value, e.g. because it takes a single value for the key and another one takes
    repeated values, so we raise.
    """
    fields_of_readers = {reader: _get_form_model_fields(reader.model) for reader in readers}
    readers_of_keys = {
        key: [reader for reader in readers if fields_of_readers[reader].reads(key)]
        for key, value in body.items()
        if isinstance(value, _FORM_VALUE_COLLECTIONS)
    }
    # Only the models of the union get the values as form data, so the other values reach every model as they are
    keys = [
        key
        for key, key_readers in readers_of_keys.items()
        if any(reader.is_validated_as_form_data for reader in key_readers)
    ]
    if not keys:
        return body
    checked_readers = {reader for key in keys for reader in readers_of_keys[key]}

    def get_wrong_keys(single_value_keys: frozenset[str]) -> tuple[dict[str, Any], set[str]]:
        union_body = {
            key: _as_form_value(value, as_single_value=key in single_value_keys) if key in keys else value
            for key, value in body.items()
        }
        bodies_of_readers = {
            reader: _normalize_form_model(union_body, model=reader.model)
            if reader.is_validated_as_form_data
            else union_body
            for reader in checked_readers
        }
        wrong_keys = {
            key
            for key in keys
            if not all(
                _form_model_gets_value(fields_of_readers[reader], key, body[key], bodies_of_readers[reader])
                for reader in readers_of_keys[key]
            )
        }
        return union_body, wrong_keys

    single_value_keys = frozenset(
        key for key in keys if _takes_single_form_value(body[key], readers_of_keys[key], fields_of_readers, key)
    )
    union_body, wrong_keys = get_wrong_keys(single_value_keys)
    # Each switch that we keep fixes a key, so this is at most quadratic in the number of keys
    switches_left = 2 * len(keys)
    while wrong_keys:
        switched = False
        for key in _get_keys_to_switch(wrong_keys, keys, readers_of_keys, fields_of_readers):
            if switches_left == 0:
                break
            switches_left -= 1
            candidate_keys = single_value_keys ^ {key}
            candidate_body, candidate_wrong_keys = get_wrong_keys(candidate_keys)
            if candidate_wrong_keys < wrong_keys:
                single_value_keys, union_body, wrong_keys = candidate_keys, candidate_body, candidate_wrong_keys
                switched = True
                break
        if not switched:
            key = next(key for key in keys if key in wrong_keys)
            raise _FormUnionValueError(_describe_form_union_value_error(key, body[key], readers_of_keys[key]))
    return union_body


def _takes_single_form_value(  # pragma: no cover # FastAPI >= 0.153
    value: Any,
    readers: list[_FormModelReader],
    fields_of_readers: "dict[_FormModelReader, _FormModelFields]",
    key: str,
) -> bool:
    """Return whether to start with the value as a single form value.

    That's the case if no field with the key takes repeated values, and no model outside the union would get the list
    in a list.
    """
    if isinstance(value, list) and not all(reader.is_validated_as_form_data for reader in readers):
        return False
    return not any(
        fields_of_readers[reader].takes_repeated_values(field)
        for reader in readers
        if reader.is_validated_as_form_data
        for field in fields_of_readers[reader].fields_of_keys.get(key, ())
    )


def _get_keys_to_switch(  # pragma: no cover # FastAPI >= 0.153
    wrong_keys: set[str],
    keys: list[str],
    readers_of_keys: dict[str, list[_FormModelReader]],
    fields_of_readers: "dict[_FormModelReader, _FormModelFields]",
) -> list[str]:
    """Return the wrong keys, and then the other aliases of the fields of the wrong keys, in the order of the body."""
    related_keys = {
        alias
        for key in wrong_keys
        for reader in readers_of_keys[key]
        for field in fields_of_readers[reader].fields_of_keys.get(key, ())
        for alias in fields_of_readers[reader].aliases_of_field(field)
    }
    return [key for key in keys if key in wrong_keys] + [
        key for key in keys if key in related_keys and key not in wrong_keys
    ]


def _as_form_value(value: Any, *, as_single_value: bool) -> Any:  # pragma: no cover # FastAPI >= 0.153
    if as_single_value:
        # FastAPI takes any value that isn't a list as a single form value
        return [value] if isinstance(value, list) else value
    return value if isinstance(value, list) else list(value)


def _describe_form_union_value_error(  # pragma: no cover # FastAPI >= 0.153
    key: str, value: Any, readers: list[_FormModelReader]
) -> str:
    model_names = ", ".join(
        f"`{reader.model.__name__}`" + ("" if reader.is_validated_as_form_data else " outside the union")
        for reader in readers
    )
    return (
        f"Cadwyn can't pass the {type(value).__name__} in {key!r} of the request body to every form model that reads "
        f"it ({model_names}). FastAPI validates the models of a union of form models from the body as if it were form "
        "data, so a field that takes a single form value would get only the last item of a list, a field that takes "
        "repeated form values would get a set or a tuple as one item, and a form model outside the union would get a "
        f"list in a list. Make {key!r} take repeated values (e.g. `list[str]`) in all of these models or in none of "
        "them, or don't put a list, set, or tuple into it in a validator or a migration."
    )


def _form_model_gets_value(  # pragma: no cover # FastAPI >= 0.153
    fields: "_FormModelFields", key: str, value: Any, model_body: dict[str, Any]
) -> bool:
    """Return whether the form model gets exactly the value from the dict that FastAPI validates it from.

    A list may be another list with the same items. A set, a frozenset, a tuple, a deque, or a subclass of a list may
    be a list of its items only if every field that Pydantic validates from the key takes repeated form values, so
    that Pydantic turns the list back into the field's type. Pydantic validates a field from the first of its aliases
    that the dict has.
    """
    result = model_body.get(key, PydanticUndefined)
    if result is value:
        return True
    items = value if type(value) is list else list(value)
    if type(result) is not list or len(result) != len(items):
        return False
    if any(result_item is not item for result_item, item in zip(result, items, strict=True)):
        return False
    if type(value) is list:
        return True
    fields_of_key = [
        field
        for field in fields.fields_of_keys.get(key, ())
        if not any(alias in model_body for alias in fields.aliases_before(field, key))
    ]
    return bool(fields_of_key) and all(fields.takes_repeated_values(field) for field in fields_of_key)


@dataclasses.dataclass(frozen=True)
class _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    """How FastAPI and Pydantic take the keys of form data for the fields of a form model. It depends only on the model.

    The fields are FastAPI's cached fields of the model, so we key them by identity.
    """

    allows_extra_fields: bool
    # The fields that FastAPI takes each key as an alias of
    fields_of_keys: dict[str, tuple[ModelField, ...]]
    # The aliases of each field, in the order in which Pydantic looks them up
    aliases_of_fields: dict[int, tuple[str, ...]]
    repeated_value_fields: frozenset[int]
    # The keys that Pydantic reads through an `AliasPath`, which FastAPI takes as extra fields
    alias_path_keys: frozenset[str]

    def reads(self, key: str) -> bool:
        return self.allows_extra_fields or key in self.fields_of_keys or key in self.alias_path_keys

    def aliases_of_field(self, field: ModelField) -> tuple[str, ...]:
        return self.aliases_of_fields[id(field)]

    def aliases_before(self, field: ModelField, key: str) -> tuple[str, ...]:
        aliases = self.aliases_of_field(field)
        return aliases[: aliases.index(key)]

    def takes_repeated_values(self, field: ModelField) -> bool:
        return id(field) in self.repeated_value_fields


_FORM_MODEL_FIELDS: "weakref.WeakKeyDictionary[type[BaseModel], _FormModelFields]" = weakref.WeakKeyDictionary()


def _get_form_model_fields(model: type[BaseModel]) -> _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    fields = _FORM_MODEL_FIELDS.get(model)
    if fields is None:
        fields = _FORM_MODEL_FIELDS[model] = _build_form_model_fields(model)
    return fields


def _build_form_model_fields(model: type[BaseModel]) -> _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    """Find the aliases of the fields by asking FastAPI about each name that Pydantic could validate a field from."""
    fields_of_keys: dict[str, list[ModelField]] = {}
    aliases_of_fields: dict[int, tuple[str, ...]] = {}
    repeated_value_fields: set[int] = set()
    alias_path_keys: set[str] = set()
    for field in _get_cached_model_fields(model):
        validation_alias = field.field_info.validation_alias
        choices = validation_alias.choices if isinstance(validation_alias, AliasChoices) else [validation_alias]
        alias_path_keys.update(
            choice.path[0] for choice in choices if isinstance(choice, AliasPath) and isinstance(choice.path[0], str)
        )
        possible_keys: list[Union[str, None]] = [choice for choice in choices if isinstance(choice, str)]
        possible_keys += [field.field_info.alias, field.alias, field.name]
        aliases = tuple(
            key
            for key in dict.fromkeys(possible_keys)
            if isinstance(key, str)
            and any(
                alias == key and item_field is field
                for alias, _, item_field in _extract_form_items([field], FormData([(key, "value")]))
            )
        )
        aliases_of_fields[id(field)] = aliases
        for alias in aliases:
            fields_of_keys.setdefault(alias, []).append(field)
        if aliases and isinstance(
            _get_multidict_value(field, FormData([(aliases[0], "a"), (aliases[0], "b")]), aliases[0]), list
        ):
            repeated_value_fields.add(id(field))
    return _FormModelFields(
        allows_extra_fields=model.model_config.get("extra") == "allow",
        fields_of_keys={key: tuple(fields) for key, fields in fields_of_keys.items()},
        aliases_of_fields=aliases_of_fields,
        repeated_value_fields=frozenset(repeated_value_fields),
        alias_path_keys=frozenset(alias_path_keys),
    )


def _unembed_form_model_errors(errors: list[Any], form_model_aliases: tuple[str, ...]) -> list[Any]:
    """Return the errors at the locations that FastAPI reports for form models that it validates from forms."""
    return [
        {**error, "loc": ("body", *error["loc"][2:])}
        if len(error["loc"]) > 1 and error["loc"][0] == "body" and error["loc"][1] in form_model_aliases
        else error
        for error in errors
    ]
