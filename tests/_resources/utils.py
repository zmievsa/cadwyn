import re

import fastapi
from pydantic.version import VERSION as PYDANTIC_VERSION

DEFAULT_API_VERSION = "2021-01-01"
BASIC_HEADERS = {"X-API-VERSION": DEFAULT_API_VERSION}

# Before pydantic 2.12, `x: Annotated[str, Field(min_length=1)]` takes its constraints from the Field() that Cadwyn
# passes as the default of the field in older versions, so field().had() changes them there, as in Cadwyn 7.4.0.
# Since pydantic 2.12, the Field() in the annotation wins
HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED = tuple(int(part) for part in PYDANTIC_VERSION.split(".")[:2]) < (2, 12)

# Like Cadwyn, we only take the numbers, so that pre-releases such as 0.163.0rc1 parse too
_FASTAPI_VERSION = tuple(map(int, re.findall(r"\d+", fastapi.__version__)[:3]))
_FASTAPI_MINOR_VERSION = _FASTAPI_VERSION[:2]
# FastAPI 0.145.0 removed the `openapi_prefix` parameter
FASTAPI_ACCEPTS_OPENAPI_PREFIX = _FASTAPI_MINOR_VERSION < (0, 145)
# Before FastAPI 0.148.0, FastAPI validates the defaults of omitted query, header, cookie, and form params.
# Since FastAPI 0.148.0, it only validates them when they set `validate_default=True`
FASTAPI_VALIDATES_DEFAULTS_OF_OMITTED_PARAMS = _FASTAPI_MINOR_VERSION < (0, 148)
# Before FastAPI 0.151.0, FastAPI treats a body that is None as an omitted one, so it can't tell it from a JSON null
FASTAPI_DISTINGUISHES_NULL_BODIES = _FASTAPI_MINOR_VERSION >= (0, 151)
# Before FastAPI 0.157.0, FastAPI reads the uploaded files of `bytes` params to the end, so other params get them empty.
# Since FastAPI 0.157.0, it puts them back where they were, which requires their streams to be seekable
FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES = _FASTAPI_MINOR_VERSION >= (0, 157)
# Before FastAPI 0.158.2, FastAPI embeds `Optional` form models, so it looks their fields up under the param's alias
FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS = _FASTAPI_VERSION >= (0, 158, 2)
# Before FastAPI 0.158.0, FastAPI validates the whole form against a form field that only a dependency override declares
FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES = _FASTAPI_MINOR_VERSION >= (0, 158)
