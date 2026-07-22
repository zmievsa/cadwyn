from fastapi.testclient import TestClient

from docs_src.concepts.schema_generation.nested_schema import app


def test__schema_nested_in_external_generic_wrapper_is_versioned():
    client = TestClient(app)

    response_2000 = client.get(
        "/customers", headers={"x-api-version": "2000-01-01"}
    )
    response_2001 = client.get(
        "/customers", headers={"x-api-version": "2001-01-01"}
    )

    assert response_2000.json() == {
        "items": [{"name": "John Doe"}],
        "total": 1,
    }
    assert response_2001.json() == {
        "items": [
            {
                "name": "John Doe",
                "address": "123 Cherry Lane",
            }
        ],
        "total": 1,
    }

    openapi_2000 = client.get("/openapi.json?version=2000-01-01").json()
    openapi_2001 = client.get("/openapi.json?version=2001-01-01").json()
    customer_2000 = openapi_2000["components"]["schemas"]["Customer"]
    customer_2001 = openapi_2001["components"]["schemas"]["Customer"]

    assert "address" not in customer_2000["properties"]
    assert "address" in customer_2001["properties"]
