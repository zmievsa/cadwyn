from cadwyn import migrate_request
from users import UserCreateRequest
from versions import versions

request_from_2000_01_01 = migrate_request(
    versions,
    UserCreateRequest,
    body={"full_name": "John"},
    version="2000-01-01",
    headers={"x-request-id": "42"},
)
