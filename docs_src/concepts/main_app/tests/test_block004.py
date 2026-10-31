import pytest
from fastapi.testclient import TestClient

from docs_src.concepts.main_app.block004 import app

client = TestClient(app)


@pytest.mark.parametrize(
    "url",
    [
        "/static/swagger-ui-bundle.js",
        "/static/swagger-ui.css",
        "/static/favicon.png",
    ],
)
def test__custom_docs_assets__swagger_ui__should_load_custom_url(url: str):
    response = client.get("/docs?version=2022-11-16")
    assert response.status_code == 200
    assert url in response.text


@pytest.mark.parametrize(
    "url", ["/static/redoc.standalone.js", "/static/favicon.png"]
)
def test__custom_docs_assets__redoc__should_load_custom_url(url: str):
    response = client.get("/redoc?version=2022-11-16")
    assert response.status_code == 200
    assert url in response.text
