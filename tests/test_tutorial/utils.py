from pathlib import Path


def clean_versions(dir: Path):
    import shutil

    for path in dir.iterdir():
        if path.name.startswith("v200"):
            shutil.rmtree(path, ignore_errors=True)
