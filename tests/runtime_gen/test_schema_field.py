import inspect
import re
from typing import Any, Literal, Union

import pytest
from pydantic import BaseModel, Field, ValidationError, constr
from pydantic.fields import FieldInfo

from cadwyn._compat import PYDANTIC_V2, model_fields
from cadwyn.exceptions import (
    CadwynStructureError,
    InvalidGenerationInstructionError,
)
from cadwyn.structure import (
    schema,
)
from tests.conftest import (
    CreateLocalVersionedPackages,
    CreateRuntimeSchemas,
    HeadModuleFor,
    _FakeModuleWithEmptyClasses,
    _FakeNamespaceWithOneStrField,
    version_change,
)


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


@pytest.fixture()
def head_with_one_int_field(head_module_for: HeadModuleFor):
    return head_module_for(
        """
    from pydantic import BaseModel
    class SchemaWithOneIntField(BaseModel):
        foo: int
    """,
    )


##############
# EXISTED AS #
##############


def test__schema_field_existed_as__original_schema_is_empty(create_runtime_schemas: CreateRuntimeSchemas):
    v1 = create_runtime_schemas(
        version_change(
            schema(EmptySchema)
            .field("bar")
            .existed_as(
                type=int,
                info=Field(alias="boo"),
            )
        ),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(EmptySchema) == (
            "class EmptySchema(pydantic.BaseModel):\n"
            "    bar: int = Field(alias='boo', serialization_alias='boo', validation_alias='boo')\n"
        )
    else:
        assert inspect.getsource(EmptySchema) == (
            "class EmptySchema(pydantic.BaseModel):\n    bar: int = Field(alias='boo', alias_priority=2)\n"
        )


def test__field_existed_as__original_schema_has_a_field(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField)
            .field("bar")
            .existed_as(type=int, info=Field(description="Hello darkness my old friend")),
        )
    )
    model = models["2000-01-01"][SchemaWithOneStrField]
    assert model.__fields__["foo"].annotation == str
    assert model.__fields__["bar"].annotation == int
    assert model.__fields__["bar"].description == "Hello darkness my old friend"
    assert model(foo="Hello", bar=83).dict() == {"foo": "Hello", "bar": 83}


def test__field_existed_as__extras_are_added(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    v1 = create_runtime_schemas(
        schema(head_with_empty_classes.EmptySchema)
        .field("foo")
        .existed_as(
            type=int,
            info=Field(
                deflolbtt="hewwo",  # pyright: ignore[reportCallIssue]
            ),
        ),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.EmptySchema) == (
            "class EmptySchema(pydantic.BaseModel):\n"
            "    foo: int = Field(json_schema_extra={'deflolbtt': 'hewwo'})\n"
        )
    else:
        assert inspect.getsource(v1.EmptySchema) == (
            "class EmptySchema(pydantic.BaseModel):\n    foo: int = Field(deflolbtt='hewwo')\n"
        )


def test__schema_field_existed_as__with_default_none(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    v1 = create_runtime_schemas(
        schema(head_with_empty_classes.EmptySchema).field("foo").existed_as(type=str | None, info=Field(default=None)),
    )

    assert inspect.getsource(v1.EmptySchema) == (
        "class EmptySchema(pydantic.BaseModel):\n    foo: typing.Union[str, None] = Field(default=None)\n"
    )


def test__schema_field_existed_as__with_new_weird_data_types(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel, constr, Field
        from enum import Enum

        class MyEnum(Enum):
            foo = 2

        def my_default_factory():
            return 83

        class EmptySchema(BaseModel):
            pass

        """,
    )
    v1 = create_runtime_schemas(
        schema(latest.EmptySchema)
        .field("foo")
        .existed_as(
            type=dict[str, int],
            info=Field(default={"a": "b"}),
        ),
        schema(latest.EmptySchema)
        .field("bar")
        .existed_as(
            type=list[int],
            info=Field(default_factory=latest.my_default_factory),
        ),
        schema(latest.EmptySchema)
        .field("baz")
        .existed_as(
            type=Literal[latest.MyEnum.foo],  # pyright: ignore
        ),
    )

    assert inspect.getsource(v1.EmptySchema) == (
        "class EmptySchema(BaseModel):\n"
        "    foo: dict[str, int] = Field(default={'a': 'b'})\n"
        "    bar: list[int] = Field(default_factory=my_default_factory)\n"
        "    baz: typing.Literal[MyEnum.foo]\n"
    )


################
# DIDN'T EXIST #
################


def test__schema_field_didnt_exist(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    v1 = create_runtime_schemas(
        schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    pass\n"


def test__schema_field_didnt_exist__with_inheritance(
    create_local_versioned_packages: CreateLocalVersionedPackages,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
    from pydantic import BaseModel

    class ParentSchema(BaseModel):
        foo: str

    class ChildSchema(ParentSchema):
        pass
    """,
    )

    v1, v2, v3 = create_local_versioned_packages(
        version_change(schema(latest.ParentSchema).field("foo").didnt_exist),
        version_change(schema(latest.ChildSchema).field("bar").existed_as(type=int)),
    )

    assert "foo" not in model_fields(v1.ChildSchema)
    assert "foo" in model_fields(v2.ChildSchema)
    assert "foo" in model_fields(v3.ChildSchema)


#######
# HAD #
#######


def assert_field_had_changes_apply(
    model: type[BaseModel],
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    latest: Any,
):
    v1 = create_runtime_schemas(
        schema(getattr(latest, model.__name__)).field("foo").had(**{attr: attr_value}),
    )
    field_info = model_fields(getattr(v1, model.__name__))["foo"]
    if not PYDANTIC_V2:
        field_info = field_info.field_info  # pyright: ignore[reportAttributeAccessIssue]
    if PYDANTIC_V2 and attr in FieldInfo.metadata_lookup:
        # We do this because _PydanticGeneralMetadata does not have a proper `__eq__`
        assert repr(FieldInfo._collect_metadata({attr: attr_value})[0]) in [repr(obj) for obj in field_info.metadata]
    else:
        assert getattr(field_info, attr) == attr_value


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("default", 100),
        ("alias", "myalias"),
        ("title", "mytitle"),
        ("description", "mydescription"),
        ("gt", 3),
        ("ge", 4),
        ("lt", 5),
        ("le", 6),
        ("multiple_of", 7),
        ("repr", False),
    ],
)
def test__schema_field_had__modifying_int_field(
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_int_field,
):
    """This test is here to guarantee that we can handle all parameter types we provide"""

    assert_field_had_changes_apply(
        head_with_one_int_field.SchemaWithOneIntField,
        attr,
        attr_value,
        create_runtime_schemas,
        head_with_one_int_field,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("min_length", 20),
        ("max_length", 50),
    ],
)
def test__schema_field_had__str_field(
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    assert_field_had_changes_apply(
        head_with_one_str_field.SchemaWithOneStrField,
        attr,
        attr_value,
        create_runtime_schemas,
        head_with_one_str_field,
    )


def test__schema_field_had__pattern(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    if PYDANTIC_V2:
        attr_name = "pattern"
    else:
        attr_name = "regex"
    assert_field_had_changes_apply(
        head_with_one_str_field.SchemaWithOneStrField,
        attr_name,
        r"hewwo darkness",
        create_runtime_schemas,
        head_with_one_str_field,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("max_digits", 12),
        ("decimal_places", 15),
    ],
)
def test__schema_field_had__decimal_field(
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel
        from decimal import Decimal
        class SchemaWithOneDecimalField(BaseModel):
            foo: Decimal
        """,
    )
    assert_field_had_changes_apply(
        latest.SchemaWithOneDecimalField,
        attr,
        attr_value,
        create_runtime_schemas,
        latest,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("exclude", [16, 17, 18]),
    ],
)
def test__schema_field_had__list_of_int_field(
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel
        class SchemaWithOneListOfIntField(BaseModel):
            foo: list[int]
        """,
    )
    assert_field_had_changes_apply(
        latest.SchemaWithOneListOfIntField,
        attr,
        attr_value,
        create_runtime_schemas,
        latest,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("include", [19, 20, 21]),
        ("min_items", 10),
        ("max_items", 15),
        ("unique_items", True),
    ],
)
def test__schema_field_had__list_of_int_field__with_fields_deprecated_in_pydantic_2(
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    if PYDANTIC_V2:
        pytest.skip("This test is only for Pydantic v1.")
    latest = head_module_for(
        """
        from pydantic import BaseModel
        class SchemaWithOneListOfIntField(BaseModel):
            foo: list[int]
        """,
    )
    assert_field_had_changes_apply(
        latest.SchemaWithOneListOfIntField,
        attr,
        attr_value,
        create_runtime_schemas,
        latest,
    )


def test__schema_field_had__float_field(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel
        class SchemaWithOneFloatField(BaseModel):
            foo: float
        """,
    )
    assert_field_had_changes_apply(
        latest.SchemaWithOneFloatField,
        "allow_inf_nan",
        attr_value=False,
        create_runtime_schemas=create_runtime_schemas,
        latest=latest,
    )


def test__schema_field_didnt_have__removing_default(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel, Field
        class SchemaWithDefaults(BaseModel):
            foo: str = "hewwo"
            bar: int = Field(default=83)
        """
    )
    v1 = create_runtime_schemas(
        schema(latest.SchemaWithDefaults).field("foo").didnt_have("default"),
        schema(latest.SchemaWithDefaults).field("bar").didnt_have("default"),
    )

    assert inspect.getsource(v1.SchemaWithDefaults) == (
        "class SchemaWithDefaults(BaseModel):\n    foo: str\n    bar: int = Field()\n"
    )


@pytest.fixture()
def head_with_constraints(head_module_for: HeadModuleFor):
    return head_module_for(
        """
        from pydantic import BaseModel, conint, Field

        class SchemaWithConstraints(BaseModel):
            foo: conint(lt=2 + 5)
            bar: str = Field(min_length=0, max_length=2 + 5)
        """,
    )


def test__schema_field_didnt_have__constrained_field_constraints_removed__constraints_do_not_render(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints.SchemaWithConstraints).field("foo").didnt_have("lt"),
        schema(head_with_constraints.SchemaWithConstraints).field("bar").didnt_have("max_length", "min_length"),
    )

    assert inspect.getsource(v1.SchemaWithConstraints) == (
        "class SchemaWithConstraints(BaseModel):\n" "    foo: conint()\n" "    bar: str = Field()\n"
    )


def test__schema_field_had_constrained_field__only_non_constraint_field_args_were_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints.SchemaWithConstraints).field("foo").had(alias="foo1"),
        schema(head_with_constraints.SchemaWithConstraints).field("bar").had(alias="bar1"),
    )

    assert inspect.getsource(v1.SchemaWithConstraints) == (
        "class SchemaWithConstraints(BaseModel):\n"
        "    foo: conint(lt=2 + 5) = Field(alias='foo1')\n"
        "    bar: str = Field(min_length=0, max_length=2 + 5, alias='bar1')\n"
    )


def test__schema_field_had_constrained_field__field_is_an_unconstrained_union(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
from pydantic import BaseModel, Field

class Schema(BaseModel):
    foo: int | None = Field(default=None)

                      """,
    )
    v1 = create_runtime_schemas(
        schema(latest.Schema).field("foo").had(ge=0),
    )

    assert inspect.getsource(v1.Schema) == (
        "class Schema(BaseModel):\n    foo: int | None = Field(default=None, ge=0)\n"
    )


def test__schema_field_had_constrained_field__constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints.SchemaWithConstraints).field("foo").had(gt=8),
        schema(head_with_constraints.SchemaWithConstraints).field("bar").had(min_length=2),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithConstraints) == (
            "class SchemaWithConstraints(BaseModel):\n"
            "    foo: conint(lt=2 + 5) = Field(gt=8)\n"
            "    bar: str = Field(min_length=2, max_length=2 + 5)\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithConstraints) == (
            "class SchemaWithConstraints(BaseModel):\n"
            "    foo: conint(lt=2 + 5, gt=8)\n"
            "    bar: str = Field(min_length=2, max_length=2 + 5)\n"
        )


def test__schema_field_had_constrained_field__both_constraints_and_non_constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints.SchemaWithConstraints).field("foo").had(gt=8, alias="foo1"),
        schema(head_with_constraints.SchemaWithConstraints).field("bar").had(min_length=2, alias="bar1"),
    )
    if PYDANTIC_V2:
        # TODO Validate that this works
        assert inspect.getsource(v1.SchemaWithConstraints) == (
            "class SchemaWithConstraints(BaseModel):\n"
            "    foo: conint(lt=2 + 5) = Field(alias='foo1', gt=8)\n"
            "    bar: str = Field(min_length=2, max_length=2 + 5, alias='bar1')\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithConstraints) == (
            "class SchemaWithConstraints(BaseModel):\n"
            "    foo: conint(lt=2 + 5, gt=8) = Field(alias='foo1')\n"
            "    bar: str = Field(min_length=2, max_length=2 + 5, alias='bar1')\n"
        )


@pytest.fixture()
def head_with_constraints_and_field(head_module_for: HeadModuleFor):
    return head_module_for(
        """
        from pydantic import BaseModel, constr, Field

        MY_VAR = "hewwo"

        class SchemaWithConstraintsAndField(BaseModel):
            foo: constr(max_length=5) = Field(default=MY_VAR)

        """,
    )


def test__schema_field_had_constrained_field__constraint_field_args_were_modified_in_type(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints_and_field: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints_and_field.SchemaWithConstraintsAndField)
        .field("foo")
        .had(type=constr(max_length=6123123121)),
    )

    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithConstraintsAndField) == (
            "class SchemaWithConstraintsAndField(BaseModel):\n"
            "    foo: Annotated[str, StringConstraints(max_length=6123123121)] = Field(default=MY_VAR)\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithConstraintsAndField) == (
            "class SchemaWithConstraintsAndField(BaseModel):\n"
            "    foo: constr(max_length=6123123121) = Field(default=MY_VAR)\n"
        )


def test__schema_field_had_constrained_field__constraint_only_args_were_modified_in_type(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_constraints_and_field: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_constraints_and_field.SchemaWithConstraintsAndField)
        .field("foo")
        .had(type=constr(max_length=6, strip_whitespace=True)),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithConstraintsAndField) == (
            "class SchemaWithConstraintsAndField(BaseModel):\n"
            "    foo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=6)] = Field(default=MY_VAR)\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithConstraintsAndField) == (
            "class SchemaWithConstraintsAndField(BaseModel):\n"
            "    foo: constr(strip_whitespace=True, max_length=6) = Field(default=MY_VAR)\n"
        )


@pytest.fixture()
def head_with_annotated_constraints(head_module_for: HeadModuleFor):
    return head_module_for(
        """
        from pydantic import BaseModel, conint, Field
        from typing import Annotated
        import annotated_types

        class SchemaWithAnnotatedConstraints(BaseModel):
            foo: Annotated[conint(lt=2 + 5), Field(description='awaw')]
        """
    )


def test__schema_field_didnt_have_annotated_constrained_field(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_annotated_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_annotated_constraints.SchemaWithAnnotatedConstraints).field("foo").didnt_have("lt"),
    )

    assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
        "class SchemaWithAnnotatedConstraints(BaseModel):\n" "    foo: Annotated[conint(), Field(description='awaw')]\n"
    )


def test__schema_field_had_annotated_constrained_field(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_annotated_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_annotated_constraints.SchemaWithAnnotatedConstraints).field("foo").had(alias="foo1"),
    )

    assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
        "class SchemaWithAnnotatedConstraints(BaseModel):\n"
        "    foo: Annotated[conint(lt=2 + 5), Field(description='awaw', alias='foo1')]\n"
    )


def test__schema_field_had_annotated_constrained_field__adding_default_default_should_be_added_outside_of_annotation(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_annotated_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_annotated_constraints.SchemaWithAnnotatedConstraints).field("foo").had(default=2),
    )

    assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
        "class SchemaWithAnnotatedConstraints(BaseModel):\n"
        "    foo: Annotated[conint(lt=2 + 5), Field(description='awaw')] = 2\n"
    )


def test__schema_field_had_annotated_constrained_field__adding_one_other_constraint(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_annotated_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_annotated_constraints.SchemaWithAnnotatedConstraints).field("foo").had(gt=8),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
            "class SchemaWithAnnotatedConstraints(BaseModel):\n"
            "    foo: Annotated[conint(lt=2 + 5), Field(description='awaw', gt=8)]\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
            "class SchemaWithAnnotatedConstraints(BaseModel):\n"
            "    foo: Annotated[conint(lt=2 + 5, gt=8), Field(description='awaw')]\n"
        )


def test__schema_field_had_annotated_constrained_field__adding_another_constraint_and_an_attribute(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_annotated_constraints: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_annotated_constraints.SchemaWithAnnotatedConstraints).field("foo").had(gt=8, alias="foo1"),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
            "class SchemaWithAnnotatedConstraints(BaseModel):\n"
            "    foo: Annotated[conint(lt=2 + 5), Field(description='awaw', alias='foo1', gt=8)]\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithAnnotatedConstraints) == (
            "class SchemaWithAnnotatedConstraints(BaseModel):\n"
            "    foo: Annotated[conint(lt=2 + 5, gt=8), Field(description='awaw', alias='foo1')]\n"
        )


def test__schema_field_had_constrained_field__schema_has_special_constraints_constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
        from pydantic import BaseModel, constr, Field

        MY_VAR = "hewwo"

        class SchemaWithSpecialConstraints(BaseModel):
            foo: constr(to_upper=True)

        """,
    )
    v1 = create_runtime_schemas(
        schema(latest.SchemaWithSpecialConstraints).field("foo").had(max_length=8),
    )
    if PYDANTIC_V2:
        assert inspect.getsource(v1.SchemaWithSpecialConstraints) == (
            "class SchemaWithSpecialConstraints(BaseModel):\n    foo: constr(to_upper=True) = Field(max_length=8)\n"
        )
    else:
        assert inspect.getsource(v1.SchemaWithSpecialConstraints) == (
            "class SchemaWithSpecialConstraints(BaseModel):\n    foo: constr(to_upper=True, max_length=8)\n"
        )


def test__schema_field_had_constrained_field__schema_has_special_constraints_constraints_have_been_modified__pydantic2(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    if not PYDANTIC_V2:
        pytest.skip("This test is only for Pydantic 2")

    latest = head_module_for(
        """
        from pydantic import BaseModel, Field, StringConstraints
        from typing import Annotated

        MY_VAR = "hewwo"

        class SchemaWithSpecialConstraints(BaseModel):
            foo: Annotated[str, StringConstraints(to_upper=True)]

        """,
    )
    v1 = create_runtime_schemas(
        schema(latest.SchemaWithSpecialConstraints).field("foo").had(max_length=8),
    )

    assert inspect.getsource(v1.SchemaWithSpecialConstraints) == (
        "class SchemaWithSpecialConstraints(BaseModel):\n"
        "    foo: Annotated[str, StringConstraints(to_upper=True)] = Field(max_length=8)\n"
    )
    assert v1.SchemaWithSpecialConstraints(foo="hewwo").foo == "HEWWO"
    with pytest.raises(ValidationError):
        assert v1.SchemaWithSpecialConstraints(foo="sdwdwewdwd").foo


@pytest.fixture()
def head_with_var(head_module_for: HeadModuleFor):
    return head_module_for(
        """
        from pydantic import BaseModel, constr, Field

        MY_VAR = 83

        class SchemaWithVar(BaseModel):
            foo: int = Field(default=MY_VAR, description='Hello darkness my old friend')

        """,
    )


def test__schema_field_had__field_has_var_in_ast_and_keyword_was_added(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_var: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_var.SchemaWithVar).field("foo").had(alias="bar"),
    )

    assert inspect.getsource(v1.SchemaWithVar) == (
        "class SchemaWithVar(BaseModel):\n"
        "    foo: int = Field(default=MY_VAR, description='Hello darkness my old friend', alias='bar')\n"
    )


def test__schema_field_had__field_has_var_in_ast_and_existing_keyword_was_changed(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_var: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_var.SchemaWithVar).field("foo").had(description="Hello sunshine my old friend"),
    )

    assert inspect.getsource(v1.SchemaWithVar) == (
        "class SchemaWithVar(BaseModel):\n"
        "    foo: int = Field(default=MY_VAR, description='Hello sunshine my old friend')\n"
    )


def test__schema_field_had__field_has_var_in_ast_and_keyword_with_var_was_changed(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_var: Any,
):
    v1 = create_runtime_schemas(
        schema(head_with_var.SchemaWithVar).field("foo").had(default=128),
    )

    assert inspect.getsource(v1.SchemaWithVar) == (
        "class SchemaWithVar(BaseModel):\n"
        "    foo: int = Field(default=128, description='Hello darkness my old friend')\n"
    )


@pytest.fixture()
def head_with_var_instead_of_field(head_module_for: HeadModuleFor):
    return head_module_for(
        """
        from pydantic import BaseModel, constr, Field

        MY_VAR = 83

        class SchemaWithVarInsteadOfField(BaseModel):
            foo: int = MY_VAR

        """,
    )


def test__schema_field_had__field_has_var_instead_of_field_and_keyword_was_added(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_var_instead_of_field,
):
    v1 = create_runtime_schemas(
        schema(head_with_var_instead_of_field.SchemaWithVarInsteadOfField)
        .field("foo")
        .had(description="Hello darkness my old friend"),
    )
    assert inspect.getsource(v1.SchemaWithVarInsteadOfField) == (
        "class SchemaWithVarInsteadOfField(BaseModel):\n"
        "    foo: int = Field(default=MY_VAR, description='Hello darkness my old friend')\n"
    )


def test__schema_field_had__field_has_var_instead_of_field_and_keyword_with_var_was_changed(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_var_instead_of_field,
):
    v1 = create_runtime_schemas(
        schema(head_with_var_instead_of_field.SchemaWithVarInsteadOfField).field("foo").had(default=128),
    )

    assert inspect.getsource(v1.SchemaWithVarInsteadOfField) == (
        "class SchemaWithVarInsteadOfField(BaseModel):\n    foo: int = Field(default=128)\n"
    )


def test__schema_field_had__default_factory(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    v1 = create_runtime_schemas(  # pragma: no branch
        schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").had(default_factory=lambda: "mew"),
    )

    assert v1.SchemaWithOneStrField().foo == "mew"


def test__schema_field_had__type(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    v1 = create_runtime_schemas(
        schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").had(type=bytes),
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    foo: bytes\n"


def test__schema_field_had_name(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    v1 = create_runtime_schemas(
        schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").had(name="doo"),
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    doo: str\n"


@pytest.fixture()
def latest_module_with_weird_types(head_module_for: HeadModuleFor):
    return head_module_for(
        """
from pydantic import Field, BaseModel
from typing_extensions import Literal
from enum import Enum, auto
def my_default_factory():
    raise NotImplementedError


class MyEnum(Enum):
    baz = auto()


class ModelWithWeirdFields(BaseModel):
    foo: dict = Field(default={"a": "b"})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: Literal[MyEnum.baz]
""",
    )


def test__schema_field_had__with_pre_existing_weird_data_types(
    create_runtime_schemas: CreateRuntimeSchemas,
    latest_module_with_weird_types,
):
    v1 = create_runtime_schemas(
        schema(latest_module_with_weird_types.ModelWithWeirdFields).field("bad").existed_as(type=int),
    )

    assert inspect.getsource(v1.ModelWithWeirdFields) == (
        "class ModelWithWeirdFields(BaseModel):\n"
        "    foo: dict = Field(default={'a': 'b'})\n"
        "    bar: list[int] = Field(default_factory=my_default_factory)\n"
        "    baz: Literal[MyEnum.baz]\n"
        "    bad: int\n"
    )


def test__schema_field_had__with_weird_data_types__with_all_fields_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
    latest_module_with_weird_types,
):
    v1 = create_runtime_schemas(
        schema(latest_module_with_weird_types.ModelWithWeirdFields).field("foo").had(description="..."),
        schema(latest_module_with_weird_types.ModelWithWeirdFields).field("bar").had(description="..."),
        schema(latest_module_with_weird_types.ModelWithWeirdFields).field("baz").had(description="..."),
    )

    assert inspect.getsource(v1.ModelWithWeirdFields) == (
        "class ModelWithWeirdFields(BaseModel):\n"
        "    foo: dict = Field(default={'a': 'b'}, description='...')\n"
        "    bar: list[int] = Field(default_factory=my_default_factory, description='...')\n"
        "    baz: Literal[MyEnum.baz] = Field(description='...')\n"
    )


def test__union_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
from pydantic import BaseModel

class EmptySchema(BaseModel):
    pass

class SchemaWithUnionFields(BaseModel):
    foo: int | str
    bar: EmptySchema | None

""",
    )
    v1 = create_runtime_schemas(
        schema(latest.SchemaWithUnionFields).field("baz").existed_as(type=int | latest.EmptySchema),
        schema(latest.SchemaWithUnionFields).field("daz").existed_as(type=Union[int, latest.EmptySchema]),
    )

    assert inspect.getsource(v1.SchemaWithUnionFields) == (
        "class SchemaWithUnionFields(BaseModel):\n"
        "    foo: int | str\n"
        "    bar: EmptySchema | None\n"
        "    baz: typing.Union[int, EmptySchema]\n"
        "    daz: typing.Union[int, EmptySchema]\n"
    )


def test__schema_that_overrides_fields_from_mro(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
from pydantic import BaseModel, Field

class ParentSchema(BaseModel):
    foo: int = Field(default=83)
    bar: int = Field(default=83)

class SchemaThatOverridesField(ParentSchema):
    foo: str = Field(description="What?")
    bar: str = Field(description="What?")

""",
    )
    v1 = create_runtime_schemas(
        schema(latest.SchemaThatOverridesField).field("foo").had(type=bytes),
        schema(latest.SchemaThatOverridesField).field("bar").had(alias="baz"),
    )

    assert inspect.getsource(v1.SchemaThatOverridesField)


def test__schema_field_existed_as__already_existing_field__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a field "foo" to "SchemaWithOneStrField" in '
            '"MyVersionChange" but there is already a field with that name.',
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").existed_as(type=int),
        )


def test__schema_field_didnt_exist__field_is_missing__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "bar" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("bar").didnt_exist,
        )


def test__schema_field_didnt_have__using_incorrect_attribute__should_raise_error():
    with pytest.raises(
        CadwynStructureError,
        match=re.escape("Unknown attribute 'defaults'. Are you sure it's a valid field attribute?"),
    ):
        schema(BaseModel).field("foo").didnt_have("defaults")  # pyright: ignore[reportArgumentType]


def test__schema_field_didnt_have__removing_nonexistent_attribute__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete the attribute "description" of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already doesn\'t have that attribute.'
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").didnt_have("description"),
        )


def test__schema_field_had_name__name_is_the_same_as_before__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already has that name.',
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").had(name="foo"),
        )


def test__schema_field_had__change_to_the_same_field_type__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "foo" to "<class \'str\'>" from'
            ' "SchemaWithOneStrField" in "MyVersionChange" but it already has type "<class \'str\'>"',
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("foo").had(type=str),
        )


def test__schema_field_had__change_attr_to_same_value__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_module_for,
):
    latest = head_module_for(
        """
    from pydantic import BaseModel, Field
    class SchemaWithOneStrField(BaseModel):
        foo: str = Field(default="wow")
    """,
    )
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "default" of field "foo" from "SchemaWithOneStrField" to \'wow\' '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            schema(latest.SchemaWithOneStrField).field("foo").had(default="wow"),
        )


def test__schema_field_had__change_metadata_attr_to_same_value__should_raise_error(
    create_local_versioned_packages: CreateLocalVersionedPackages,
    head_with_empty_classes,
):
    if not PYDANTIC_V2:
        pytest.skip("This test is only for Pydantic 2")

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "gt" of field "foo" from "EmptySchema" to 8 '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_local_versioned_packages(
            version_change(schema(head_with_empty_classes.EmptySchema).field("foo").had(gt=8)),
            version_change(
                schema(head_with_empty_classes.EmptySchema).field("foo").existed_as(type=int, info=Field(gt=8, lt=12)),
            ),
        )


def test__schema_field_had__nonexistent_field__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    head_with_one_str_field: _FakeNamespaceWithOneStrField,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the field "boo" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(
            schema(head_with_one_str_field.SchemaWithOneStrField).field("boo").had(type=int),
        )
