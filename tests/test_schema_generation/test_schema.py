import re

import pytest
from pydantic import BaseModel, RootModel

from cadwyn.exceptions import CadwynStructureError, InvalidGenerationInstructionError
from cadwyn.structure.schemas import schema
from tests._data.sqlalchemy_models import MappedChild, MappedModel, ModelFields
from tests.conftest import CreateRuntimeSchemas, assert_models_are_equal, version_change


class MySchema(BaseModel):
    foo: str


def test__schema_had_name(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(MySchema).had(name="Aww")))

    assert_models_are_equal(schemas["2000-01-01"][MySchema], MySchema)
    assert schemas["2000-01-01"][MySchema].__name__ == "Aww"
    assert schemas["2000-01-01"][MySchema].__qualname__ == "Aww"


def test__schema_had_name__with_the_same_name__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of "MySchema" in "MyVersionChange" '
            "but it already has the name you tried to assign."
        ),
    ):
        create_runtime_schemas(version_change(schema(MySchema).had(name="MySchema")))


@pytest.mark.parametrize("model", [MappedModel, MappedChild, ModelFields])
def test__schema__sqlalchemy_model__should_reject_migrations(model: type[BaseModel]):
    with pytest.raises(CadwynStructureError, match=r"SQLAlchemy.*separate Pydantic model"):
        schema(model)


class DocumentedSchema(BaseModel):
    """INTERNAL: head-only implementation note."""

    foo: str


class IndentedDocumentedSchema(BaseModel):
    """
    Public description.
    """  # noqa: D200, D212

    foo: str


def test__schema_had_description__should_change_description_of_older_versions_only(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(DocumentedSchema).had(description="Public description.")))

    assert schemas["2000-01-01"][DocumentedSchema].__doc__ == "Public description."
    assert schemas["2000-01-01"][DocumentedSchema].model_json_schema()["description"] == "Public description."
    assert schemas["2001-01-01"][DocumentedSchema].model_json_schema()["description"] == (
        "INTERNAL: head-only implementation note."
    )
    assert_models_are_equal(schemas["2000-01-01"][DocumentedSchema], DocumentedSchema)


def test__schema_had_description__none__should_remove_description(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(DocumentedSchema).had(description=None)))

    assert schemas["2000-01-01"][DocumentedSchema].__doc__ is None
    assert "description" not in schemas["2000-01-01"][DocumentedSchema].model_json_schema()
    assert "description" in schemas["2001-01-01"][DocumentedSchema].model_json_schema()


def test__schema_had_description__for_model_without_docstring__should_add_description(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(MySchema).had(description="Old description")))

    assert schemas["2000-01-01"][MySchema].model_json_schema()["description"] == "Old description"
    assert "description" not in schemas["2001-01-01"][MySchema].model_json_schema()


def test__schema_had_description__should_propagate_to_all_older_versions(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(DocumentedSchema).had(description="The oldest description")),
        version_change(),
        version_change(schema(DocumentedSchema).had(description="An old description")),
    )

    assert [schemas[version][DocumentedSchema].model_json_schema().get("description") for version in schemas] == [
        "INTERNAL: head-only implementation note.",
        "An old description",
        "An old description",
        "The oldest description",
    ]


def test__schema_had_name_and_description__should_change_both(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(DocumentedSchema).had(name="OldSchema", description="Old description"))
    )

    assert schemas["2000-01-01"][DocumentedSchema].__name__ == "OldSchema"
    assert schemas["2000-01-01"][DocumentedSchema].__doc__ == "Old description"


def test__schema_had__without_arguments__should_raise_error():
    with pytest.raises(
        CadwynStructureError,
        match=re.escape("You must pass at least one of the arguments of schema(DocumentedSchema).had()"),
    ):
        schema(DocumentedSchema).had()


@pytest.mark.parametrize(
    ("model", "description"),
    [
        (DocumentedSchema, "INTERNAL: head-only implementation note."),
        # Pydantic cleans up the indentation of docstrings so this description would be the same
        (IndentedDocumentedSchema, "Public description."),
    ],
)
def test__schema_had_description__with_the_same_description__should_raise_error(
    model: type[BaseModel], description: str, create_runtime_schemas: CreateRuntimeSchemas
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            f'You tried to change the description of "{model.__name__}" in "MyVersionChange" '
            "but it already has the description you tried to assign."
        ),
    ):
        create_runtime_schemas(version_change(schema(model).had(description=description)))


def test__schema_had_description__with_the_same_description_as_in_newer_version__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the description of "DocumentedSchema" in "MyVersionChange" '
            "but it already has the description you tried to assign."
        ),
    ):
        create_runtime_schemas(
            version_change(schema(DocumentedSchema).had(description="Old")),
            version_change(schema(DocumentedSchema).had(description="Old")),
        )


def test__schema_had_description__removing_missing_description__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to remove the description of "MySchema" in "MyVersionChange" '
            "but it already doesn't have a description."
        ),
    ):
        create_runtime_schemas(version_change(schema(MySchema).had(description=None)))


@pytest.mark.parametrize("description", ["", " ", "\n\t "])
def test__schema_had_description__blank__should_raise_error(description: str):
    with pytest.raises(
        CadwynStructureError,
        match=re.escape(
            "The description that you passed to schema(DocumentedSchema).had() is blank. "
            "Pass description=None to remove the description instead."
        ),
    ):
        schema(DocumentedSchema).had(description=description)


class DocumentedParent(BaseModel):
    """Parent description."""

    foo: str


class DocumentedChild(DocumentedParent):
    """Child description."""


class UndocumentedChild(DocumentedParent):
    pass


def test__schema_had_description__of_parent__should_not_change_descriptions_of_subclasses(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(DocumentedParent).had(description="Old parent description"),
            schema(DocumentedParent).field("foo").had(max_length=3),
        )
    )

    assert schemas["2000-01-01"][DocumentedParent].model_json_schema()["description"] == "Old parent description"
    assert schemas["2000-01-01"][DocumentedChild].model_json_schema()["description"] == "Child description."
    assert "description" not in schemas["2000-01-01"][UndocumentedChild].model_json_schema()
    # The subclasses are still copied with the changes of their parent
    assert schemas["2000-01-01"][UndocumentedChild].model_json_schema()["properties"]["foo"]["maxLength"] == 3


def test__schema_had_description__of_subclass__should_not_change_descriptions_of_parent_and_siblings(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(UndocumentedChild).had(description="Old child description")))

    assert schemas["2000-01-01"][UndocumentedChild].model_json_schema()["description"] == "Old child description"
    assert schemas["2000-01-01"][DocumentedParent].model_json_schema()["description"] == "Parent description."
    assert schemas["2000-01-01"][DocumentedChild].model_json_schema()["description"] == "Child description."


class DocumentedRootModel(RootModel[list[int]]):
    """Head root description."""


def test__schema_had_description__of_root_model__should_change_its_description(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(DocumentedRootModel).had(description="Old root description"))
    )

    assert schemas["2000-01-01"][DocumentedRootModel].model_json_schema() == {
        "description": "Old root description",
        "items": {"type": "integer"},
        "title": "DocumentedRootModel",
        "type": "array",
    }
    assert schemas["2001-01-01"][DocumentedRootModel].model_json_schema()["description"] == "Head root description."
