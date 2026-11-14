from cadwyn import migrate_request
from versions import User, versions

message = {"name": "John"}

user = migrate_request(versions, User, body=message, version="2000-01-01").body
