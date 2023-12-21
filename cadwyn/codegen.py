import ast
from collections.abc import Collection, Sequence
from copy import deepcopy
from types import ModuleType
from typing import (
    Any,
    final,
)

import ast_comments
from typing_extensions import assert_never

from cadwyn._codegen.asts import (
    add_keyword_to_call,
    get_ast_keyword_from_argument_name_and_value,
    get_fancy_repr,
    pop_docstring_from_cls_body,
)
from cadwyn._codegen.common import (
    CodegenContext,
    GlobalCodegenContext,
    PydanticModelWrapper,
    _EnumWrapper,
    get_fields_from_model,
)
from cadwyn._codegen.main import (
    CodegenPlugin,
    MigrationPlugin,
    generate_versioned_directories,
)
from cadwyn._compat import (
    PYDANTIC_V2,
    FieldInfo,
    PydanticFieldWrapper,
    dict_of_empty_field_info,
    get_attrs_that_are_not_from_field_and_that_are_from_field,
    is_pydantic_constrained_type,
)
from cadwyn._package_utils import (
    IdentifierPythonPath,
    get_absolute_python_path_of_import,
    get_cls_pythonpath,
)
from cadwyn.structure.enums import (
    AlterEnumSubInstruction,
    EnumDidntHaveMembersInstruction,
    EnumHadMembersInstruction,
)
from cadwyn.structure.schemas import (
    AlterSchemaInstruction,
    AlterSchemaSubInstruction,
    OldSchemaFieldDidntExist,
    OldSchemaFieldExistedWith,
    OldSchemaFieldHad,
)
from cadwyn.structure.versions import VersionBundle

from ._utils import Sentinel
from .exceptions import InvalidGenerationInstructionError

_extra_imports: list[tuple[str, str]] = [
    ("typing", "import typing"),
    ("Any", "from typing import Any"),
    ("Annotated", "from typing import Annotated"),
    ("Field", "from pydantic import Field"),
    ("conbytes", "from pydantic import conbytes"),
    ("conlist", "from pydantic import conlist"),
    ("conset", "from pydantic import conset"),
    ("constr", "from pydantic import constr"),
    ("conint", "from pydantic import conint"),
    ("confloat", "from pydantic import confloat"),
    ("condecimal", "from pydantic import condecimal"),
    ("condate", "from pydantic import condate"),
]


if PYDANTIC_V2:
    _extra_imports.extend(
        [
            ("confrozenset", "from pydantic import conset"),
            ("StringConstraints", "from pydantic import StringConstraints"),
            ("StrictBool", "from pydantic import StrictBool"),
            ("StrictBytes", "from pydantic import StrictBytes"),
            ("StrictFloat", "from pydantic import StrictFloat"),
            ("StrictInt", "from pydantic import StrictInt"),
            ("StrictStr", "from pydantic import StrictStr"),
        ],
    )

_rendered_extra_imports = [(seek_str, ast.parse(imp).body[0]) for seek_str, imp in _extra_imports]


@final
class _ImportedModule:
    __slots__ = (
        "path",
        "name",
        "alias",
        "absolute_python_path_to_origin",
        "how_far_up_is_base_schema_dir_from_current_module",
        "is_package",
    )

    def __init__(
        self,
        version_dir: str,
        import_pythonpath_template: str,
        package_name: str,
        how_far_up_is_base_schema_dir_from_current_module: int,
        absolute_python_path_template: str,
        is_package: bool,
    ) -> None:
        self.path = import_pythonpath_template.format(version_dir)
        self.name = package_name.format(version_dir)
        if self.path == "":
            self.alias = self.name
        else:
            self.alias = f"{self.path.replace('.', '_')}_{self.name}"
        self.absolute_python_path_to_origin = absolute_python_path_template.format("latest")
        self.how_far_up_is_base_schema_dir_from_current_module = how_far_up_is_base_schema_dir_from_current_module
        self.is_package = is_package

    def get_ast(self) -> ast.ImportFrom:
        module = f"{self.path}.{self.name}"
        name = ast.alias(name="*")
        level = self.how_far_up_is_base_schema_dir_from_current_module
        # TODO: Add a testcase where is_package == True and level == 3
        if self.is_package and level == 2:
            level -= 1
        return ast.ImportFrom(
            level=level,
            module=module,
            names=[name],
        )


def change_model(
    model: PydanticModelWrapper,
    alter_schema_instruction: AlterSchemaInstruction,
    version_change_name: str,
):
    # We only handle names right now so we just go ahead and check
    if alter_schema_instruction.name == model.name:
        raise InvalidGenerationInstructionError(
            f'You tried to change the name of "{model.name}" in "{version_change_name}" '
            "but it already has the name you tried to assign.",
        )
    model.name = alter_schema_instruction.name


def add_field_to_model(
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
    alter_schema_instruction: OldSchemaFieldExistedWith,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields(schemas)
    if alter_schema_instruction.field_name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.field_name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    model.fields[alter_schema_instruction.field_name] = PydanticFieldWrapper(
        annotation_ast=None,  # TODO: Get this from migration
        annotation=alter_schema_instruction.type,
        init_model_field=alter_schema_instruction.field,
        import_from=alter_schema_instruction.import_from,
        import_as=alter_schema_instruction.import_as,
        field_ast=None,  # TODO: Get this from migration
    )


def change_field_in_model(
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
    alter_schema_instruction: OldSchemaFieldHad,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields(schemas)
    if alter_schema_instruction.field_name not in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to change the type of field "{alter_schema_instruction.field_name}" from '
            f'"{model.name}" in "{version_change_name}" but it doesn\'t have such a field.',
        )

    field = defined_fields[alter_schema_instruction.field_name]
    model.fields[alter_schema_instruction.field_name] = field

    current_field_is_constrained_type = is_pydantic_constrained_type(field.annotation)
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type:
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.field_name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type

        field.annotation_ast = None
        if current_field_is_constrained_type:
            field.field_ast = None

    if alter_schema_instruction.new_name is not Sentinel:
        if alter_schema_instruction.new_name == alter_schema_instruction.field_name:
            raise InvalidGenerationInstructionError(
                f'You tried to change the name of field "{alter_schema_instruction.field_name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already has that name.",
            )
        model.fields[alter_schema_instruction.new_name] = model.fields.pop(
            alter_schema_instruction.field_name,
        )

    field_info = field.field_info

    dict_of_field_info = {k: getattr(field_info, k) for k in field_info.__slots__}
    if dict_of_field_info == dict_of_empty_field_info:
        field_info = FieldInfo()
        field.field_info = field_info
    for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
        attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
        if attr_value is not Sentinel:
            if field.passed_field_attributes.get(attr_name, Sentinel) == attr_value:
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.field_name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )

            if hasattr(field.annotation, attr_name) and current_field_is_constrained_type:
                setattr(field.annotation, attr_name, attr_value)
                ann_ast = field.annotation_ast
                if ann_ast is not None and isinstance(ann_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, ann_ast)
                else:
                    field.field_ast = None
                    field.annotation_ast = None
            else:
                field.update_attribute(name=attr_name, value=attr_value)
                field_ast = field.field_ast
                if isinstance(field_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, field_ast)
                else:
                    field.field_ast = None


def delete_field_from_model(model: PydanticModelWrapper, field_name: str, version_change_name: str):
    if field_name not in model.fields:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )
    model.fields.pop(field_name)


##########################
# BEWARE. EXPERIMENTS HERE
##########################


def generate_code_for_versioned_packages(
    template_module: ModuleType,
    versions: VersionBundle,
    *,
    ignore_coverage_for_latest_aliases: bool = True,
):
    """
    Args:
        template_module: The latest package from which we will generate the versioned packages
        versions: Version bundle to generate versions from
        ignore_coverage_for_latest_aliases: Add a pragma: no cover comment to the star imports in the generated
        version of the latest module.
    """

    generate_versioned_directories(
        template_module,
        versions=list(versions),
        schemas={
            k: PydanticModelWrapper(v, v.__name__, get_fields_from_model(v))
            for k, v in deepcopy(versions.versioned_schemas).items()
        },
        enums={
            k: _EnumWrapper(v, {member.name: member.value for member in v})
            for k, v in deepcopy(versions.versioned_enums).items()
        },
        extra_context={"ignore_coverage_for_latest_aliases": ignore_coverage_for_latest_aliases},
        codegen_plugins=[CodegenPlugin(call=_migrate_module_to_another_version, node_type=ast.Module)],
        migration_plugins=[MigrationPlugin(call=_apply_migrations)],
    )


# TODO: Get rid of import as. Instead, add a separate instruction for it
def _migrate_module_to_another_version(
    template_module: ast.Module,
    context: CodegenContext,
):
    if context.version_is_latest:
        return _get_alias_of_template_module(template_module, context)
    else:
        return _get_template_package_migrated_to_concrete_non_latest_version(template_module, context)


def _get_template_package_migrated_to_concrete_non_latest_version(
    template_module: ast.Module,
    context: CodegenContext,
):
    extra_field_imports = [
        ast.ImportFrom(
            module=field.import_from,
            names=[ast.alias(name=get_fancy_repr(field.annotation).strip("'"), asname=field.import_as)],
            level=0,
        )
        for val in context.schemas.values()
        for field in val.fields.values()
        if field.import_from is not None
    ]

    new_parsed_file = ast.Module(
        extra_field_imports + [_migrate_ast_node_to_another_version(n, context) for n in template_module.body],
        type_ignores=[],
    )

    modified_source = ast.unparse(new_parsed_file)
    extra_lib_imports = [
        import_
        for seek_str, import_ in _rendered_extra_imports
        if seek_str in modified_source and seek_str not in context.all_names_defined_on_toplevel_of_file
    ]

    new_parsed_file.body = extra_lib_imports + new_parsed_file.body
    return new_parsed_file


def _get_alias_of_template_module(
    template_module: ast.Module,
    context: CodegenContext,
):
    imports = _prepare_imports_from_version_dirs(
        context.template_module,
        ["latest"],
        context.index_of_latest_schema_dir_in_module_python_path,
    )

    import_text = ast.unparse(imports[0].get_ast()) + " # noqa: F403"
    if context.extra["ignore_coverage_for_latest_aliases"]:
        import_text += " # pragma: no cover"
    return ast_comments.parse(import_text)


def _prepare_imports_from_version_dirs(
    original_module: ModuleType,
    version_dir_names: Collection[str],
    index_of_latest_schema_dir_in_pythonpath: int,
) -> list[_ImportedModule]:
    # package.latest                     -> from .. import latest
    # package.latest.module              -> from ...latest import module
    # package.latest.subpackage          -> from ...latest import subpackage
    # package.latest.subpackage.module   -> from ....subpackage import module

    # package.latest                    -> from ..latest import *
    # package.latest.module             -> from ..latest.module import *
    # package.latest.subpackage         -> from ...latest.subpackage import *
    # package.latest.subpackage.module  -> from ...latest.subpackage.module import *

    original_module_parts = original_module.__name__.split(".")
    original_module_parts[index_of_latest_schema_dir_in_pythonpath] = "{}"
    how_far_up_is_base_schema_dir_from_current_module = (
        len(original_module_parts) - index_of_latest_schema_dir_in_pythonpath
    )
    is_package = original_module_parts[-1] == "__init__"
    if is_package:
        original_module_parts.pop(-1)

    package_name = original_module_parts[-1]
    package_path = original_module_parts[index_of_latest_schema_dir_in_pythonpath:-1]
    import_pythonpath_template = ".".join(package_path)
    absolute_python_path_template = ".".join(original_module_parts)
    return [
        _ImportedModule(
            version_dir,
            import_pythonpath_template,
            package_name,
            how_far_up_is_base_schema_dir_from_current_module,
            absolute_python_path_template,
            is_package,
        )
        for version_dir in version_dir_names
    ]


def _apply_migrations(context: GlobalCodegenContext):
    for version_change in context.version.version_changes:
        _apply_alter_schema_instructions(
            context.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
        )
        _apply_alter_enum_instructions(
            context.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )


def _apply_alter_schema_instructions(
    modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
    alter_schema_instructions: Sequence[AlterSchemaSubInstruction | AlterSchemaInstruction],
    version_change_name: str,
):
    # TODO: If we have a request migration for an endpoint instead of a schema and we haven't found that endpoint
    # during codegen -- raise an error or maybe add an argument that controlls that. Or maybe this is overengineering..
    for alter_schema_instruction in alter_schema_instructions:
        schema = alter_schema_instruction.schema
        schema_path = get_cls_pythonpath(schema)
        mutable_schema_info = modified_schemas[schema_path]
        if isinstance(alter_schema_instruction, OldSchemaFieldDidntExist):
            delete_field_from_model(mutable_schema_info, alter_schema_instruction.field_name, version_change_name)
        elif isinstance(alter_schema_instruction, OldSchemaFieldHad):
            change_field_in_model(
                mutable_schema_info,
                modified_schemas,
                alter_schema_instruction,
                version_change_name,
            )
        elif isinstance(alter_schema_instruction, OldSchemaFieldExistedWith):
            add_field_to_model(mutable_schema_info, modified_schemas, alter_schema_instruction, version_change_name)
        elif isinstance(alter_schema_instruction, AlterSchemaInstruction):
            change_model(mutable_schema_info, alter_schema_instruction, version_change_name)
        else:
            assert_never(alter_schema_instruction)


def _apply_alter_enum_instructions(
    enums: dict[IdentifierPythonPath, _EnumWrapper],
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
    version_change_name: str,
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = alter_enum_instruction.enum
        enum_path = get_cls_pythonpath(enum)
        enum = enums[enum_path]
        if isinstance(alter_enum_instruction, EnumDidntHaveMembersInstruction):
            for member in alter_enum_instruction.members:
                if member not in enum.members:
                    raise InvalidGenerationInstructionError(
                        f'You tried to delete a member "{member}" from "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but it doesn\'t have such a member.',
                    )
                enum.members.pop(member)
        elif isinstance(alter_enum_instruction, EnumHadMembersInstruction):
            for member, member_value in alter_enum_instruction.members.items():
                if member in enum.members and enum.members[member] == member_value:
                    raise InvalidGenerationInstructionError(
                        f'You tried to add a member "{member}" to "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but there is already a member with that name and value.',
                    )
                enum.members[member] = member_value
        else:
            assert_never(alter_enum_instruction)


def _migrate_ast_node_to_another_version(
    node: ast.stmt,
    context: CodegenContext,
):
    if isinstance(node, ast.ClassDef):
        return _migrate_cls_to_another_version(node, context)
    elif isinstance(node, ast.ImportFrom):
        python_path = get_absolute_python_path_of_import(node, context.module_python_path)
        node.names = [
            name
            if (name_path := f"{python_path}.{name.name}") not in context.schemas
            else ast.alias(name=context.schemas[name_path].name, asname=name.asname)
            for name in node.names
        ]

    return node


def _migrate_cls_to_another_version(
    cls_node: ast.ClassDef,
    context: CodegenContext,
) -> ast.ClassDef:
    cls_python_path = f"{context.module_python_path}.{cls_node.name}"

    if cls_python_path in context.schemas:
        cls_node = _modify_schema_cls(cls_node, context.schemas, cls_python_path)
    elif cls_python_path in context.enums:
        cls_node = _modify_enum_cls(cls_node, context.enums[cls_python_path])

    if not cls_node.body:
        cls_node.body = [ast.Pass()]

    ast_renamer = _AnnotationASTNodeTransformerWithSchemaRenaming(
        context.schemas,
        context.all_names_defined_on_toplevel_of_file,
        context.module_python_path,
    )
    return ast_renamer.visit(ast.parse(ast.unparse(cls_node)).body[0])


# TODO: Make sure that cadwyn doesn't remove OLD property definitions
def _modify_schema_cls(
    cls_node: ast.ClassDef,
    modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
    cls_python_path: str,
) -> ast.ClassDef:
    model_info = modified_schemas[cls_python_path]
    # This is for possible schema renaming
    cls_node.name = model_info.name

    field_definitions = [
        ast.AnnAssign(
            target=ast.Name(name, ctx=ast.Store()),
            annotation=ast.Name(get_fancy_repr(field.render_annotation())),
            value=_generate_field_ast(field),
            simple=1,
        )
        for name, field in model_info.fields.items()
    ]

    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)]
    docstring = pop_docstring_from_cls_body(old_body)
    cls_node.body = docstring + field_definitions + old_body
    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node


def _generate_field_ast(field: PydanticFieldWrapper):
    if field.field_ast is not None:
        return field.field_ast
    passed_attrs = field.passed_field_attributes
    # TODO: This is None check feels buggy
    if is_pydantic_constrained_type(field.annotation) and field.annotation_ast is None:
        (
            attrs_that_are_only_in_contype,
            attrs_that_are_only_in_field,
        ) = get_attrs_that_are_not_from_field_and_that_are_from_field(field.annotation)
        if not attrs_that_are_only_in_contype:
            passed_attrs |= attrs_that_are_only_in_field
    if passed_attrs:
        return ast.Call(
            func=ast.Name("Field"),
            args=[],
            keywords=[
                get_ast_keyword_from_argument_name_and_value(attr, attr_value)
                for attr, attr_value in passed_attrs.items()
            ],
        )
    return None


def _modify_enum_cls(cls_node: ast.ClassDef, enum: _EnumWrapper) -> ast.ClassDef:
    new_body = [
        ast.Assign(
            targets=[ast.Name(member, ctx=ast.Store())],
            value=ast.Name(get_fancy_repr(member_value)),
            lineno=0,
        )
        for member, member_value in enum.members.items()
    ]

    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)]
    docstring = pop_docstring_from_cls_body(old_body)

    cls_node.body = docstring + new_body + old_body
    return cls_node


class _AnnotationASTNodeTransformerWithSchemaRenaming(ast.NodeTransformer):
    def __init__(
        self,
        modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
        all_names_in_module: dict[str, str],
        module_python_path: str,
    ):
        super().__init__()
        self.modified_schemas = modified_schemas
        self.module_python_path = module_python_path
        self.all_names_in_module = all_names_in_module

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        return self._get_name(node, node.id)

    def _get_name(self, node: ast.AST, name: str):
        model_info = self.modified_schemas.get(f"{self.all_names_in_module.get(name, self.module_python_path)}.{name}")
        if model_info is not None:
            return ast.Name(model_info.name)
        return node
