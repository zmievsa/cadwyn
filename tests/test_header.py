from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universi.header import api_version_var, get_universi_dependency


def test__header():
    app = FastAPI(
        dependencies=[get_universi_dependency(version_header_name="x-test-version")],
    )

    @app.get("/")
    async def root():
        assert api_version_var.get() is not None
        return api_version_var.get()

    client = TestClient(app)

    response = client.get("/", headers={"x-test-version": "2021-01-01"})
    assert response.status_code == 200
    assert response.json() == "2021-01-01"

    response = client.get("/")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "loc": ["header", "x-test-version"],
                "msg": "field required",
                "type": "value_error.missing",
            },
        ],
    }


def test__header__with_default_version():
    app = FastAPI(
        dependencies=[get_universi_dependency(version_header_name="x-test-version", default_version=date(2021, 1, 1))],
    )

    @app.get("/")
    async def root():
        assert api_version_var.get() is not None
        return api_version_var.get()

    client = TestClient(app)

    response = client.get("/", headers={"x-test-version": "2022-01-01"})
    assert response.status_code == 200
    assert response.json() == "2022-01-01"

    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == "2021-01-01"
