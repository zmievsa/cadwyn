import ast

from cadwyn._codegen.common import CodegenContext
from cadwyn._compat import PYDANTIC_V2

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


class ImportAutoAddingPlugin:
    node_type = ast.Module

    @staticmethod
    def __call__(
        node: ast.Module,
        context: CodegenContext,
    ):
        if context.current_version_is_latest:
            return node
        source = ast.unparse(node)
        extra_lib_imports = [
            import_
            for seek_str, import_ in _rendered_extra_imports
            if seek_str in source and seek_str not in context.all_names_defined_on_toplevel_of_file
        ]

        node.body = extra_lib_imports + node.body

        return node
