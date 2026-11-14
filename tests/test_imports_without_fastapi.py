"""Versioning must work without FastAPI and its dependencies, with nothing but Pydantic and what Pydantic requires.

Standalone use isn't available yet (https://github.com/zmievsa/cadwyn/issues/203): `cadwyn` and `cadwyn.structure`
still import the FastAPI integration, so these tests skip their `__init__` modules and check the modules that
standalone use will be built from. Add each module here once it imports without FastAPI.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import cadwyn

# The packages of Pydantic and of what Pydantic requires
_PACKAGES_OF_STANDALONE_USE = ("cadwyn", "pydantic", "pydantic_core", "typing_extensions", "typing_inspection")
_PACKAGES_THAT_PYDANTIC_REQUIRES = ("annotated_types",)
# The packages whose `__init__` modules still import the FastAPI integration
_PACKAGES_THAT_IMPORT_FASTAPI = ("cadwyn", "cadwyn.structure", "cadwyn.structure.versions")

_IMPORT_WITH_ONLY_PYDANTIC = textwrap.dedent(
    """
    import builtins
    import importlib
    import importlib.abc
    import importlib.machinery
    import sys
    import sysconfig
    import types
    from pathlib import Path

    module_name, cadwyn_dir, allowed_packages, packages_that_import_fastapi = sys.argv[1:]
    allowed = set(allowed_packages.split(","))
    installed_package_dirs = tuple({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})


    class OnlyAllowedInstalledPackages(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname.partition(".")[0] in allowed:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and (spec.origin or "").startswith(installed_package_dirs):
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return None


    sys.meta_path.insert(0, OnlyAllowedInstalledPackages())
    # Modules that are imported already (e.g. coverage, which measures this process) skip the finders
    original_import = builtins.__import__


    def import_only_allowed_installed_packages(name, globals=None, locals=None, fromlist=(), level=0):
        module = original_import(name, globals, locals, fromlist, level)
        top_level_name = name.partition(".")[0]
        importer = (globals or {}).get("__name__") or ""
        if level == 0 and importer.partition(".")[0] == "cadwyn" and top_level_name not in allowed:
            if (getattr(sys.modules.get(top_level_name), "__file__", None) or "").startswith(installed_package_dirs):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return module


    builtins.__import__ = import_only_allowed_installed_packages
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("FastAPI must not be importable")

    for package in packages_that_import_fastapi.split(","):
        stub = types.ModuleType(package)
        stub.__path__ = [str(Path(cadwyn_dir, *package.split(".")[1:]))]
        sys.modules[package] = stub
    importlib.import_module(module_name)
    """
)


@pytest.mark.parametrize(
    "module_name",
    [
        "cadwyn._utils",
        "cadwyn.exceptions",
        "cadwyn.structure.common",
        "cadwyn.structure.data",
        "cadwyn.structure.endpoints",
        "cadwyn.structure.enums",
        "cadwyn.structure.schemas",
        "cadwyn.structure.versions._aliases",
        "cadwyn.structure.versions._bundle",
        "cadwyn.structure.versions._changes",
        "cadwyn.structure.versions._core_schema",
        "cadwyn.structure.versions._enums",
        "cadwyn.structure.versions._json",
        "cadwyn.structure.versions._migration_targets",
        "cadwyn.structure.versions._migrations",
    ],
)
def test__import__without_fastapi_and_its_dependencies__should_succeed(module_name: str):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _IMPORT_WITH_ONLY_PYDANTIC,
            module_name,
            str(Path(cadwyn.__file__).parent),
            ",".join((*_PACKAGES_OF_STANDALONE_USE, *_PACKAGES_THAT_PYDANTIC_REQUIRES)),
            ",".join(_PACKAGES_THAT_IMPORT_FASTAPI),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
