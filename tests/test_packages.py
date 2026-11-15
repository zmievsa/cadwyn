"""`cadwyn` and `cadwyn-core` are released together in the same version, and they fail at import otherwise."""

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import cadwyn_core
import pytest

import cadwyn

_ROOT = Path(__file__).parent.parent


def _read_pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def test__versions__of_cadwyn_and_cadwyn_core__should_be_the_same_everywhere():
    cadwyn_project = _read_pyproject(_ROOT / "pyproject.toml")["project"]
    core_project = _read_pyproject(_ROOT / "cadwyn-core" / "pyproject.toml")["project"]

    assert cadwyn_project["version"] == core_project["version"] == cadwyn.__version__ == cadwyn_core.__version__
    assert f"cadwyn-core=={core_project['version']}" in cadwyn_project["dependencies"]


def test__license__of_cadwyn_core__should_be_the_license_of_cadwyn():
    assert (_ROOT / "cadwyn-core" / "LICENSE").read_text() == (_ROOT / "LICENSE").read_text()


def _import_with_installed_distribution(tmp_path: Path, name: str, version: str, module: str):
    """Import the module with another installed version of a distribution, which Python finds before the real one"""
    dist_info = tmp_path / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("module", ["cadwyn", "cadwyn_core"])
def test__import__with_another_version_of_cadwyn__should_name_the_versions(tmp_path: Path, module: str):
    result = _import_with_installed_distribution(tmp_path, "cadwyn", "0.0.1", module)

    assert result.returncode != 0
    assert (
        f"ImportError: cadwyn-core {cadwyn_core.__version__} only works with cadwyn {cadwyn_core.__version__}, "
        "but cadwyn 0.0.1 is installed."
    ) in result.stderr


def test__import__with_another_version_of_cadwyn_core__should_name_the_versions(tmp_path: Path):
    result = _import_with_installed_distribution(tmp_path, "cadwyn-core", "0.0.1", "cadwyn")

    assert result.returncode != 0
    assert (
        f"ImportError: cadwyn-core 0.0.1 only works with cadwyn 0.0.1, but cadwyn {cadwyn.__version__} is installed."
    ) in result.stderr


def test__import_of_cadwyn_core__without_cadwyn__should_succeed(tmp_path: Path):
    # Without site-packages, nothing but cadwyn-core is installed, as in a standalone install
    dist_info = tmp_path / f"cadwyn_core-{cadwyn_core.__version__}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: cadwyn-core\nVersion: {cadwyn_core.__version__}\n"
    )
    core_source = str(Path(cadwyn_core.__file__).parent.parent)

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                f"import sys; sys.path[:0] = [{core_source!r}, {str(tmp_path)!r}]; import cadwyn_core; "
                "print(cadwyn_core.__version__)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stdout.strip()) == (0, cadwyn_core.__version__), result.stderr


def test__import_of_cadwyn__without_cadwyn_core__should_name_the_version_that_it_needs():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib.abc
                import sys


                class WithoutCadwynCore(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path, target=None):
                        if fullname == "cadwyn_core":
                            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
                        return None


                sys.meta_path.insert(0, WithoutCadwynCore())
                import cadwyn
                """
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (
        f"ImportError: cadwyn {cadwyn.__version__} needs cadwyn-core {cadwyn.__version__}, which isn't installed."
    ) in result.stderr
