from docs_src.concepts.schema_generation.block001 import (
    MyVersionedSchema,
    MyVersionedSchemaFrom2025,
    schema_generators,
)


def test__generate_versioned_models__older_version__should_have_old_fields():
    assert set(MyVersionedSchemaFrom2025.model_fields) == {"name"}
    user = MyVersionedSchemaFrom2025.model_validate({"name": "John"})
    assert user.model_dump() == {"name": "John"}


def test__generate_versioned_models__latest_version__should_have_head_fields():
    latest_schema = schema_generators["2026-01-01"][MyVersionedSchema]
    assert set(latest_schema.model_fields) == {"full_name"}
