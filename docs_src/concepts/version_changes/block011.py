from cadwyn.schema_generation import migrate_response_body
from users import UserResource
from versions import versions

body_from_2000_01_01 = migrate_response_body(
    versions, UserResource, latest_body={"name": "John"}, version="2000-01-01"
)
