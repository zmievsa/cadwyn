from logging import getLogger

_CADWYN_REQUEST_PARAM_NAME = "cadwyn_request_param"
_CADWYN_RESPONSE_PARAM_NAME = "cadwyn_response_param"
# Marks the routes that an older version doesn't have, which route generation deletes after versioning them
_DELETED_ROUTE_TAG = "_CADWYN_DELETED_ROUTE"
# The FastAPI integration logs with it. It has its name from before cadwyn-core, which logging configs refer to.
_logger = getLogger("cadwyn.structure.versions")
_RouteId = int
