from cadwyn._utils import present_private_submodules_as_package

from . import (
    _annotation_evaluation as _annotation_evaluation,
)
from . import (
    _annotation_transformer,
    _enums,
    _fields,
    _generics,
    _instructions,
    _models,
    _references,
    _schema_generator,
)
from . import (
    _callables as _callables,
)
from . import (
    _common as _common,
)
from . import (
    _core_schema as _core_schema,
)
from . import (
    _versioned_models as _versioned_models,
)
from ._callables import is_async_gen_callable as is_async_gen_callable
from ._callables import is_coroutine_callable as is_coroutine_callable
from ._callables import is_gen_callable as is_gen_callable
from ._callables import is_regular_function as is_regular_function
from ._fields import EXTRA_FIELD_NAME as EXTRA_FIELD_NAME
from ._fields import PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP as PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP
from ._fields import VALIDATOR_CONFIG_KEY as VALIDATOR_CONFIG_KEY
from ._fields import PydanticFieldWrapper as PydanticFieldWrapper
from ._fields import dict_of_empty_field_info as dict_of_empty_field_info
from ._schema_generator import SchemaGenerator as SchemaGenerator
from ._versioned_models import generate_versioned_models as generate_versioned_models
from ._versioned_models import get_models_that_routes_request as get_models_that_routes_request
from ._versioned_models import migrate_response_body as migrate_response_body

# These modules import some names only for type checking because importing them at runtime would be cyclic.
# Bind them now that every module has loaded so that runtime annotation introspection can resolve them.
_annotation_transformer.SchemaGenerator = SchemaGenerator
_annotation_transformer._CycleCopier = _references._CycleCopier
_enums.SchemaGenerator = SchemaGenerator
_fields._PydanticModelWrapper = _models._PydanticModelWrapper
_generics.SchemaGenerator = SchemaGenerator
_generics._PydanticModelWrapper = _models._PydanticModelWrapper
_generics._CycleCopier = _references._CycleCopier
_instructions.SchemaGenerator = SchemaGenerator
_instructions._PydanticModelWrapper = _models._PydanticModelWrapper
_instructions._RuntimeSchemaGenContext = _schema_generator._RuntimeSchemaGenContext
_models.SchemaGenerator = SchemaGenerator
_models._CycleCopier = _references._CycleCopier
_references.SchemaGenerator = SchemaGenerator
_schema_generator._ParentRename = _instructions._ParentRename
_schema_generator._PydanticModelWrapper = _models._PydanticModelWrapper
_schema_generator._ReferenceCycles = _references._ReferenceCycles

# This package used to be a single module. Keep its objects' __module__ and namespace as they were then.
present_private_submodules_as_package(__name__)
