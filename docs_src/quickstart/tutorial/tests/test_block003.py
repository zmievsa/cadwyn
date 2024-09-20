import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from docs_src.quickstart.tutorial.block003 import app

    return TestClient(app)


def test__basic_post__with_version_2000(client: TestClient):
    from docs_src.quickstart.tutorial.tests.test_block001 import test__basic_post__with_version_2000

    test__basic_post__with_version_2000(client)


def test__basic_post__with_version_2001(client: TestClient):
    from docs_src.quickstart.tutorial.tests.test_block002 import test__basic_post__with_version_2001

    test__basic_post__with_version_2001(client)
