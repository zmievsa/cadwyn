from cadwyn import migrate_response_body
from versions import User, versions

user = User(full_name="John")

body = migrate_response_body(
    versions, User, latest_body=user.model_dump(), version="2000-01-01"
)
