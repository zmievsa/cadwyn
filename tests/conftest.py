import threading
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from copy import deepcopy
from enum import Enum
from pathlib import Path
from typing import Any, Union

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_fixture_classes import fixture_class
from typing_extensions import override

from cadwyn import Cadwyn, VersionBundle, VersionedAPIRouter
from cadwyn._utils import same_method_definition_as_in
from cadwyn.schema_generation import (
    SchemaGenerator,
    _AnnotationTransformer,
    _change_version_of_model_references,
    _extract_passed_field_attributes,
    _get_head_model_references,
    _get_model_annotations,
    _get_model_field_names,
    _is_versionable_model,
    _PydanticModelWrapper,
    _ReferencedModelCollector,
    _unwrap_model,
    generate_versioned_models,
)
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction, SchemaHadInstruction
from cadwyn.structure.versions import HeadVersion

CURRENT_DIR = Path(__file__).parent
Undefined = object()


@pytest.fixture(autouse=True)
def _check_that_reference_discovery_finds_every_model_that_copying_requests(monkeypatch: pytest.MonkeyPatch):
    """Check that copying a model only requests the models that reference discovery finds in it.

    Generic args are only versioned when reference discovery shows that copying them cannot recurse forever,
    which is only true if discovery finds a superset of the models that copying requests. So for every model
    copied in the test suite, we check every model that copying requests against what discovery found:
    the references that the generator cached and the references that discovery finds in the model right now.
    We check it on every request so that a missed reference fails the test before it can lead to infinite recursion.

    The models in reference cycles that do not need versioning are not copied at all, so we also check that
    copying them would only request the models that discovery found in them. Otherwise, they could refer to
    the head versions of the models that have instructions.
    """
    state = threading.local()
    original_getitem = SchemaGenerator.__getitem__
    original_generate_model_copy = _PydanticModelWrapper.generate_model_copy

    def getitem(self: SchemaGenerator, model: Any, /) -> Any:
        models_being_copied = getattr(state, "models_being_copied", [])
        if models_being_copied and _is_versionable_model(model):
            model_being_copied, discovered_references = models_being_copied[-1]
            for references in discovered_references:
                assert _unwrap_model(model) in references.models | references.arg_models | references.base_models, (
                    f"Reference discovery missed {model} in {model_being_copied}"
                )
        versioned_model = original_getitem(self, model)
        if versioned_model is model and _is_versionable_model(model) and issubclass(model, BaseModel):
            check_that_discovery_finds_every_model_that_copying_would_request(self, model)
        return versioned_model

    def generate_model_copy(self: _PydanticModelWrapper, generator: SchemaGenerator) -> Any:
        if not hasattr(state, "models_being_copied"):
            state.models_being_copied = []
        discovered_references = [
            # The models with instructions are always discovered through their wrappers, the others are
            # discovered in place
            self.get_references(generator.model_bundle.schemas)
            if self.cls in generator._changed_models
            else _get_head_model_references(self.cls),
            # The references that the generator caches for its decisions
            generator._get_references(self.cls),
        ]
        # Discovery does not know the references of a model whose annotations refer to names that are not
        # defined yet so the generic args that lead to it are never versioned
        complete_references = [references for references in discovered_references if references.is_complete]
        state.models_being_copied.append((self.cls, complete_references))
        try:
            return original_generate_model_copy(self, generator)
        finally:
            state.models_being_copied.pop()

    monkeypatch.setattr(SchemaGenerator, "__getitem__", getitem)
    monkeypatch.setattr(_PydanticModelWrapper, "generate_model_copy", generate_model_copy)


def check_that_discovery_finds_every_model_that_copying_would_request(
    generator: SchemaGenerator, model: type[BaseModel]
) -> None:
    references = generator._get_references(model)
    # A collector that requests the models the same way as copying does instead of only recording them
    collector = _ReferencedModelCollector()
    collector.annotation_transformer = _AnnotationTransformer(collector)
    fields = model.model_fields
    _change_version_of_model_references(
        collector,
        model,
        {name: _extract_passed_field_attributes(fields[name]) for name in _get_model_field_names(model, fields)},
        _get_model_annotations(model, fields),
    )
    requested_models = collector.models | collector.arg_models | collector.base_models
    discovered_models = references.models | references.arg_models | references.base_models
    assert requested_models <= discovered_models, (
        f"Reference discovery missed {requested_models - discovered_models} in {model}"
    )


@pytest.fixture
def api_version_var():
    api_version_var = ContextVar("api_version_var")
    api_version_var.set(None)
    return api_version_var


@pytest.fixture
def random_uuid():
    return uuid.uuid4()


class CadwynTestClient(TestClient):
    @same_method_definition_as_in(TestClient.__init__)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.app: Cadwyn


class TestClientWithHardcodedAPIVersion(CadwynTestClient):
    def __init__(
        self,
        *args,
        api_version_var: Union[ContextVar, None] = None,
        api_version: Union[str, object, None] = Undefined,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.api_version_var = api_version_var
        self.api_version = api_version

    @override
    def request(self, *args, **kwargs):
        if self.api_version is not Undefined and self.api_version_var:
            self.api_version_var.set(self.api_version)
        return super().request(*args, **kwargs)


def client(
    router: APIRouter,
    api_version: Any = Undefined,
    api_version_var: Union[ContextVar[Union[str, None]], None] = None,
):
    app = FastAPI()
    app.include_router(router)

    return TestClientWithHardcodedAPIVersion(app, api_version=api_version, api_version_var=api_version_var)


@pytest.fixture
def router() -> VersionedAPIRouter:
    return VersionedAPIRouter()


@fixture_class(name="create_runtime_schemas")
class CreateRuntimeSchemas:
    def __call__(self, *version_changes: type[VersionChange]) -> dict[str, SchemaGenerator]:
        return generate_versioned_models(VersionBundle(*versions(*version_changes)))


@fixture_class(name="create_versioned_app")
class CreateVersionedApp:
    api_version_var: ContextVar[Union[str, None]]
    router: VersionedAPIRouter

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: Union[VersionedAPIRouter, None] = None,
    ) -> Cadwyn:
        router = router or self.router
        app = Cadwyn(
            versions=VersionBundle(
                HeadVersion(*head_version_changes),
                *versions(*version_changes),
                api_version_var=self.api_version_var,
            )
        )
        app.generate_and_include_versioned_routers(router)
        app._cadwyn_initialize()
        return app


def versions(*version_changes: type[VersionChange]) -> list[Version]:
    versions = [Version("2000-01-01")]
    for i, change in enumerate(version_changes):
        versions.append(Version(f"{2001 + i}-01-01", change))
    return list(reversed(versions))


@fixture_class(name="create_versioned_clients")
class CreateVersionedClients:
    create_versioned_app: CreateVersionedApp
    api_version_var: ContextVar[Union[str, None]]

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: Union[VersionedAPIRouter, None] = None,
    ) -> dict[str, CadwynTestClient]:
        app = self.create_versioned_app(*version_changes, head_version_changes=head_version_changes, router=router)
        return {
            version: CadwynTestClient(app, headers={app.router.api_version_parameter_name: version})
            for version in reversed(app.router.versioned_routers)
        }


def version_change(
    *instructions: Union[
        SchemaHadInstruction, AlterSchemaSubInstruction, AlterEndpointSubInstruction, AlterEnumSubInstruction
    ],
    **body_items: Any,
) -> type[VersionChange]:
    return type(VersionChange)(
        "MyVersionChange",
        (VersionChange,),
        {
            "description": "",
            "instructions_to_migrate_to_previous_version": instructions,
            **body_items,
        },
    )


def serialize_enum(enum: type[Enum]) -> dict[str, Any]:
    return {member.name: member.value for member in enum}


def assert_models_are_equal(model1: type[BaseModel], model2: type[BaseModel]):
    model1_schema = serialize_schema(model1.__pydantic_core_schema__)
    model2_schema = serialize_schema(model2.__pydantic_core_schema__)
    assert model1_schema == model2_schema


def serialize_schema(schema: Any):
    schema = deepcopy(schema)
    if "cls" in schema:
        schema_to_modify = schema
    else:
        schema_to_modify = schema["schema"]
    del schema_to_modify["cls"]
    if "model_name" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["model_name"]
    if "schema" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["schema"]["model_name"]
    del schema_to_modify["config"]["title"]
    return serialize_object(schema)


def serialize_object(obj: Any):
    if isinstance(obj, dict):
        obj.pop("schema_ref", None)
        obj.pop("ref", None)
        return {k: v.__name__ if callable(v) else serialize_object(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        modified_list = []
        for v in obj:
            if callable(v):
                v = v.__name__
            modified_list.append(serialize_object(v))
        return modified_list
    else:
        return obj
