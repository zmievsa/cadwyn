from fastapi.testclient import TestClient

from docs_src.concepts.main_app.block001 import app

client = TestClient(app)


def test__versioned_router__request_for_users__should_return_users():
    response = client.get("/users/", headers={"x-api-version": "2022-11-16"})
    assert response.status_code == 200, response.json()
    assert response.json() == [{"username": "Rick"}, {"username": "Morty"}]


def test__versioned_router__request_for_user__should_return_user():
    response = client.get(
        "/users/Morty", headers={"x-api-version": "2022-11-16"}
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"username": "Morty"}


def test__versioned_router__openapi__should_document_routes_with_tags():
    openapi = client.get("/openapi.json?version=2022-11-16").json()
    assert openapi["paths"]["/users/"]["get"]["tags"] == ["users"]
    assert openapi["paths"]["/users/{username}"]["get"]["tags"] == ["users"]
