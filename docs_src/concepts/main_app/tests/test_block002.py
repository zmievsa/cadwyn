from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from docs_src.concepts.main_app.block002 import app

# Cadwyn's development environment has sqlmodel, so fastapi-pagination
# recommends its sqlmodel extension instead of the default paginate()
pytestmark = pytest.mark.filterwarnings(
    "ignore::fastapi_pagination.utils.FastAPIPaginationWarning"
)


@pytest.fixture(params=[False, True], ids=["without_lifespan", "with_lifespan"])
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    if request.param:
        with TestClient(app) as client:
            yield client
    else:
        yield TestClient(app)


def test__add_pagination_to_router__request_to_older_version__should_return_migrated_page(
    client: TestClient,
):
    response = client.get(
        "/users",
        params={"size": 1},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "items": [{"id": 1}],
        "total": 2,
        "page": 1,
        "size": 1,
        "pages": 2,
    }


def test__add_pagination_to_router__request_to_newer_version__should_return_page(
    client: TestClient,
):
    response = client.get(
        "/users",
        params={"page": 2, "size": 1},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "items": [{"id": 2, "name": "Morty"}],
        "total": 2,
        "page": 2,
        "size": 1,
        "pages": 2,
    }
