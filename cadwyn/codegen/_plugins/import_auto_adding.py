import ast

from cadwyn._compat import PYDANTIC_V2
from cadwyn.codegen._common import CodegenContext

_extra_imports: list[tuple[str, str]] = [
    ("typing", "import typing"),
    ("pydantic", "import pydantic"),
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
    ("validator", "from pydantic import validator"),
    ("root_validator", "from pydantic import root_validator"),
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
            ("model_validator", "from pydantic import model_validator"),
            ("field_validator", "from pydantic import field_validator"),
        ],
    )

_rendered_extra_imports = [(seek_str, ast.parse(imp).body[0]) for seek_str, imp in _extra_imports]


class ImportAutoAddingPlugin:
    node_type = ast.Module

    @staticmethod
    def __call__(node: ast.Module, context: CodegenContext):
        if context.current_version_is_latest:
            return node
        source = ast.unparse(node)
        extra_lib_imports = [
            import_
            for seek_str, import_ in _rendered_extra_imports
            if seek_str in source and seek_str not in context.all_names_defined_on_toplevel_of_file
        ]
        # We do this because when we import our module, we import not the package but
        # the __init__.py file directly which produces this extra `.__init__` suffix in the name
        module_name = context.template_module.__name__.removesuffix(".__init__")
        if module_name in context.modules:
            manual_imports = context.modules[module_name].extra_imports
        else:
            manual_imports = []

        node.body = extra_lib_imports + manual_imports + node.body

        return node
