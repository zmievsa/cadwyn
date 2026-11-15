"""What Cadwyn's versioning modules get from FastAPI once the FastAPI integration is imported.

Versions, version changes, and schema instructions work without FastAPI, so their modules don't import it. They only
name FastAPI's classes in annotations, which runtime annotation introspection (e.g. `typing.get_type_hints()`) resolves
in their namespaces. `cadwyn_core._stand_ins` gives them stand-ins for those, and this replaces the stand-ins with
FastAPI's classes.
"""

import importlib
import sys

from fastapi import Response
from fastapi.routing import APIRoute, APIWebSocketRoute

import cadwyn_core.structure.versions
from cadwyn import exceptions
from cadwyn._internal.old_names import keep_names_of_moved_package
from cadwyn.structure.versions import _requests, _responses, _versioned_endpoints
from cadwyn_core import _stand_ins, _utils
from cadwyn_core._utils import present_private_submodules_as_package
from cadwyn_core.structure import data
from cadwyn_core.structure.versions import _bundle

exceptions.APIRoute = APIRoute
exceptions.APIWebSocketRoute = APIWebSocketRoute

# Every module of Cadwyn that has a stand-in gets the class it stands for, the stand-ins' own module too, so that the
# modules that are imported later get the classes as well
for _module in [module for name, module in sys.modules.items() if name.partition(".")[0] in {"cadwyn", "cadwyn_core"}]:
    for _name, _value in list(vars(_module).items()):
        _stood_for = _stand_ins.get_class_stood_for(_value)
        if _stood_for is not None:
            setattr(_module, _name, getattr(importlib.import_module(_stood_for[0]), _stood_for[1]))

_utils.same_method_definition_as_in(Response.set_cookie)(data.ResponseInfo.set_cookie)
_utils.same_method_definition_as_in(Response.delete_cookie)(data.ResponseInfo.delete_cookie)

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
