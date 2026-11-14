from cadwyn import migrate_response
from users import UserResource
from versions import versions

response_from_2000_01_01 = migrate_response(
    versions, UserResource, body=UserResource(name="John"), version="2000-01-01"
)
