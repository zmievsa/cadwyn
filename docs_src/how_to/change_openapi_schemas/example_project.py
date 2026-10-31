"""Run the how-to examples that are laid out as a small project.

The how-to pages describe an app with a `users` module, a `versions` package,
and a `main` module, and their examples import these as top-level modules
(`from users import UserResource`). Several examples use the same module names,
so each test imports one example project at a time and forgets its modules
afterwards.
"""

import importlib
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient


@contextmanager
def example_project_client(
    directory: Path,
) -> Generator[TestClient, None, None]:
    """Import `main.app` from the example project in `directory`."""
    directory = directory.resolve()
    top_level_module_names = {
        path.stem for path in directory.iterdir() if path.suffix == ".py"
    } | {path.name for path in directory.iterdir() if path.is_dir()}
    already_imported = top_level_module_names & sys.modules.keys()
    if already_imported:
        msg = f"Modules {already_imported} of another app are already imported"
        raise RuntimeError(msg)
    sys.path.insert(0, str(directory))
    try:
        main = importlib.import_module("main")
        with TestClient(main.app) as client:
            yield client
    finally:
        sys.path.remove(str(directory))
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if file is not None and Path(file).resolve().is_relative_to(
                directory
            ):
                del sys.modules[name]
