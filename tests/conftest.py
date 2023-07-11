from tests.test_codegen import CURRENT_DIR


import pytest


import shutil
import sys


@pytest.fixture(autouse=True, scope="module")
def remove_generated_files():
    yield
    shutil.rmtree(CURRENT_DIR / "_data/v2000_01_01", ignore_errors=True)
    shutil.rmtree(CURRENT_DIR / "_data/v2001_01_01", ignore_errors=True)
    shutil.rmtree("tests/_data/latest/another_temp1", ignore_errors=True)
    shutil.rmtree("tests/_data/latest/another_temp1", ignore_errors=True)
    for module_name in list(sys.modules):
        if module_name.startswith("tests._data.latest.another_temp1"):
            del sys.modules[module_name]
