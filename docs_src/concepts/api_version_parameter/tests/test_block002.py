from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block002 import app


def test__swagger_authorize__token_request_without_version_header__succeeds():
    client = TestClient(app)
    # This is the request that Swagger UI's "Authorize" button sends
    response = client.post(
        "/token",
        data={"grant_type": "password", "username": "alice", "password": "x"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"access_token": "alice", "token_type": "bearer"}

    response = client.get(
        "/users/me",
        headers={
            "Authorization": "Bearer alice",
            "x-api-version": "2000-01-01",
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"token": "alice"}
