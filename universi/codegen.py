import ast
import importlib
import inspect
import os
import shutil
import sys
import textwrap
from collections.abc import Callable, Generator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
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

from universi.structure.enums import (
    AlterEnumSubInstruction,
    EnumDidntHaveMembersInstruction,
    EnumHadMembersInstruction,
)
from universi.structure.schemas import (
    AlterSchemaSubInstruction,
    OldSchemaDidntHaveField,
    OldSchemaFieldWas,
    OldSchemaHadField,
    SchemaPropertyDefinitionInstruction,
    SchemaPropertyDidntExistInstruction,
)
from universi.structure.versions import Version, VersionBundle

from ._utils import Sentinel, get_index_of_base_schema_dir_in_pythonpath
from .exceptions import CodeGenerationError, InvalidGenerationInstructionError
from .fields import FieldInfo

_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch
_FieldName: TypeAlias = str
_PropertyName: TypeAlias = str
_dict_of_empty_field_info = {k: getattr(PydanticFieldInfo(), k) for k in PydanticFieldInfo.__slots__}


@dataclass(slots=True)
class ModelInfo:
    fields: dict[_FieldName, tuple[type[BaseModel], ModelField]]
    properties: dict[_PropertyName, Callable[[Any], Any]] = field(default_factory=dict)


# TODO: Add enum alteration here
def regenerate_dir_to_all_versions(
    template_module: ModuleType,
    versions: VersionBundle,
):
    schemas = {k: ModelInfo(_get_fields_for_model(v)) for k, v in deepcopy(versions.versioned_schemas).items()}
    enums = {k: (v, {member.name: member.value for member in v}) for k, v in deepcopy(versions.versioned_enums).items()}

    for version in versions.versions:
        # NOTE: You'll have to use relative imports

        _generate_versioned_directory(template_module, schemas, enums, version.date)
        _apply_migrations(version, schemas, enums)
    _generate_union_directory(template_module, versions)

    current_package = template_module.__name__
    while current_package != "":
        importlib.reload(sys.modules[current_package])
        current_package = ".".join(current_package.split(".")[:-1])


def _generate_union_directory(template_module: ModuleType, versions: VersionBundle):
    template_dir = _get_package_path_from_module(template_module)
    union_dir = template_dir.with_name("unions")
    index_of_base_schema_in_pythonpath = get_index_of_base_schema_dir_in_pythonpath(
        template_module,
        union_dir,
    )
    for _, original_module, parallel_file in _generate_parallel_directory(
        template_module,
        union_dir,
    ):
        new_module_text = _get_unionized_version_of_module(
            original_module,
            versions,
            index_of_base_schema_in_pythonpath,
        )
        parallel_file.write_text(new_module_text)


def _get_unionized_version_of_module(
    original_module: ModuleType,
    versions: VersionBundle,
    index_of_base_schema_in_pythonpath: int,
):
    original_module_parts = original_module.__name__.split(".")
    original_module_parts[index_of_base_schema_in_pythonpath] = "{}"

    import_pythonpath_template = (".".join(original_module_parts)).removesuffix(
        ".__init__",
    )
    imported_modules = [
        import_pythonpath_template.format(_get_version_dir_name(version.date)) for version in versions.versions
    ]
    imported_modules += [import_pythonpath_template.format("latest")]
    parsed_file = _parse_python_module(original_module)

    body = ast.Module(
        [
            ast.ImportFrom(module="universi", names=[ast.alias(name="Field")], level=0),
            ast.Import(names=[ast.alias(name="typing")], level=0),
            *[ast.Import(names=[ast.Name(module)]) for module in imported_modules],
        ]
        + [
            ast.Name(
                f"\n{node.name}Union: typing.TypeAlias = {' | '.join(f'{module}.{node.name}' for module in imported_modules)}",
            )
            if isinstance(node, ast.ClassDef)
            else node
            for node in parsed_file.body
        ],
        [],
    )

    return ast.unparse(body)


def _apply_migrations(
    version: Version,
    schemas: dict[
        str,
        ModelInfo,
    ],
    enums: dict[str, tuple[type[Enum], dict[str, Any]]],
):
    for version_change in version.version_changes:
        _apply_alter_schema_instructions(
            schemas,
            version_change.alter_schema_instructions,
        )
        _apply_alter_enum_instructions(enums, version_change.alter_enum_instructions)


def _apply_alter_schema_instructions(
    schema_infos: dict[str, ModelInfo],
    alter_schema_instructions: Sequence[AlterSchemaSubInstruction],
):
    for alter_schema_instruction in alter_schema_instructions:
        schema = alter_schema_instruction.schema
        schema_path = schema.__module__ + schema.__name__
        field_name_to_field_model = schema_infos[schema_path].fields
        if isinstance(alter_schema_instruction, OldSchemaDidntHaveField):
            # TODO: Check that the user doesn't pop it and change it at the same time
            # TODO: Add a check that field actually exists (it's necessary!)
            field_name_to_field_model.pop(alter_schema_instruction.field_name)

        elif isinstance(alter_schema_instruction, OldSchemaFieldWas):
            # TODO: Add a check that field actually exists (it's necessary!)
            model_field = field_name_to_field_model[alter_schema_instruction.field_name][1]
            if alter_schema_instruction.type is not Sentinel:
                if model_field.annotation == alter_schema_instruction.type:
                    raise InvalidGenerationInstructionError(
                        f"You tried to change the type of field '{alter_schema_instruction.field_name}' to '{alter_schema_instruction.type}' in {schema.__name__} but it already has type '{model_field.annotation}'",
                    )
                model_field.annotation = alter_schema_instruction.type
                model_field.type_ = convert_generics(alter_schema_instruction.type)
                model_field.outer_type_ = alter_schema_instruction.type
            field_info = model_field.field_info

            if not isinstance(field_info, FieldInfo):
                dict_of_field_info = {k: getattr(field_info, k) for k in field_info.__slots__}
                if dict_of_field_info == _dict_of_empty_field_info:
                    field_info = FieldInfo()
                    model_field.field_info = field_info
                else:
                    raise InvalidGenerationInstructionError(
                        f"You have defined a Field using pydantic.fields.Field but you must use universi.Field in {schema.__name__}",
                    )
            for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
                attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
                if attr_value is not Sentinel:
                    setattr(field_info, attr_name, attr_value)
                    field_info._universi_field_names.add(attr_name)
        elif isinstance(alter_schema_instruction, OldSchemaHadField):
            field_name_to_field_model[alter_schema_instruction.field_name] = (
                schema,
                ModelField(
                    name=alter_schema_instruction.field_name,
                    type_=alter_schema_instruction.type,
                    field_info=alter_schema_instruction.field,
                    class_validators=None,
                    model_config=BaseConfig,
                ),
            )
        elif isinstance(alter_schema_instruction, SchemaPropertyDefinitionInstruction):
            if alter_schema_instruction.name in field_name_to_field_model:
                raise InvalidGenerationInstructionError(
                    f"You tried to define a property '{alter_schema_instruction.name}' in '{schema.__name__}' "
                    "but there is already a field with that name.",
                )
            schema_infos[schema_path].properties[alter_schema_instruction.name] = alter_schema_instruction.function
        elif isinstance(alter_schema_instruction, SchemaPropertyDidntExistInstruction):
            if alter_schema_instruction.name not in schema_infos[schema_path].properties:
                raise InvalidGenerationInstructionError(
                    f"You tried to delete a property '{alter_schema_instruction.name}' in '{schema.__name__}' "
                    "but there is no such property defined in any of the migrations.",
                )
            schema_infos[schema_path].properties.pop(alter_schema_instruction.name)
        else:
            assert_never(alter_schema_instruction)


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


def _get_version_dir_path(template_module: ModuleType, version: date) -> Path:
    template_dir = _get_package_path_from_module(template_module)
    return template_dir.with_name(_get_version_dir_name(version))


def _get_version_dir_name(version: date):
    return "v" + version.isoformat().replace("-", "_")


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
    schemas: dict[str, ModelInfo],
    enums: dict[str, tuple[type[Enum], dict[str, Any]]],
    version: date,
):
    version_dir = _get_version_dir_path(template_module, version)
    for (
        _relative_path_to_file,
        original_module,
        parallel_file,
    ) in _generate_parallel_directory(
        template_module,
        version_dir,
    ):
        new_module_text = _migrate_module_to_another_version(
            original_module,
            schemas,
            enums,
        )
        parallel_file.write_text(new_module_text)


def _generate_parallel_directory(
    template_module: ModuleType,
    parallel_dir: Path,
) -> Generator[tuple[Path, ModuleType, Path], Any, None]:
    assert template_module.__file__ is not None
    dir = _get_package_path_from_module(template_module)
    parallel_dir.mkdir(exist_ok=True)
    # [universi, structure, schemas]
    template_module_python_path_parts = template_module.__name__.split(".")
    # [home, foo, bar, universi, structure, schemas]
    template_module_path_parts = Path(template_module.__file__).parent.parts
    # [home, foo, bar] = [home, foo, bar, universi, structure, schemas][:-3]
    root_module_path = Path(
        *template_module_path_parts[: -len(template_module_python_path_parts)],
    )
    for subroot, dirnames, filenames in os.walk(dir):
        original_subroot = Path(subroot)
        parallel_subroot = parallel_dir / original_subroot.relative_to(dir)
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for dirname in dirnames:
            (parallel_subroot / dirname).mkdir(exist_ok=True)
        for filename in filenames:
            original_file = (original_subroot / filename).absolute()
            parallel_file = (parallel_subroot / filename).absolute()
            print(original_file)

            if filename.endswith(".py"):
                original_module_path = ".".join(
                    original_file.relative_to(root_module_path).with_suffix("").parts,
                )
                original_module = importlib.import_module(original_module_path)
                yield original_subroot.relative_to(dir), original_module, parallel_file
            else:
                shutil.copyfile(original_file, parallel_file)


def _get_fields_for_model(
    model: type[BaseModel],
) -> dict[_FieldName, tuple[type[BaseModel], ModelField]]:
    actual_fields: dict[_FieldName, tuple[type[BaseModel], ModelField]] = {}
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


def _parse_python_module(module: ModuleType) -> ast.Module:
    try:
        return ast.parse(inspect.getsource(module))
    except OSError as e:
        if module.__file__ is None:  # pragma: no cover
            raise CodeGenerationError("Failed to get file path to the module") from e

        path = Path(module.__file__)
        if path.is_file() and path.read_text() == "":
            return ast.Module([])
        # Not sure how to get here so this is just a precaution
        raise CodeGenerationError(
            "Failed to get source code for module",
        ) from e  # pragma: no cover


def _migrate_module_to_another_version(
    module,
    modified_schemas: dict[str, ModelInfo],
    modified_enums: dict[str, tuple[type[Enum], dict[str, Any]]],
) -> str:
    parsed_file = _parse_python_module(module)
    if module.__name__.endswith(".__init__"):
        module_name = module.__name__.removesuffix(".__init__")
    else:
        module_name = module.__name__

    body = ast.Module(
        [
            ast.ImportFrom(module="universi", names=[ast.alias(name="Field")], level=0),
            ast.Import(names=[ast.alias(name="typing")], level=0),
            ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0),
        ]
        + [
            _migrate_cls_to_another_version(
                n,
                module_name,
                modified_schemas,
                modified_enums,
            )
            if isinstance(n, ast.ClassDef)
            else n
            for n in parsed_file.body
        ],
        [],
    )

    return ast.unparse(body)


def _migrate_cls_to_another_version(
    cls_node: ast.ClassDef,
    module_python_path: str,
    modified_schemas: dict[str, ModelInfo],
    modified_enums: dict[str, tuple[type[Enum], dict[str, Any]]],
) -> ast.ClassDef:
    cls_python_path = module_python_path + cls_node.name
    try:
        if cls_python_path in modified_schemas:
            cls_node = _modify_schema_cls(cls_node, modified_schemas[cls_python_path])
        if cls_python_path in modified_enums:
            cls_node = _modify_enum_cls(cls_node, modified_enums[cls_python_path][1])
    except CodeGenerationError as e:  # pragma: no cover # This is just a safeguard that will likely never be triggered
        raise CodeGenerationError(
            f"Failed to migrate class '{cls_node.name}' to an older version.",
        ) from e

    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node


def _modify_schema_cls(
    cls_node: ast.ClassDef,
    model_info: ModelInfo,
) -> ast.ClassDef:
    field_definitions = [
        ast.AnnAssign(
            target=ast.Name(name, ctx=ast.Store()),
            annotation=ast.Name(custom_repr(field[1].annotation)),
            value=ast.Call(
                func=ast.Name("Field"),
                args=[],
                keywords=[
                    ast.keyword(
                        arg=attr,
                        value=ast.Name(
                            custom_repr(_get_field_from_field_info(field[1], attr)),
                        ),
                    )
                    # TODO: We should lint the code to make sure that the user is not using pydantic.fields.Field instead of universi.Field
                    for attr in getattr(
                        field[1].field_info,
                        "_universi_field_names",
                        (),
                    )
                ],
            ),
            simple=1,
        )
        for name, field in model_info.fields.items()
    ]
    property_definitions = [_make_property_ast(name, func) for name, func in model_info.properties.items()]
    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Pass | ast.Ellipsis)]
    docstring = _pop_docstring_from_cls_body(old_body)
    cls_node.body = docstring + field_definitions + old_body + property_definitions

    return cls_node


def _get_field_from_field_info(field: ModelField, attr: str) -> Any:
    field_value = getattr(field.field_info, attr, Sentinel)
    if field_value is Sentinel:
        field_value = field.field_info.extra.get(attr, Sentinel)
    if field_value is Sentinel:  # pragma: no cover # This is just a safeguard that will most likely never be triggered
        raise CodeGenerationError(f"Field '{attr}' is not present in '{field.name}'")
    return field_value


# TODO: Type hint these func definitions everywhere
def _make_property_ast(name: str, func: Callable):
    func_source = inspect.getsource(func)

    func_ast = ast.parse(textwrap.dedent(func_source)).body[0]
    # TODO: What if it's a lambda?
    assert isinstance(func_ast, ast.FunctionDef)
    func_ast.decorator_list = [ast.Name("property")]
    func_ast.name = name
    func_ast.args.args[0].annotation = None
    return func_ast


def _modify_enum_cls(cls_node: ast.ClassDef, enum_info: dict[str, Any]) -> ast.ClassDef:
    new_body = [
        ast.Assign(
            targets=[ast.Name(member, ctx=ast.Store())],
            value=ast.Name(custom_repr(member_value)),
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
        return PlainRepr(
            value.__class__((custom_repr(k), custom_repr(v)) for k, v in value.items()),
        )
    if isinstance(value, _BaseGenericAlias | GenericAlias):
        return f"{custom_repr(get_origin(value))}[{', '.join(custom_repr(a) for a in get_args(value))}]"
    if isinstance(value, type):
        # TODO: Add tests for constrained types
        # TODO: Be wary of this hack when migrating to pydantic v2
        # This is a hack for pydantic's Constrained types
        if value.__name__.startswith("Constrained") and hasattr(value, "__origin__") and hasattr(value, "__args__"):
            return custom_repr(value.__origin__[value.__args__])
        return value.__name__
    if isinstance(value, Enum):
        return PlainRepr(f"{value.__class__.__name__}.{value.name}")
    if isinstance(value, auto):
        return PlainRepr("auto()")
    if isinstance(value, LambdaType) and _LambdaFunctionName == value.__name__:
        # We clean source because getsource() can return only a part of the expression which
        # on its own is not a valid expression such as: "\n  .had(default_factory=lambda: 91)"
        return _find_a_lambda(inspect.getsource(value).strip(" \n\t."))
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

    ast.parse(source)
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
        raise InvalidGenerationInstructionError(
            "More than one lambda found in default_factory. This is not supported.",
        )
