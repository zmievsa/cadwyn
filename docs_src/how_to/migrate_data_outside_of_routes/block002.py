from cadwyn import migrate_response
from versions import User, versions

user = User(full_name="John")

body = migrate_response(versions, User, body=user, version="2000-01-01").body
