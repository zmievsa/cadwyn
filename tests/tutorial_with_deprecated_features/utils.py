import shutil
from pathlib import Path


def clean_versions(dir: Path):
    # This is only here for testing purposes. In reality, you wouldn't do that.

    for path in dir.iterdir():
        if path.name.startswith("v200"):
            shutil.rmtree(path, ignore_errors=True)
