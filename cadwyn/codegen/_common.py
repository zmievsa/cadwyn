import ast
import dataclasses
import inspect
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, Protocol, TypeAlias, TypeVar, cast

from pydantic import BaseModel
from typing_extensions import Self

from cadwyn._compat import PydanticFieldWrapper, model_fields
from cadwyn._package_utils import IdentifierPythonPath
from cadwyn.exceptions import CodeGenerationError
from cadwyn.structure.versions import Version

from ._asts import _ValidatorWrapper, get_validator_info_or_none

_FieldName: TypeAlias = str
_CodegenPluginASTType = TypeVar("_CodegenPluginASTType", bound=ast.AST)


@dataclasses.dataclass(slots=True)
class PydanticModelWrapper:
    cls: type[BaseModel]
    name: str
    fields: dict[_FieldName, PydanticFieldWrapper]
    validators: dict[_FieldName, _ValidatorWrapper]
    _parents: list[Self] | None = dataclasses.field(init=False, default=None)

    def _get_parents(self, schemas: "dict[IdentifierPythonPath, Self]"):
        if self._parents is not None:
            return self._parents
        parents = []
        for base in self.cls.mro()[1:]:
            schema_path = f"{base.__module__}.{base.__name__}"

            if schema_path in schemas:
                parents.append(schemas[schema_path])
            elif issubclass(base, BaseModel):
                fields, validators = get_fields_and_validators_from_model(base)
                parents.append(type(self)(base, base.__name__, fields, validators))
        self._parents = parents
        return parents

    def _get_defined_fields_through_mro(
        self, schemas: "dict[IdentifierPythonPath, Self]"
    ) -> dict[str, PydanticFieldWrapper]:
        fields = {}

        for parent in reversed(self._get_parents(schemas)):
            fields |= parent.fields

        return fields | self.fields

    def _get_defined_annotations_through_mro(self, schemas: "dict[IdentifierPythonPath, Self]") -> dict[str, Any]:
        annotations = {}

        for parent in reversed(self._get_parents(schemas)):
            annotations |= parent.cls.__annotations__

        return annotations | self.cls.__annotations__


@cache
def get_fields_and_validators_from_model(
    cls: type,
) -> tuple[dict[_FieldName, PydanticFieldWrapper], dict[_FieldName, _ValidatorWrapper]]:
    if not isinstance(cls, type) or not issubclass(cls, BaseModel):
        raise CodeGenerationError(f"Model {cls} is not a subclass of BaseModel")

    fields = model_fields(cls)
    try:
        source = inspect.getsource(cls)
    except OSError:
        return (
            {
                field_name: PydanticFieldWrapper(annotation=field.annotation, init_model_field=field)
                for field_name, field in fields.items()
            },
            {},
        )
    else:
        cls_ast = cast(ast.ClassDef, ast.parse(source).body[0])
        validators: dict[str, _ValidatorWrapper] = {}

        validators_and_nones = (
            get_validator_info_or_none(node)
            for node in cls_ast.body
            if isinstance(node, ast.FunctionDef) and node.decorator_list
        )
        validators = {validator.func_ast.name: validator for validator in validators_and_nones if validator is not None}

        return (
            {
                node.target.id: PydanticFieldWrapper(
                    annotation=fields[node.target.id].annotation,
                    init_model_field=fields[node.target.id],
                    annotation_ast=node.annotation,
                    value_ast=node.value,
                )
                for node in cls_ast.body
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in fields
            },
            validators,
        )


@dataclass(slots=True)
class _EnumWrapper:
    cls: type[Enum]
    members: dict[_FieldName, Any]


@dataclass(slots=True)
class _ModuleWrapper:
    value: ModuleType
    extra_imports: list[ast.Import | ast.ImportFrom] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(slots=True, kw_only=True)
class GlobalCodegenContext:
    current_version: Version
    latest_version: Version = dataclasses.field(init=False)
    versions: list[Version]
    schemas: dict[IdentifierPythonPath, PydanticModelWrapper] = dataclasses.field(repr=False)
    enums: dict[IdentifierPythonPath, _EnumWrapper] = dataclasses.field(repr=False)
    modules: dict[IdentifierPythonPath, _ModuleWrapper] = dataclasses.field(repr=False)
    extra: dict[str, Any]

    def __post_init__(self):
        self.latest_version = max(self.versions, key=lambda v: v.value)

    @property
    def current_version_is_latest(self):
        return self.latest_version == self.current_version


@dataclasses.dataclass(slots=True, kw_only=True)
class CodegenContext(GlobalCodegenContext):
    # This attribute is extremely useful for calculating relative imports
    index_of_latest_package_dir_in_module_python_path: int
    module_python_path: str
    module_path: Path
    template_module: ModuleType
    all_names_defined_on_toplevel_of_file: dict[IdentifierPythonPath, str]


class CodegenPlugin(Protocol, Generic[_CodegenPluginASTType]):
    @property
    def node_type(self) -> type[_CodegenPluginASTType]:
        raise NotImplementedError

    @staticmethod
    def __call__(node: _CodegenPluginASTType, context: CodegenContext) -> _CodegenPluginASTType:
        raise NotImplementedError


class MigrationPlugin(Protocol):
    @staticmethod
    def __call__(context: GlobalCodegenContext) -> None:
        raise NotImplementedError
