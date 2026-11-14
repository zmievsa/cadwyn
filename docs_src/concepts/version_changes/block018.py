from cadwyn.schema_generation import migrate_request_body
from users import UserCreateRequest
from versions import versions

request_body_from_2000_01_01 = migrate_request_body(
    versions,
    UserCreateRequest,
    body={"full_name": "John"},
    version="2000-01-01",
)
