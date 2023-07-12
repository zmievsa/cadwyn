import ast
import importlib
import inspect
import os
import shutil
import sys
from collections.abc import Sequence
from copy import deepcopy
from datetime import date
from enum import Enum, auto
from pathlib import Path
from types import GenericAlias, LambdaType, ModuleType
from typing import (
    Any,
    TypeAlias,
    _BaseGenericAlias,  # pyright: ignore[reportGeneralTypeIssues]
    get_args,
    get_origin,
)

from pydantic import BaseConfig, BaseModel
from pydantic.fields import FieldInfo as PydanticFieldInfo
from pydantic.fields import ModelField
from pydantic.typing import convert_generics
from typing_extensions import assert_never

from universi.structure.enums import AlterEnumSubInstruction, EnumDidntHaveMembersInstruction, EnumHadMembersInstruction
from universi.structure.schemas import (
    AlterSchemaInstruction,
    OldSchemaDidntHaveField,
    OldSchemaFieldWas,
    OldSchemaHadField,
)
from universi.structure.versions import Version, Versions

from ._utils import Sentinel
from .exceptions import CodeGenerationError, InvalidGenerationInstructionError
from .fields import FieldInfo

LambdaFunctionName = (lambda: None).__name__  # pragma: no branch
FieldNameT: TypeAlias = str
dict_of_empty_field_info = {k: getattr(PydanticFieldInfo(), k) for k in PydanticFieldInfo.__slots__}


# TODO: Add enum alteration here
def regenerate_dir_to_all_versions(template_module: ModuleType, versions: Versions):
    schemas = {k: (v, _get_fields_for_model(v)) for k, v in deepcopy(versions.versioned_schemas).items()}
    enums = {k: (v, {member.name: member.value for member in v}) for k, v in deepcopy(versions.versioned_enums).items()}

    for version in versions.versions:
        # NOTE: You'll have to use relative imports

        _generate_versioned_directory(template_module, schemas, enums, version.date)
        _apply_migrations(version, schemas, enums)

    current_package = template_module.__name__
    while current_package != "":
        importlib.reload(sys.modules[current_package])
        current_package = ".".join(current_package.split(".")[:-1])


def _apply_migrations(
    version: Version,
    schemas: dict[
        str,
        tuple[type[BaseModel], dict[FieldNameT, tuple[type[BaseModel], ModelField]]],
    ],
    enums: dict[str, tuple[type[Enum], dict[str, Any]]],
):
    for version_change in version.version_changes:
        _apply_alter_schema_instructions(schemas, version_change.alter_schema_instructions)
        _apply_alter_enum_instructions(enums, version_change.alter_enum_instructions)


def _apply_alter_schema_instructions(
    schemas: dict[str, tuple[type[BaseModel], dict[FieldNameT, tuple[type[BaseModel], ModelField]]]],
    alter_schema_instructions: Sequence[AlterSchemaInstruction],
):
    for alter_schema_instruction in alter_schema_instructions:
        schema = alter_schema_instruction.schema
        schema_path = schema.__module__ + schema.__name__
        schema_field_info_bundle = schemas[schema_path]
        field_name_to_field_model = schema_field_info_bundle[1]
        for field_change in alter_schema_instruction.changes:
            if isinstance(field_change, OldSchemaDidntHaveField):
                # TODO: Check that the user doesn't pop it and change it at the same time
                # TODO: Add a check that field actually exists (it's very necessary!)
                field_name_to_field_model.pop(field_change.field_name)

            elif isinstance(field_change, OldSchemaFieldWas):
                # TODO: Add a check that field actually exists (it's very necessary!)
                model_field = field_name_to_field_model[field_change.field_name][1]
                if field_change.type is not Sentinel:
                    if model_field.annotation == field_change.type:
                        raise InvalidGenerationInstructionError(
                            f"You tried to change the type of field '{field_change.field_name}' to '{field_change.type}' in {schema.__name__} but it already has type '{model_field.annotation}'",
                        )
                    model_field.annotation = field_change.type
                    model_field.type_ = convert_generics(field_change.type)
                    model_field.outer_type_ = field_change.type
                field_info = model_field.field_info

                if not isinstance(field_info, FieldInfo):
                    dict_of_field_info = {k: getattr(field_info, k) for k in field_info.__slots__}
                    if dict_of_field_info == dict_of_empty_field_info:
                        field_info = FieldInfo()
                        model_field.field_info = field_info
                    else:
                        raise InvalidGenerationInstructionError(
                            f"You have defined a Field using pydantic.fields.Field but you must use universi.Field in {schema.__name__}",
                        )
                for attr_name in field_change.field_changes.__dataclass_fields__:
                    attr_value = getattr(field_change.field_changes, attr_name)
                    if attr_value is not Sentinel:
                        setattr(field_info, attr_name, attr_value)
                        field_info._universi_field_names.add(attr_name)
            elif isinstance(field_change, OldSchemaHadField):
                field_name_to_field_model[field_change.field_name] = (
                    schema,
                    ModelField(
                        name=field_change.field_name,
                        type_=field_change.type,
                        field_info=field_change.field,
                        class_validators=None,
                        model_config=BaseConfig,
                    ),
                )
            else:
                assert_never(field_change)


def _apply_alter_enum_instructions(
    enums: dict[str, tuple[type[Enum], dict[str, Any]]],
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = alter_enum_instruction.enum
        enum_path = enum.__module__ + enum.__name__
        enum_member_to_value = enums[enum_path]
        if isinstance(alter_enum_instruction, EnumDidntHaveMembersInstruction):
            for member in alter_enum_instruction.members:
                if member not in enum_member_to_value[1]:
                    raise InvalidGenerationInstructionError(
                        f"Enum member '{member}' was not found in enum '{enum_path}'",
                    )
                enum_member_to_value[1].pop(member)
        elif isinstance(alter_enum_instruction, EnumHadMembersInstruction):
            for member, member_value in alter_enum_instruction.members.items():
                if member in enum_member_to_value[1] and enum_member_to_value[1][member] == member_value:
                    raise InvalidGenerationInstructionError(
                        f"Enum member '{member}' already exists in enum '{enum_path}' with the same value",
                    )
                else:
                    enum_member_to_value[1][member] = member_value
        else:
            assert_never(alter_enum_instruction)


def _get_versioned_schema_dir_name(template_module: ModuleType, version: date) -> Path:
    template_dir = _get_package_path_from_module(template_module)

    version_as_str = version.isoformat().replace("-", "_")
    return template_dir.with_name("v" + version_as_str)


def _get_package_path_from_module(template_module: ModuleType) -> Path:
    file = inspect.getsourcefile(template_module)

    # I am too lazy to reproduce this error correctly
    if file is None:  # pragma: no cover
        raise CodeGenerationError(f"Module {template_module} has no source file")
    file = Path(file)
    if not file.name == "__init__.py":
        raise CodeGenerationError(f"Module {template_module} is not a package")
    return file.parent


def _generate_versioned_directory(
    template_module: ModuleType,
    schemas: dict[
        str,
        tuple[type[BaseModel], dict[FieldNameT, tuple[type[BaseModel], ModelField]]],
    ],
    enums: dict[str, tuple[type[Enum], dict[str, Any]]],
    version: date,
):
    assert template_module.__file__ is not None
    dir = _get_package_path_from_module(template_module)
    version_dir = _get_versioned_schema_dir_name(template_module, version)
    version_dir.mkdir(exist_ok=True)
    # [universi, structure, schemas]
    template_module_python_path_parts = template_module.__name__.split(".")
    # [home, foo, bar, universi, structure, schemas]
    template_module_path_parts = Path(template_module.__file__).parent.parts
    # [home, foo, bar] = [home, foo, bar, universi, structure, schemas][:-3]
    root_module_path = Path(*template_module_path_parts[: -len(template_module_python_path_parts)])
    for subroot, dirnames, filenames in os.walk(dir):
        original_subroot = Path(subroot)
        versioned_subroot = version_dir / original_subroot.relative_to(dir)
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for dirname in dirnames:
            (versioned_subroot / dirname).mkdir(exist_ok=True)
        for filename in filenames:
            original_file = (original_subroot / filename).absolute()
            versioned_file = (versioned_subroot / filename).absolute()
            print(original_file)

            if filename.endswith(".py"):
                module_path = ".".join(
                    original_file.relative_to(root_module_path).with_suffix("").parts,
                )
                module = importlib.import_module(module_path)
                new_module_text = _modify_module(module, schemas, enums)
                versioned_file.write_text(new_module_text)
            else:
                shutil.copyfile(original_file, versioned_file)


def _get_fields_for_model(model: type[BaseModel]) -> dict[FieldNameT, tuple[type[BaseModel], ModelField]]:
    actual_fields: dict[FieldNameT, tuple[type[BaseModel], ModelField]] = {}
    for cls in model.__mro__:
        if cls is BaseModel:
            return actual_fields
        if not issubclass(cls, BaseModel):
            continue
        for field_name, field in cls.__fields__.items():
            if field_name not in actual_fields and field_name in cls.__annotations__:
                actual_fields[field_name] = (cls, field)
    else:
        raise CodeGenerationError(f"Model {model} is not a subclass of BaseModel")


def _modify_module(
    module,
    modified_schemas: dict[
        str,
        tuple[type[BaseModel], dict[str, tuple[type[BaseModel], ModelField]]],
    ],
    modified_enums: dict[str, tuple[type[Enum], dict[str, Any]]],
) -> str:
    try:
        parsed_file = ast.parse(inspect.getsource(module))
    except OSError as e:
        path = Path(module.__file__)
        if path.is_file() and path.read_text() == "":
            return ""
        # Not sure how to get here so this is just a precaution
        raise CodeGenerationError("Failed to get source code for module") from e  # pragma: no cover

    if module.__name__.endswith(".__init__"):
        module_name = module.__name__.removesuffix(".__init__")
    else:
        module_name = module.__name__
    body = ast.Module(
        [
            ast.ImportFrom(module="universi", names=[ast.alias(name="Field")], level=0),
            ast.Import(names=[ast.alias(name="typing")], level=0),
        ]
        + [
            _modify_cls(n, module_name, modified_schemas, modified_enums) if isinstance(n, ast.ClassDef) else n
            for n in parsed_file.body
        ],
        [],
    )

    return ast.unparse(body)


def _modify_cls(
    cls_node: ast.ClassDef,
    module_python_path: str,
    modified_schemas: dict[
        str,
        tuple[type[BaseModel], dict[str, tuple[type[BaseModel], ModelField]]],
    ],
    modified_enums: dict[str, tuple[type[Enum], dict[str, Any]]],
) -> ast.ClassDef:
    cls_python_path = module_python_path + cls_node.name
    if cls_python_path in modified_schemas:
        cls_node = _modify_schema_cls(cls_node, modified_schemas[cls_python_path][1])
    if cls_python_path in modified_enums:
        cls_node = _modify_enum_cls(cls_node, modified_enums[cls_python_path][1])

    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node


def _modify_schema_cls(
    cls_node: ast.ClassDef,
    actual_fields: dict[str, tuple[type[BaseModel], ModelField]],
) -> ast.ClassDef:
    body = [
        ast.AnnAssign(
            target=ast.Name(id=name, ctx=ast.Store()),
            annotation=ast.Name(id=custom_repr(field[1].annotation), ctx=ast.Load()),
            value=ast.Call(
                func=ast.Name(id="Field", ctx=ast.Load()),
                args=[],
                keywords=[
                    ast.keyword(
                        arg=attr,
                        value=ast.Name(
                            id=custom_repr(getattr(field[1].field_info, attr)),
                        ),
                    )
                    # TODO: We should lint the code to make sure that the user is not using pydantic.fields.Field instead of universi.Field
                    for attr in getattr(field[1].field_info, "_universi_field_names", ())
                ],
            ),
            simple=1,
        )
        for name, field in actual_fields.items()
    ]
    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Pass | ast.Ellipsis)]
    docstring = _pop_docstring_from_cls_body(old_body)
    cls_node.body = docstring + body + old_body

    return cls_node


def _modify_enum_cls(cls_node: ast.ClassDef, enum_info: dict[str, Any]) -> ast.ClassDef:
    new_body = [
        ast.Assign(
            targets=[ast.Name(id=member, ctx=ast.Store())],
            value=ast.Name(id=custom_repr(member_value)),
            lineno=0,
        )
        for member, member_value in enum_info.items()
    ]

    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Ellipsis)]
    docstring = _pop_docstring_from_cls_body(old_body)

    cls_node.body = docstring + new_body + old_body
    return cls_node


def _pop_docstring_from_cls_body(old_body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        len(old_body) > 0
        and isinstance(old_body[0], ast.Expr)
        and isinstance(old_body[0].value, ast.Constant)
        and isinstance(old_body[0].value.value, str)
    ):
        return [old_body.pop(0)]
    else:
        return []


# The following is based on by Samuel Colvin's devtools


def custom_repr(value: Any) -> Any:
    if isinstance(value, list | tuple | set | frozenset):
        return PlainRepr(value.__class__(map(custom_repr, value)))
    if isinstance(value, dict):
        return PlainRepr(value.__class__((custom_repr(k), custom_repr(v)) for k, v in value.items()))
    if isinstance(value, _BaseGenericAlias | GenericAlias):
        return f"{custom_repr(get_origin(value))}[{', '.join(custom_repr(a) for a in get_args(value))}]"
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, Enum):
        return PlainRepr(f"{value.__class__.__name__}.{value.name}")
    if isinstance(value, auto):
        return PlainRepr("auto()")
    if isinstance(value, LambdaType) and LambdaFunctionName == value.__name__:
        return _find_a_lambda(inspect.getsource(value).strip())
    if inspect.isfunction(value):
        return PlainRepr(value.__name__)
    else:
        return PlainRepr(repr(value))


class PlainRepr(str):
    """
    String class where repr doesn't include quotes.
    """

    def __repr__(self) -> str:
        return str(self)


def _find_a_lambda(source: str) -> str:
    found_lambdas: list[ast.Lambda] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "default_factory" and isinstance(node.value, ast.Lambda):
            found_lambdas.append(node.value)
    if len(found_lambdas) == 1:
        return ast.unparse(found_lambdas[0])
    # These two errors are really hard to cover. Not sure if even possible, honestly :)
    elif len(found_lambdas) == 0:  # pragma: no cover
        raise InvalidGenerationInstructionError(
            f"No lambda found in default_factory even though one was passed: {source}",
        )
    else:  # pragma: no cover
        raise InvalidGenerationInstructionError("More than one lambda found in default_factory. This is not supported.")
