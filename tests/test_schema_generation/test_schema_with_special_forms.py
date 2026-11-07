import sys
import warnings
from collections.abc import Callable
from typing import Any, ClassVar, Final, TypeGuard

import pytest
import typing_extensions
from pydantic import BaseModel, create_model

from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from tests.conftest import CreateRuntimeSchemas, CreateVersionedClients, version_change

# Pydantic treats `Final` with a default as a class variable, and warns that it will become a field in pydantic v3
_FINAL_WITH_DEFAULT_WARNING = "ignore:Annotation .* is marked as final and has a default value"


class Item(BaseModel):
    name: str


def _version_change_with_changed_item(*instructions: Any):
    return version_change(schema(Item).field("name").had(type=int), *instructions)


with warnings.catch_warnings():
    warnings.filterwarnings("ignore", "Annotation .* is marked as final and has a default value")

    class ModelWithFinals(BaseModel):
        constant: Final[int] = 1
        item: Final[Item]
        other: str

    class ChildOfModelWithFinals(ModelWithFinals):
        child_field: str


@pytest.mark.filterwarnings(_FINAL_WITH_DEFAULT_WARNING)
@pytest.mark.parametrize(
    ("model", "instruction"),
    [
        pytest.param(ModelWithFinals, schema(ModelWithFinals).field("other").had(description="Other"), id="itself"),
        pytest.param(
            ChildOfModelWithFinals,
            schema(ChildOfModelWithFinals).field("child_field").had(description="Child"),
            id="child",
        ),
    ],
)
def test__schema_field_had__model_with_final_annotations__should_keep_final_class_vars_and_fields(
    create_runtime_schemas: CreateRuntimeSchemas, model: type[ModelWithFinals], instruction: Any
):
    schemas = create_runtime_schemas(_version_change_with_changed_item(instruction))

    model_copy = schemas["2000-01-01"][model]
    assert model_copy.__class_vars__ == {"constant"}
    assert model_copy.constant == 1
    assert set(model_copy.model_fields) == set(model.model_fields)
    assert model_copy.model_fields["item"].annotation is schemas["2000-01-01"][Item]
    assert model_copy.model_fields["item"].frozen is True
    assert model_copy.model_validate({"item": {"name": 1}, "other": "a", "child_field": "b"}).item.name == 1


@pytest.mark.filterwarnings(_FINAL_WITH_DEFAULT_WARNING)
def test__router_generation__body_with_final_annotations__should_validate_each_version_and_hide_class_var(
    router: VersionedAPIRouter, create_versioned_clients: CreateVersionedClients
):
    @router.post("/test")
    async def route(payload: ChildOfModelWithFinals):
        return {"item": payload.item.model_dump(), "constant": payload.constant}

    clients = create_versioned_clients(
        _version_change_with_changed_item(
            schema(ChildOfModelWithFinals).field("child_field").had(description="Child"),
        )
    )

    body = {"item": {"name": "a"}, "other": "a", "child_field": "b"}
    assert clients["2001-01-01"].post("/test", json=body).json() == {"item": {"name": "a"}, "constant": 1}
    # The older version validates its own Item, whose name is an int
    response = clients["2000-01-01"].post("/test", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "item", "name"]
    for version, client in clients.items():
        openapi = client.get(f"/openapi.json?version={version}").json()
        assert set(openapi["components"]["schemas"]["ChildOfModelWithFinals"]["properties"]) == {
            "item",
            "other",
            "child_field",
        }


if sys.version_info >= (3, 13):
    # Type checkers call ClassVar[Final[...]] redundant, but the runtime and pydantic allow it since Python 3.13
    ModelWithFinalClassVar = type(
        "ModelWithFinalClassVar",
        (BaseModel,),
        {"__annotations__": {"constant": ClassVar[Final[int]], "other": str}, "constant": 1},
    )


@pytest.mark.skipif(sys.version_info < (3, 13), reason="ClassVar[Final[...]] is only allowed since Python 3.13")
def test__schema_field_had__model_with_final_class_var__should_keep_it_a_class_var(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ModelWithFinalClassVar).field("other").had(description="Other"))
    )

    model_copy = schemas["2000-01-01"][ModelWithFinalClassVar]
    assert model_copy.__class_vars__ == {"constant"}
    assert model_copy.constant == 1
    assert set(model_copy.model_fields) == {"other"}


@pytest.mark.parametrize(
    "special_form",
    [
        pytest.param(TypeGuard, id="typing.TypeGuard"),
        pytest.param(typing_extensions.TypeGuard, id="typing_extensions.TypeGuard"),
        pytest.param(typing_extensions.TypeIs, id="typing_extensions.TypeIs"),
    ],
)
def test__schema_field_had__callable_that_returns_special_form_of_changed_model__should_use_versioned_model(
    create_runtime_schemas: CreateRuntimeSchemas, special_form: Any
):
    model = create_model("ModelWithCallableField", checker=(Callable[[Any], special_form[Item]], ...), other=(str, ...))

    schemas = create_runtime_schemas(
        _version_change_with_changed_item(schema(model).field("other").had(description="Other"))
    )

    item_copy = schemas["2000-01-01"][Item]
    assert schemas["2000-01-01"][model].model_fields["checker"].annotation == Callable[[Any], special_form[item_copy]]
