from pydantic.version import VERSION as PYDANTIC_VERSION

DEFAULT_API_VERSION = "2021-01-01"
BASIC_HEADERS = {"X-API-VERSION": DEFAULT_API_VERSION}

# Before pydantic 2.12, `x: Annotated[str, Field(min_length=1)]` takes its constraints from the Field() that Cadwyn
# passes as the default of the field in older versions, so field().had() changes them there, as in Cadwyn 7.4.0.
# Since pydantic 2.12, the Field() in the annotation wins
HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED = tuple(int(part) for part in PYDANTIC_VERSION.split(".")[:2]) < (2, 12)
