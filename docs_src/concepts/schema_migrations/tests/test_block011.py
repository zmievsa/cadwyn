from cadwyn import Cadwyn, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block011 import versions
from docs_src.concepts.schema_migrations.head.with_internal_notes import User

router = VersionedAPIRouter()


@router.get("/users/me")
async def get_current_user() -> User:
    return User(name="John")


app = Cadwyn(versions=versions)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__schema_had_description_in_head_version__openapi_of_latest_version__should_hide_docstring():
    openapi = client.get("/openapi.json?version=2001-01-01").json()
    user_schema = openapi["components"]["schemas"]["User"]
    assert user_schema["description"] == "A user of the API."


def test__schema_had_description_in_head_version__response__should_have_same_data():
    response = client.get("/users/me", headers={"x-api-version": "2001-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"name": "John"}
