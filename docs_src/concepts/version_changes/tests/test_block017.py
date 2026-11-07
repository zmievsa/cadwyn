import pytest
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.block017 import app, sent_emails


@pytest.mark.parametrize("version", ["2023-02-10", "2022-11-16"])
def test__schedule_welcome_email__in_versioned_request__should_send_one_email(
    version: str,
):
    sent_emails.clear()
    client = TestClient(app, headers={"x-api-version": version})

    response = client.post("/users", params={"email": "john@example.com"})

    assert response.status_code == 200, response.json()
    assert sent_emails == ["john@example.com"]
