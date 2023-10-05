from contextvars import ContextVar
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cadwyn.header import get_cadwyn_dependency


def test__header(api_version_var: ContextVar[date | None]):
    app = FastAPI(
        dependencies=[get_cadwyn_dependency(version_header_name="x-test-version", api_version_var=api_version_var)],
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


def test__header__with_default_version(api_version_var: ContextVar[date | None]):
    app = FastAPI(
        dependencies=[
            get_cadwyn_dependency(
                version_header_name="x-test-version",
                default_version=date(2021, 1, 1),
                api_version_var=api_version_var,
            ),
        ],
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
