import re
from typing import Annotated, Any, Generic, TypeVar, Union, cast

import pytest
from annotated_types import Ge, Le, MaxLen
from pydantic import BaseModel, Field, ValidationError

from cadwyn import VersionedAPIRouter, schema
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import PossibleFieldAttributes
from tests.conftest import (
    CreateRuntimeSchemas,
    CreateVersionedClients,
    assert_models_are_equal,
    version_change,
)
from tests.test_schema_generation.test_schema_field import assert_field_had_changes_apply

BoolT = TypeVar("BoolT", bound=Union[bool, int])


class GenericSchema(BaseModel, Generic[BoolT]):
    foo: BoolT
    bar: bool


class ParametrizedSchema(GenericSchema[bool]):
    pass


def test__generate_versioned_routers__generic_metaclass_adds_a_base__should_preserve_responses(
    create_versioned_clients: CreateVersionedClients, router: VersionedAPIRouter
):
    class Mixin:
        pass

    class Meta(type(BaseModel)):
        def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
            if Mixin not in bases:
                bases = (*bases, Mixin)
            return super().__new__(mcs, name, bases, namespace, **kwargs)

    class Model(BaseModel, Generic[BoolT], metaclass=Meta):
        value: BoolT

    router.get("/", response_model=Model[int])(lambda: {"value": 42})
    clients = create_versioned_clients(version_change())

    for client in clients.values():
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"value": 42}


def test__generate_versioned_routers__metaclass_parent_has_overridden_forward_ref__should_preserve_responses(
    create_versioned_clients: CreateVersionedClients, router: VersionedAPIRouter
):
    class Parent(BaseModel):
        value: "NotDefined"  # noqa: F821  # ty: ignore[unresolved-reference]  # Intentionally incomplete; child overrides.

    class Meta(type(BaseModel)):
        def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
            if not any(issubclass(base, Parent) for base in bases):
                bases = (Parent, *bases)
            return super().__new__(mcs, name, bases, namespace, **kwargs)

    class Model(BaseModel, Generic[BoolT], metaclass=Meta):
        value: BoolT

    router.get("/", response_model=Model[int])(lambda: {"value": 42})
    clients = create_versioned_clients(version_change())

    for client in clients.values():
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"value": 42}


class GenericMixin(Generic[BoolT]):
    pass


class MixinAddedByMetaclass:
    pass


class MetaclassThatAppendsMixin(type(BaseModel)):
    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
        return super().__new__(mcs, name, (*bases, MixinAddedByMetaclass), namespace, **kwargs)


class ModelWithGenericAliasBaseAndAppendingMetaclass(BaseModel, GenericMixin[int], metaclass=MetaclassThatAppendsMixin):
    value: int = 42


def test__schema_generation__metaclass_appends_base_to_generic_alias_bases__should_keep_bases(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change())

    old_model = schemas["2000-01-01"][ModelWithGenericAliasBaseAndAppendingMetaclass]
    assert old_model.__bases__ == ModelWithGenericAliasBaseAndAppendingMetaclass.__bases__
    assert old_model().model_dump() == {"value": 42}


class FirstParentWithValue(BaseModel):
    value: int = 1


class SecondParentWithValue(BaseModel):
    value: int = 2


class MetaclassThatReversesModelBases(type(BaseModel)):
    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
        models = tuple(base for base in bases if issubclass(base, BaseModel))
        others = tuple(base for base in bases if not issubclass(base, BaseModel))
        return super().__new__(mcs, name, (*reversed(models), *others), namespace, **kwargs)


class ModelWithGenericAliasBaseAndReversingMetaclass(
    FirstParentWithValue, SecondParentWithValue, GenericMixin[int], metaclass=MetaclassThatReversesModelBases
):
    pass


def test__schema_generation__metaclass_reorders_generic_alias_bases__should_keep_bases(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change())

    old = schemas["2000-01-01"]
    old_model = old[ModelWithGenericAliasBaseAndReversingMetaclass]
    assert old_model.__bases__ == (old[SecondParentWithValue], old[FirstParentWithValue], GenericMixin)
    assert old_model().model_dump() == {"value": 2}


class ParentWithFooThatGetsRenamed(BaseModel):
    foo: int


class AliasOfParentWithFooThatGetsRenamed:
    def __mro_entries__(self, bases: tuple[object, ...]) -> tuple[type, ...]:
        return (ParentWithFooThatGetsRenamed,)


class ChildOfAliasAndGenericAlias(AliasOfParentWithFooThatGetsRenamed(), GenericMixin[BoolT]):
    pass


def test__schema_field_had_name__child_of_mro_entries_base_and_generic_alias__should_rename_inherited_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFooThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    old_child = schemas["2000-01-01"][ChildOfAliasAndGenericAlias]
    assert old_child.__bases__ == (schemas["2000-01-01"][ParentWithFooThatGetsRenamed], GenericMixin)
    assert old_child.__parameters__ == (BoolT,)
    assert old_child.model_validate({"old_foo": 1}).model_dump() == {"old_foo": 1}
    assert schemas["2001-01-01"][ChildOfAliasAndGenericAlias].model_validate({"foo": 1}).model_dump() == {"foo": 1}


ATTR_MAP: tuple[tuple[str, str], list[tuple[PossibleFieldAttributes, Any]]] = (
    ("attr", "value"),
    [
        ("title", "Foo"),
        ("description", "A foo"),
        ("repr", False),
    ],
)

###########
# GENERIC #
###########


def test__schema_field_existed_as__field_is_typevar(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema)
            .field("baz")
            .existed_as(
                type=BoolT,
            ),
        ),
    )

    class ExpectedSchema(BaseModel, Generic[BoolT]):
        foo: BoolT
        bar: bool

        baz: BoolT

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


def test__schema_field_didnt_exist__field_is_typevar(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema).field("foo").didnt_exist,
        ),
    )

    class ExpectedSchema(BaseModel):
        bar: bool

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_had__modifying_typevar_field(
    attr: PossibleFieldAttributes,
    value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    assert_field_had_changes_apply(
        GenericSchema,
        attr,
        value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_didnt_have__modifying_typevar_field(
    attr: PossibleFieldAttributes, value: Any, create_runtime_schemas: CreateRuntimeSchemas
):
    class GenericSchemaWithMetadata(BaseModel, Generic[BoolT]):
        foo: Annotated[BoolT, Field(**dict(ATTR_MAP[1]))]
        bar: bool

    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchemaWithMetadata).field("foo").didnt_have(attr),
        )
    )

    assert not schemas["2000-01-01"][GenericSchemaWithMetadata].model_fields["foo"].metadata


def test__schema_field_had__field_type_is_narrowed(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema)
            .field("bar")
            .had(
                type=BoolT,
            ),
        ),
    )

    class ExpectedSchema(BaseModel, Generic[BoolT]):
        foo: BoolT
        bar: BoolT

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


def test__schema_had_description__generic_model__should_only_change_description_of_generic_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(GenericSchema).had(description="Old generic description")))

    assert schemas["2000-01-01"][GenericSchema].__doc__ == "Old generic description"
    assert schemas["2000-01-01"][GenericSchema].model_json_schema()["description"] == "Old generic description"
    assert "description" not in schemas["2001-01-01"][GenericSchema].model_json_schema()
    # Like in pydantic, the subclasses of the generic model do not inherit its description
    assert "description" not in schemas["2000-01-01"][ParametrizedSchema].model_json_schema()


################
# PARAMETRIZED #
################


def test__schema_field_didnt_exist__parametrized_field_is_missing__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "foo" from "ParametrizedSchema" '
            'in "MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(
            version_change(
                schema(ParametrizedSchema).field("foo").didnt_exist,
            ),
        )


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_had__modifying_parametrized_field(
    attr: str,
    value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    assert_field_had_changes_apply(
        ParametrizedSchema,
        attr,
        value,
        create_runtime_schemas,
    )


def test__schema_field_had__field_parametrized_type_is_replaced(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParametrizedSchema)
            .field("foo")
            .had(
                type=int,
            ),
        ),
    )

    class ExpectedSchema(GenericSchema[int]):
        pass

    assert_models_are_equal(schemas["2000-01-01"][ParametrizedSchema], ExpectedSchema)


def test__schema_had_description__subclass_of_parametrized_model__should_change_its_description(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParametrizedSchema).had(description="Old parametrized description"))
    )

    assert schemas["2000-01-01"][ParametrizedSchema].model_json_schema()["description"] == (
        "Old parametrized description"
    )
    assert "description" not in schemas["2000-01-01"][GenericSchema].model_json_schema()
    assert_models_are_equal(schemas["2000-01-01"][ParametrizedSchema], ParametrizedSchema)


################################
# INHERITED FIELDS OF GENERICS #
################################

FirstT = TypeVar("FirstT")
SecondT = TypeVar("SecondT")


class GenericPair(BaseModel, Generic[FirstT, SecondT]):
    first: FirstT
    second: SecondT


class StrIntPair(GenericPair[str, int]):
    pass


class OtherStrIntPair(GenericPair[str, int]):
    pass


class PairBox(BaseModel, Generic[FirstT]):
    pair: GenericPair[FirstT, int]


class NonGenericParent(BaseModel):
    name: str
    count: int = 0


class GenericChildOfNonGenericParent(NonGenericParent, Generic[FirstT]):
    payload: FirstT


class NonGenericSiblingOfGenericChild(NonGenericParent):
    pass


def _get_constraints(model: type[BaseModel], field_name: str) -> list[Any]:
    return model.model_fields[field_name].metadata


def test__schema_field_had__inherited_field_of_subclass_of_parametrization__should_not_change_parents_and_siblings(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericPair).field("first").had(description="First"),
            schema(StrIntPair).field("second").had(ge=5),
        )
    )

    # Before #466, the change of StrIntPair also applied to the generic model, its parametrization and its siblings
    assert _get_constraints(schemas["2000-01-01"][StrIntPair], "second") == [Ge(5)]
    for model in (GenericPair, GenericPair[str, int], OtherStrIntPair):
        assert _get_constraints(schemas["2000-01-01"][model], "second") == []


def test__schema_field_had__inherited_field_of_parametrization__should_change_it_and_its_subclasses_only(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericPair).field("first").had(description="First"),
            schema(GenericPair[str, int]).field("second").had(ge=5),
            schema(StrIntPair).field("second").had(le=100),
        )
    )

    old_schemas = schemas["2000-01-01"]
    for model in (GenericPair[str, int], OtherStrIntPair):
        assert _get_constraints(old_schemas[model], "second") == [Ge(5)]
    assert _get_constraints(old_schemas[StrIntPair], "second") == [Ge(5), Le(100)]
    for model in (GenericPair, GenericPair[int, int]):
        assert _get_constraints(old_schemas[model], "second") == []
    # The instructions of the parametrization also apply where other generics build it (#420), but not the ones of
    # its subclasses
    pair_in_box = cast("type[BaseModel]", old_schemas[PairBox[str]].model_fields["pair"].annotation)
    assert _get_constraints(pair_in_box, "second") == [Ge(5)]
    assert old_schemas[PairBox[str]].model_validate({"pair": {"first": "a", "second": 500}}).pair.second == 500
    with pytest.raises(ValidationError):
        old_schemas[PairBox[str]].model_validate({"pair": {"first": "a", "second": 1}})


def test__schema_field_had__inherited_field_of_generic_subclass__should_change_its_parametrizations_only(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(NonGenericParent).field("count").had(description="Count"),
            schema(GenericChildOfNonGenericParent).field("name").had(max_length=3),
        )
    )

    old_schemas = schemas["2000-01-01"]
    for model in (GenericChildOfNonGenericParent, GenericChildOfNonGenericParent[int]):
        assert _get_constraints(old_schemas[model], "name") == [MaxLen(3)]
    for model in (NonGenericParent, NonGenericSiblingOfGenericChild):
        assert _get_constraints(old_schemas[model], "name") == []
