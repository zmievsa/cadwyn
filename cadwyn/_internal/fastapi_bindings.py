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

import cadwyn_core.structure.versions
from cadwyn import exceptions
from cadwyn._internal.old_names import keep_names_of_moved_package
from cadwyn.structure.versions import _requests, _responses, _versioned_endpoints
from cadwyn_core import _utils
from cadwyn_core._utils import present_private_submodules_as_package
from cadwyn_core.schema_generation import _annotation_transformer, _callables, _versioned_models
from cadwyn_core.structure import data, endpoints
from cadwyn_core.structure.versions import _aliases, _bundle, _enums, _json, _migration_targets, _migrations

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

# ty sees two different types of the same module object
_annotation_transformer.fastapi = fastapi  # ty: ignore[invalid-assignment]
_callables.APIRoute = APIRoute
_versioned_models.fastapi = fastapi  # ty: ignore[invalid-assignment]


# The submodules that migrate requests and responses in routes refer back to the bundle in annotations
_requests.VersionBundle = _bundle.VersionBundle
_responses.VersionBundle = _bundle.VersionBundle
_versioned_endpoints.VersionBundle = _bundle.VersionBundle
# The FastAPI names that are bound above are in the namespaces of the packages too, where
# `typing.get_type_hints()` looks for the objects that report the package as their module
present_private_submodules_as_package("cadwyn_core.structure.versions")
keep_names_of_moved_package("cadwyn.structure.versions", cadwyn_core.structure.versions)
# `cadwyn.structure.versions` used to be a single module, which had their objects too
present_private_submodules_as_package("cadwyn.structure.versions")
