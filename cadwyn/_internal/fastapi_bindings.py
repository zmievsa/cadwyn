"""What Cadwyn's versioning modules get from FastAPI once the FastAPI integration is imported.

Versions, version changes, and schema instructions work without FastAPI, so their modules don't import it. They only
name FastAPI's classes in annotations, which runtime annotation introspection (e.g. `typing.get_type_hints()`) resolves
in their namespaces, and FastAPI's dataclasses can be in the attributes that they compare.
"""

import fastapi.params
from fastapi import Request, Response
from fastapi._compat import ModelField
from fastapi.dependencies.models import Dependant
from fastapi.params import Depends
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.datastructures import MutableHeaders, UploadFile
from starlette.routing import BaseRoute

from cadwyn import _utils, exceptions
from cadwyn.structure import data, endpoints
from cadwyn.structure.versions import _aliases, _bundle, _enums, _json, _migration_targets, _migrations, _uploads

_utils._allow_dataclasses_in_attributes(fastapi.params.Depends, fastapi.params.Security)

exceptions.APIRoute = APIRoute
exceptions.APIWebSocketRoute = APIWebSocketRoute

endpoints.Response = Response
endpoints.Depends = Depends
endpoints.APIRoute = APIRoute
endpoints.BaseRoute = BaseRoute

data.Request = Request
data.Response = Response
data.MutableHeaders = MutableHeaders
data.UploadFile = UploadFile
_utils.same_method_definition_as_in(Response.set_cookie)(data.ResponseInfo.set_cookie)
_utils.same_method_definition_as_in(Response.delete_cookie)(data.ResponseInfo.delete_cookie)

_aliases.ModelField = ModelField
_bundle.APIRoute = APIRoute
_enums.ModelField = ModelField
_enums.Dependant = Dependant
_enums.APIRoute = APIRoute
_json.ModelField = ModelField
_json.Dependant = Dependant
_json.APIRoute = APIRoute
_migration_targets.APIRoute = APIRoute
_migrations.APIRoute = APIRoute
_migrations._UploadsToClose = _uploads._UploadsToClose
