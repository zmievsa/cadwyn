import inspect
import re
from copy import deepcopy
from functools import partial
from typing import Any, Literal, Union

import pytest
from pydantic import BaseModel, Field, ValidationError, constr
from pydantic.fields import FieldInfo
from pydantic_core.core_schema import ModelSchema

from cadwyn._compat import PYDANTIC_V2, model_fields
from cadwyn.exceptions import (
    CadwynStructureError,
    InvalidGenerationInstructionError,
)
from cadwyn.structure import schema
from tests.conftest import (
    CreateLocalVersionedPackages,
    CreateRuntimeSchemas,
    HeadModuleFor,
    _FakeModuleWithEmptyClasses,
    _FakeNamespaceWithOneStrField,
    version_change,
)
from tests.runtime_gen import base_schemas


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
    del schema_to_modify["ref"]
    del schema_to_modify["schema"]["model_name"]
    del schema_to_modify["config"]["title"]
    return serialize_object(schema)


def serialize_object(obj: Any):
    if isinstance(obj, dict):
        return {k: v.__name__ if callable(v) else serialize_object(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        modified_list = []
        for v in obj:
            if callable(v):
                while hasattr(v, "func"):
                    v = v.func
                v = v.__name__
            modified_list.append(serialize_object(v))
        return modified_list
    else:
        return obj


@pytest.fixture()
def head_with_one_int_field(head_module_for: HeadModuleFor):
    return head_module_for(
        """
    from pydantic import BaseModel
    class SchemaWithOneIntField(BaseModel):
        foo: int
    """,
    )


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


class SchemaWithOneIntField(BaseModel):
    foo: int


##############
# EXISTED AS #
##############


def test__schema_field_existed_as__original_schema_is_empty(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(EmptySchema).field("bar").existed_as(type=int, info=Field(alias="boo")))
    )

    class ExpectedSchema(BaseModel):
        bar: int = Field(alias="boo")

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][EmptySchema])


def test__field_existed_as__original_schema_has_a_field(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField)
            .field("bar")
            .existed_as(type=int, info=Field(description="Hello darkness my old friend")),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str
        bar: int = Field(description="Hello darkness my old friend")

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaWithOneStrField])


def test__field_existed_as__extras_are_added(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(EmptySchema)
            .field("foo")
            .existed_as(
                type=int,
                info=Field(deflolbtt="hewwo"),  # pyright: ignore[reportCallIssue]
            ),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: int = Field(json_schema_extra={"deflolbtt": "hewwo"})

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][EmptySchema])


def test__schema_field_existed_as__with_default_none(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(EmptySchema).field("foo").existed_as(type=str | None, info=Field(default=None)),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str | None = Field(default=None)

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][EmptySchema])


def test__schema_field_existed_as__with_new_weird_data_types(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(EmptySchema).field("foo").existed_as(type=dict[str, int], info=Field(default={"a": "b"})),
            schema(EmptySchema).field("bar").existed_as(type=list[int], info=Field(default_factory=lambda: 83)),
            schema(EmptySchema).field("baz").existed_as(type=Literal[base_schemas.MyEnum.foo]),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: dict[str, int] = Field(default={"a": "b"})
        bar: list[int] = Field(default_factory=lambda: 83)
        baz: Literal[base_schemas.MyEnum.foo]

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][EmptySchema])


################
# DIDN'T EXIST #
################


def test__NOTE_TO_ADD_THIS_TEST_TO_ROUTER_GENERATION_NOT_JUST_HERE_ADD_A_TEST_THAT_CHECKS_THAT_WHEN_PARENT_SCHEMA_IS_ALTERED__ALL_OF_ITS_CHILDREN_GET_ALTERED_AS_WELL__BECAUSE_WITH_RUNTIME_GENERATION_THE_SIMPLE_APPROACH_OF_JUST_REBUILDING_THE_SCHEMAS_MENTIONED_IN_VERSION_CHANGES_NO_LONGER_WORKS():
    assert False


def test__schema_field_didnt_exist(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).field("foo").didnt_exist,
        )
    )

    class ExpectedSchema(BaseModel):
        pass

    schemas["2000-01-01"][SchemaWithOneStrField]()
    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaWithOneStrField])


class ParentSchema(BaseModel):
    foo: str


class ChildSchema(BaseModel):
    pass


def test__schema_field_didnt_exist__with_inheritance(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(ParentSchema).field("foo").didnt_exist),
        version_change(schema(ChildSchema).field("bar").existed_as(type=int)),
    )

    class ExpectedChildSchema(
        schemas["2000-01-01"][ParentSchema],  # pyright: ignore[reportUntypedBaseClass, reportGeneralTypeIssues]
    ):
        bar: int

    assert_models_are_equal(EmptySchema, schemas["2000-01-01"][ParentSchema])
    assert_models_are_equal(ExpectedChildSchema, schemas["2000-01-01"][ChildSchema])
    assert set(schemas["2000-01-01"][ChildSchema].model_fields) == {"bar"}


def test__schema_field_didnt_exist__with_inheritance_and_child_not_versioned__child_must_still_change(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(ParentSchema).field("foo").didnt_exist))

    assert_models_are_equal(EmptySchema, schemas["2000-01-01"][ParentSchema])
    assert "foo" not in schemas["2000-01-01"][ChildSchema].model_fields
    assert schemas["2000-01-01"][ChildSchema]().json() == "{}"


#######
# HAD #
#######


def assert_field_had_changes_apply(
    model: type[BaseModel],
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(model).field("foo").had(**{attr: attr_value})))

    field_info = model_fields(schemas["2000-01-01"][model])["foo"]
    if attr in FieldInfo.metadata_lookup:
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
    attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas
):
    """This test is here to guarantee that we can handle all parameter types we provide"""

    assert_field_had_changes_apply(
        SchemaWithOneIntField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("min_length", 20),
        ("max_length", 50),
    ],
)
def test__schema_field_had__str_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    assert_field_had_changes_apply(
        SchemaWithOneStrField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


def test__schema_field_had__pattern(create_runtime_schemas: CreateRuntimeSchemas):
    if PYDANTIC_V2:
        attr_name = "pattern"
    else:
        attr_name = "regex"
    assert_field_had_changes_apply(
        SchemaWithOneStrField,
        attr_name,
        r"hewwo darkness",
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("max_digits", 12),
        ("decimal_places", 15),
    ],
)
def test__schema_field_had__decimal_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    from decimal import Decimal

    class SchemaWithOneDecimalField(BaseModel):
        foo: Decimal

    assert_field_had_changes_apply(
        SchemaWithOneDecimalField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [("exclude", [16, 17, 18])],
)
def test__schema_field_had__list_of_int_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithOneListOfIntField(BaseModel):
        foo: list[int]

    assert_field_had_changes_apply(
        SchemaWithOneListOfIntField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


def test__schema_field_had__float_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithOneFloatField(BaseModel):
        foo: float

    assert_field_had_changes_apply(
        SchemaWithOneFloatField,
        "allow_inf_nan",
        attr_value=False,
        create_runtime_schemas=create_runtime_schemas,
    )


def test__schema_field_didnt_have__removing_default(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithDefaults(BaseModel):
        foo: str = "hewwo"
        bar: int = Field(default=83)

    v1 = create_runtime_schemas(
        version_change(
            schema(SchemaWithDefaults).field("foo").didnt_have("default"),
            schema(SchemaWithDefaults).field("bar").didnt_have("default"),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str
        bar: int

    assert_models_are_equal(ExpectedSchema, v1["2000-01-01"][SchemaWithDefaults])


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


def test__schema_field_had__default_factory(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(  # pragma: no branch
        version_change(schema(SchemaWithOneStrField).field("foo").had(default_factory=lambda: "mew"))
    )

    assert schemas["2000-01-01"][SchemaWithOneStrField]().foo == "mew"


def test__schema_field_had__type(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(type=bytes)))

    class ExpectedSchema(BaseModel):
        foo: bytes

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaWithOneStrField])


def test__schema_field_had_name(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(name="doo")))

    class ExpectedSchema(BaseModel):
        doo: str

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaWithOneStrField])


from enum import Enum, auto

from typing_extensions import Literal


def my_default_factory():
    raise NotImplementedError


class MyEnum(Enum):
    baz = auto()


class ModelWithWeirdFields(BaseModel):
    foo: dict = Field(default={"a": "b"})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: Literal[MyEnum.baz]


def test__schema_field_had__with_pre_existing_weird_data_types(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(ModelWithWeirdFields).field("bad").existed_as(type=int)))

    class ExpectedSchema(BaseModel):
        foo: dict = Field(default={"a": "b"})
        bar: list[int] = Field(default_factory=my_default_factory)
        baz: Literal[MyEnum.baz]
        bad: int

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][ModelWithWeirdFields])


def test__schema_field_had__with_weird_data_types__with_all_fields_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ModelWithWeirdFields).field("foo").had(description="..."),
            schema(ModelWithWeirdFields).field("bar").had(description="..."),
            schema(ModelWithWeirdFields).field("baz").had(description="..."),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: dict = Field(default={"a": "b"}, description="...")
        bar: list[int] = Field(default_factory=my_default_factory, description="...")
        baz: Literal[MyEnum.baz] = Field(description="...")

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][ModelWithWeirdFields])


def test__union_fields(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithUnionFields(BaseModel):
        foo: int | str
        bar: EmptySchema | None

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithUnionFields).field("baz").existed_as(type=int | EmptySchema),
            schema(SchemaWithUnionFields).field("daz").existed_as(type=Union[int, EmptySchema]),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: int | str
        bar: EmptySchema | None
        baz: int | EmptySchema
        daz: int | EmptySchema

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaWithUnionFields])


def test__schema_that_overrides_fields_from_mro(create_runtime_schemas: CreateRuntimeSchemas):
    class ParentSchema(BaseModel):
        foo: int = Field(default=83)
        bar: int = Field(default=83)

    class SchemaThatOverridesField(ParentSchema):
        foo: str = Field(description="What?")  # pyright: ignore
        bar: str = Field(description="What?")  # pyright: ignore

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaThatOverridesField).field("foo").had(type=bytes),
            schema(SchemaThatOverridesField).field("bar").had(alias="baz"),
        )
    )

    class ExpectedSchema(
        schemas["2000-01-01"][ParentSchema],  # pyright: ignore[reportUntypedBaseClass, reportGeneralTypeIssues]
    ):
        foo: bytes = Field(description="What?")
        bar: str = Field(description="What?", alias="baz")

    assert_models_are_equal(ExpectedSchema, schemas["2000-01-01"][SchemaThatOverridesField])


def test__schema_field_existed_as__already_existing_field__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a field "foo" to "SchemaWithOneStrField" in '
            '"MyVersionChange" but there is already a field with that name.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").existed_as(type=int)))


def test__schema_field_didnt_exist__field_is_missing__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "bar" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("bar").didnt_exist))


def test__schema_field_didnt_have__using_incorrect_attribute__should_raise_error():
    with pytest.raises(
        CadwynStructureError,
        match=re.escape("Unknown attribute 'defaults'. Are you sure it's a valid field attribute?"),
    ):
        version_change(schema(BaseModel).field("foo").didnt_have("defaults"))  # pyright: ignore[reportArgumentType]


def test__schema_field_didnt_have__removing_nonexistent_attribute__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete the attribute "description" of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already doesn\'t have that attribute.'
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_have("description")))


def test__schema_field_had_name__name_is_the_same_as_before__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already has that name.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(name="foo")),
        )


def test__schema_field_had__change_to_the_same_field_type__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "foo" to "<class \'str\'>" from'
            ' "SchemaWithOneStrField" in "MyVersionChange" but it already has type "<class \'str\'>"',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(type=str)),
        )


def test__schema_field_had__change_attr_to_same_value__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithOneStrField(BaseModel):
        foo: str = Field(default="wow")

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "default" of field "foo" from "SchemaWithOneStrField" to \'wow\' '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(default="wow")),
        )


def test__schema_field_had__change_metadata_attr_to_same_value__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "gt" of field "foo" from "EmptySchema" to 8 '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(EmptySchema).field("foo").had(gt=8)),
            version_change(
                schema(EmptySchema).field("foo").existed_as(type=int, info=Field(gt=8, lt=12)),
            ),
        )


def test__schema_field_had__nonexistent_field__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the field "boo" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("boo").had(type=int)))
