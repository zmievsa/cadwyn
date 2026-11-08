from pydantic._internal._generics import get_model_typevars_map as get_model_typevars_map
from pydantic._internal._model_construction import unpack_lenient_weakvaluedict as unpack_lenient_weakvaluedict

from cadwyn._utils import present_private_submodules_as_package

from . import (
    _annotations,
    _bundle,
    _config,
    _enums,
    _fields,
    _generator,
    _generics,
    _instructions,
    _models,
    _references,
)
from ._annotation_utils import (
    _UNRESOLVED as _UNRESOLVED,
)
from ._annotation_utils import (
    _contains_forward_refs as _contains_forward_refs,
)
from ._annotation_utils import (
    _eval_forward_refs as _eval_forward_refs,
)
from ._annotation_utils import (
    _evaluate_user_annotations as _evaluate_user_annotations,
)
from ._annotation_utils import (
    _get_model_annotations as _get_model_annotations,
)
from ._annotation_utils import (
    _get_model_field_names as _get_model_field_names,
)
from ._annotation_utils import (
    _get_rebuilt_model_fields as _get_rebuilt_model_fields,
)
from ._annotation_utils import (
    _get_value_of_type_alias_with_args as _get_value_of_type_alias_with_args,
)
from ._annotations import (
    _AnnotationTransformer as _AnnotationTransformer,
)
from ._annotations import (
    _ReferencedModelCollector as _ReferencedModelCollector,
)
from ._callables import (
    _add_request_and_response_params as _add_request_and_response_params,
)
from ._callables import (
    _CallableWrapper as _CallableWrapper,
)
from ._common import (
    _is_versionable_model as _is_versionable_model,
)
from ._common import (
    _unwrap_model as _unwrap_model,
)
from ._enums import (
    _EnumWrapper as _EnumWrapper,
)
from ._fields import (
    _extract_passed_field_attributes as _extract_passed_field_attributes,
)
from ._generation import (
    generate_versioned_models as generate_versioned_models,
)
from ._generation import (
    get_models_that_routes_request as get_models_that_routes_request,
)
from ._generation import (
    migrate_response_body as migrate_response_body,
)
from ._generator import (
    SchemaGenerator as SchemaGenerator,
)
from ._models import (
    _change_version_of_model_references as _change_version_of_model_references,
)
from ._models import (
    _get_head_model_references as _get_head_model_references,
)
from ._models import (
    _PydanticModelWrapper as _PydanticModelWrapper,
)
from ._references import (
    _BareGenericCopier as _BareGenericCopier,
)
from ._references import (
    _BareGenericCopies as _BareGenericCopies,
)
from ._references import (
    _CycleCopier as _CycleCopier,
)

# Resolve annotation back-references after all defining modules have loaded, without cyclic imports.
_annotations.SchemaGenerator = SchemaGenerator
_annotations._CycleCopier = _CycleCopier
_bundle._EnumWrapper = _EnumWrapper
_bundle._PydanticModelWrapper = _PydanticModelWrapper
_config._PydanticModelWrapper = _PydanticModelWrapper
_enums.SchemaGenerator = SchemaGenerator
_fields._PydanticModelWrapper = _PydanticModelWrapper
_generator._ParentRename = _instructions._ParentRename
_generator._PydanticModelWrapper = _PydanticModelWrapper
_generator._ReferenceCycles = _references._ReferenceCycles
_generics.SchemaGenerator = SchemaGenerator
_generics._PydanticModelWrapper = _PydanticModelWrapper
_generics._CycleCopier = _CycleCopier
_instructions.SchemaGenerator = SchemaGenerator
_instructions._PydanticModelWrapper = _PydanticModelWrapper
_models.SchemaGenerator = SchemaGenerator
_models._CycleCopier = _CycleCopier
_references.SchemaGenerator = SchemaGenerator

# This package used to be a single module. Keep its objects' __module__ and namespace as they were then.
present_private_submodules_as_package(__name__)
