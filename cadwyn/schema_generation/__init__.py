"""`cadwyn_core.schema_generation` under its old name, with every name of it, as the same objects."""

import cadwyn_core.schema_generation
from cadwyn._internal.old_names import keep_names_of_moved_package
from cadwyn_core.schema_generation import EXTRA_FIELD_NAME as EXTRA_FIELD_NAME
from cadwyn_core.schema_generation import (
    PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP as PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP,
)
from cadwyn_core.schema_generation import VALIDATOR_CONFIG_KEY as VALIDATOR_CONFIG_KEY
from cadwyn_core.schema_generation import PydanticFieldWrapper as PydanticFieldWrapper
from cadwyn_core.schema_generation import SchemaGenerator as SchemaGenerator
from cadwyn_core.schema_generation import dict_of_empty_field_info as dict_of_empty_field_info
from cadwyn_core.schema_generation import generate_versioned_models as generate_versioned_models
from cadwyn_core.schema_generation import get_models_that_routes_request as get_models_that_routes_request
from cadwyn_core.schema_generation import is_async_gen_callable as is_async_gen_callable
from cadwyn_core.schema_generation import is_coroutine_callable as is_coroutine_callable
from cadwyn_core.schema_generation import is_gen_callable as is_gen_callable
from cadwyn_core.schema_generation import is_regular_function as is_regular_function
from cadwyn_core.schema_generation import migrate_request as migrate_request
from cadwyn_core.schema_generation import migrate_response as migrate_response
from cadwyn_core.schema_generation import migrate_response_body as migrate_response_body

from . import _annotation_evaluation as _annotation_evaluation
from . import _annotation_transformer as _annotation_transformer
from . import _callables as _callables
from . import _common as _common
from . import _core_schema as _core_schema
from . import _enums as _enums
from . import _fields as _fields
from . import _generics as _generics
from . import _instructions as _instructions
from . import _models as _models
from . import _references as _references
from . import _schema_generator as _schema_generator
from . import _versioned_models as _versioned_models

keep_names_of_moved_package(__name__, cadwyn_core.schema_generation)
