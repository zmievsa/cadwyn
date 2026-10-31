from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
)
from fastapi.testclient import TestClient

from docs_src.concepts.changelogs.block001 import (
    RemoveAddressFromUser,
    RenameUserLookupPath,
    User,
)


class AddUserLookupEndpoint(VersionChange):
    """A version change that the changelog shows."""

    instructions_to_migrate_to_previous_version = ()


router = VersionedAPIRouter()


@router.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    return {"id": user_id, "address": "1 Main St"}


app = Cadwyn(
    versions=VersionBundle(
        Version(
            "2001-01-01",
            RenameUserLookupPath,
            RemoveAddressFromUser,
            AddUserLookupEndpoint,
        ),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)


def test__changelog__with_hidden_instruction_and_version_change__should_leave_them_out():
    client = TestClient(app)

    response = client.get("/changelog")

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "versions": [
            {
                "value": "2001-01-01",
                "changes": [
                    {
                        "description": RenameUserLookupPath.description,
                        "side_effects": False,
                        "instructions": [],
                    },
                    {
                        "description": AddUserLookupEndpoint.description,
                        "side_effects": False,
                        "instructions": [],
                    },
                ],
            }
        ]
    }


def test__hidden_version_changes__in_api__should_still_apply():
    client = TestClient(app)

    old_response = client.get(
        "/user/1", headers={"x-api-version": "2000-01-01"}
    )
    new_response = client.get(
        "/users/1", headers={"x-api-version": "2001-01-01"}
    )

    assert old_response.json() == {"id": 1, "address": "1 Main St"}
    assert new_response.json() == {"id": 1}
