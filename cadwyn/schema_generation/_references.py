import collections.abc  # noqa: TC003 # Needed by runtime annotation introspection.
import itertools
import threading
import types
from collections.abc import Callable
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ForwardRef,
    Union,
    cast,
    final,
)

from pydantic import BaseModel
from pydantic._internal._generics import PydanticGenericMetadata
from pydantic._internal._model_construction import build_lenient_weakvaluedict, unpack_lenient_weakvaluedict
from pydantic.fields import FieldInfo  # noqa: TC002 # Needed by runtime annotation introspection.
from typing_extensions import (
    TypeVar,
)

from cadwyn._utils import (
    lenient_issubclass,
)
from cadwyn.exceptions import CadwynError, SchemaGenerationError

from ._annotation_evaluation import (
    _NO_REDEFINED_FIELDS,
    _get_annotation_with_metadata,
    _get_rebuilt_model_fields,
    _replace_forward_refs,
    _replace_in_type_positions,
)
from ._annotation_transformer import (
    _AnnotationTransformer,
    _ModelReferences,
)
from ._common import (
    _T_ANY_MODEL,
    _is_versionable_model,
    _unwrap_model,
)
from ._generics import (
    _change_version_of_generic_metadata,
    _is_parametrization,
    _is_same_parametrization,
)
from ._models import (
    _get_head_model_references,
    _get_models_in_field,
)

if TYPE_CHECKING:
    from ._schema_generator import (
        SchemaGenerator,
    )

_T_NODE = TypeVar("_T_NODE")
_T_COMPONENT_VALUE = TypeVar("_T_COMPONENT_VALUE")


def _find_strongly_connected_components(
    start: _T_NODE,
    get_successors: Callable[[_T_NODE], "collections.abc.Iterable[_T_NODE]"],
    components: dict[_T_NODE, _T_COMPONENT_VALUE],
    get_value: Callable[[list[_T_NODE], bool], _T_COMPONENT_VALUE],
) -> None:
    """Find the strongly connected components reachable from start (Tarjan's algorithm).

    Maps the nodes of each component in `components` to get_value(component, is_cycle), where is_cycle is whether
    the component has more than one node or a node that is its own successor.
    The nodes already in `components` are skipped because none of them can lead back to the new ones.
    It is iterative instead of recursive because the chains of references in large apps can be very long.
    """
    indices: dict[_T_NODE, int] = {start: 0}
    lowest_indices = {start: 0}
    stack = [start]
    nodes_to_visit = [(start, iter(get_successors(start)))]
    while nodes_to_visit:
        current_node, successors = nodes_to_visit[-1]
        for successor in successors:
            if successor in components:
                continue
            if successor not in indices:
                indices[successor] = lowest_indices[successor] = len(indices)
                stack.append(successor)
                nodes_to_visit.append((successor, iter(get_successors(successor))))
                break
            # The node is still on the stack because the nodes that are not are in finished components
            lowest_indices[current_node] = min(lowest_indices[current_node], indices[successor])
        else:
            nodes_to_visit.pop()
            if nodes_to_visit:
                previous_node = nodes_to_visit[-1][0]
                lowest_indices[previous_node] = min(lowest_indices[previous_node], lowest_indices[current_node])
            if lowest_indices[current_node] == indices[current_node]:
                component = []
                while not component or component[-1] is not current_node:
                    component.append(stack.pop())
                is_cycle = len(component) > 1 or current_node in get_successors(current_node)
                components.update(dict.fromkeys(component, get_value(component, is_cycle)))


@final
class _ReferenceCycles:
    """The reference cycles among the head models, shared by the generators of all versions.

    SchemaGenerator only uses the head models of the cycles whose models are not versioned, and the references of
    such models are the same in every version because only instructions change them. So we find the cycles once
    in the references of the head models. The cycles that only exist in some versions because of instructions
    contain models with instructions so all of their models are versioned and copied anyway.
    """

    __slots__ = (
        "_changed_models",
        "_changed_parametrizations",
        "_head_references",
        "_head_references_with_redefined_fields",
        "_lock",
        "_models_in_cycles",
        "_models_in_cycles_with_redefined_fields",
        "_parametrizations_built_by_pydantic",
        "_reaches_changed_parametrizations",
        "_redefined_field_names",
        "_redefined_fields",
        "_requested_models",
    )

    def __init__(self, changed_models: "frozenset[type[Union[BaseModel, Enum]]]") -> None:
        # The generators of different versions can ask at the same time
        self._lock = threading.Lock()
        self._models_in_cycles: dict[type[Union[BaseModel, Enum]], bool] = {}
        self._requested_models: dict[type[Union[BaseModel, Enum]], frozenset[type[Union[BaseModel, Enum]]]] = {}
        # The references of the head models are the same in every version so the generators share them
        self._head_references: dict[type[BaseModel], _ModelReferences] = {}
        # The models with schema or enum instructions in any version
        self._changed_models = changed_models
        # The parametrizations with instructions, which also apply where pydantic builds them in the fields of other
        # generics, e.g. `schema(Pair[str, int])` in `Box[str]` with `pair: Pair[T, int]` (see
        # get_redefined_field_names). Without them, nothing is redefined.
        self._changed_parametrizations = frozenset(
            model for model in changed_models if lenient_issubclass(model, BaseModel) and _is_parametrization(model)
        )
        # See get_redefined_field_names
        self._parametrizations_built_by_pydantic: dict[
            type[Union[BaseModel, Enum]], dict[str, frozenset[type[BaseModel]]]
        ] = {}
        self._reaches_changed_parametrizations: dict[type[BaseModel], bool] = {}
        self._models_in_cycles_with_redefined_fields: dict[type[Union[BaseModel, Enum]], bool] = {}
        self._redefined_field_names: dict[type[BaseModel], tuple[str, ...]] = {}
        self._redefined_fields: dict[type[BaseModel], collections.abc.Mapping[str, Any]] = {}
        self._head_references_with_redefined_fields: dict[type[BaseModel], _ModelReferences] = {}

    def is_in_cycle(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether copying the model would request the model itself again, directly or through other models.

        Copying the generic args never leads back to the model because they are only versioned when they do not
        (see SchemaGenerator._should_version_generic_args) so only the models in the fields, annotations,
        and bases count.
        """
        with self._lock:
            if model not in self._models_in_cycles:
                _find_strongly_connected_components(
                    model, self._get_requested_models, self._models_in_cycles, lambda _, is_cycle: is_cycle
                )
            return self._models_in_cycles[model]

    def get_requested_models(self, model: type[Union[BaseModel, Enum]]) -> frozenset[type[Union[BaseModel, Enum]]]:
        """Return the head models that copying the head model would request"""
        with self._lock:
            return self._get_requested_models(model)

    def get_head_references(self, model: type[BaseModel]) -> "_ModelReferences":
        """Return the references of the head model (see _get_head_model_references), with its redefined fields"""
        with self._lock:
            redefined_fields = self._get_redefined_fields(model)
            if not redefined_fields:
                return self._get_head_references(model)
            if model not in self._head_references_with_redefined_fields:
                # They are complete because the fields are only redefined if we know what copying them requests
                self._head_references_with_redefined_fields[model] = _get_head_model_references(model, redefined_fields)
            return self._head_references_with_redefined_fields[model]

    def get_redefined_field_names(self, model: type[BaseModel]) -> tuple[str, ...]:
        """Return the inherited fields that the copies of the parametrization of a generic define themselves.

        Pydantic builds the fields that a parametrization (e.g. `Box[str]`) inherits from its generic
        (e.g. `pair: Pair[T, int]` in `Box`) by parametrizing their generics itself, through its own cache. So in
        the copy of `Box[str]`, `pair` gets a `Pair[str, int]` that pydantic builds from the copy of `Pair`
        instead of the copy of `Pair[str, int]` itself, which is the only one that has the instructions of
        `schema(Pair[str, int])`. So when such parametrizations lead to parametrizations with their own
        instructions, the copy defines these fields itself with the annotations that pydantic resolved for the head
        parametrization, which the generator then versions as usual. Everything else stays as on main: the fields
        of the parametrizations whose generic, its bases, or the parametrization itself have instructions (their
        fields can differ from the head ones), and the parametrizations whose redefined fields would lead back to
        them (copying them would never finish).
        """
        with self._lock:
            return self._get_redefined_field_names(model)

    def get_redefined_fields(self, model: type[BaseModel]) -> "collections.abc.Mapping[str, Any]":
        """Return the annotations of the fields that the copies of the model redefine (see get_redefined_field_names).

        They are the annotations that pydantic resolved for the head model, with the parametrizations that have
        instructions instead of the equal ones that pydantic built separately (Pydantic 2.11 builds another
        `Pair[str, int]` for `Box[str]` if `Box[str]` is created before `Pair[str, int]` itself).
        """
        with self._lock:
            return self._get_redefined_fields(model)

    def _get_redefined_fields(self, model: type[BaseModel]) -> "collections.abc.Mapping[str, Any]":
        redefined_field_names = self._get_redefined_field_names(model)
        if not redefined_field_names:
            return _NO_REDEFINED_FIELDS
        if model not in self._redefined_fields:
            # The fields are only redefined if they are known (see _get_parametrizations_built_by_pydantic)
            fields = cast("dict[str, FieldInfo]", _get_rebuilt_model_fields(model))
            self._redefined_fields[model] = types.MappingProxyType(
                {
                    name: _replace_in_type_positions(
                        _get_annotation_with_metadata(fields[name]), self._get_changed_parametrization
                    )
                    for name in redefined_field_names
                }
            )
        return self._redefined_fields[model]

    def _get_changed_parametrization(self, annotation: Any) -> Any:
        """Return the parametrization with instructions that is the same as the annotation, or the annotation"""
        # The type positions can hold anything, including unhashable values such as the list of args of Callable
        if not (lenient_issubclass(annotation, BaseModel) and _is_parametrization(annotation)) or (
            annotation in self._changed_parametrizations
        ):
            return annotation
        return next(
            (
                parametrization
                for parametrization in self._changed_parametrizations
                if _is_same_parametrization(annotation, parametrization)
            ),
            annotation,
        )

    def _get_redefined_field_names(self, model: type[BaseModel]) -> tuple[str, ...]:
        if not self._changed_parametrizations or not lenient_issubclass(model, BaseModel):
            return ()
        if model not in self._redefined_field_names:
            redefined_field_names = tuple(
                name
                for name, parametrizations in self._get_parametrizations_built_by_pydantic(model).items()
                if any(self._reaches_changed_parametrization(parametrization) for parametrization in parametrizations)
            )
            if redefined_field_names and self._is_in_cycle_with_redefined_fields(model):
                redefined_field_names = ()
            self._redefined_field_names[model] = redefined_field_names
        return self._redefined_field_names[model]

    def _get_parametrizations_built_by_pydantic(
        self, model: type[Union[BaseModel, Enum]]
    ) -> dict[str, frozenset[type[BaseModel]]]:
        """Return the parametrizations of generics that pydantic builds itself in the inherited fields of the model.

        They are the ones in the fields of a parametrization (e.g. `Pair[str, int]` in `Box[str]`) that are neither
        in the fields of its generic (e.g. `Pair[T, int]` in `Box`) nor in its type args, which the copy gets from
        the generator already.
        """
        if model in self._parametrizations_built_by_pydantic:
            return self._parametrizations_built_by_pydantic[model]
        parametrizations: dict[str, frozenset[type[BaseModel]]] = {}
        # E.g. enums, which the cycles can have
        if not lenient_issubclass(model, BaseModel):
            return parametrizations
        if not self._can_redefine_fields(model):
            return parametrizations
        origin = cast("type[BaseModel]", model.__pydantic_generic_metadata__["origin"])
        fields = _get_rebuilt_model_fields(model)
        origin_fields = _get_rebuilt_model_fields(origin)
        references = self._get_head_references(model)
        if fields is not None and origin_fields is not None and references.is_complete:
            # A parametrization inherits all of its fields from its generic
            for name, field in fields.items():
                models = _get_models_in_field(field)
                origin_models = _get_models_in_field(origin_fields[name])
                if models is None or origin_models is None:
                    # E.g. a recursive type alias: we cannot know what copying the field would request so the copy
                    # inherits it as on main
                    continue
                field_parametrizations = frozenset(
                    referenced_model
                    for referenced_model in models - origin_models - references.arg_models
                    if lenient_issubclass(referenced_model, BaseModel) and _is_parametrization(referenced_model)
                )
                if field_parametrizations:
                    parametrizations[name] = field_parametrizations
        self._parametrizations_built_by_pydantic[model] = parametrizations
        return parametrizations

    def _can_redefine_fields(self, model: type[BaseModel]) -> bool:
        return _is_parametrization(model) and not any(
            cls in self._changed_models for cls in model.__mro__ if lenient_issubclass(cls, BaseModel)
        )

    def _reaches_changed_parametrization(self, model: type[BaseModel]) -> bool:
        """Whether the model has instructions or leads to such parametrizations through the ones pydantic builds"""
        if model not in self._reaches_changed_parametrizations:
            models_to_visit = [model]
            visited_models: set[type[BaseModel]] = set()
            answer = False
            while models_to_visit:
                current_model = models_to_visit.pop()
                if current_model in visited_models:
                    continue
                visited_models.add(current_model)
                if self._get_changed_parametrization(current_model) in self._changed_parametrizations:
                    answer = True
                    break
                for parametrizations in self._get_parametrizations_built_by_pydantic(current_model).values():
                    models_to_visit.extend(parametrizations)
            self._reaches_changed_parametrizations[model] = answer
        return self._reaches_changed_parametrizations[model]

    def _is_in_cycle_with_redefined_fields(self, model: type[BaseModel]) -> bool:
        """Whether copying the model would request the model itself again if every candidate field was redefined.

        The fields are only redefined in the models outside of such cycles so the cycles stay the same as on main.
        """

        def get_successors(current_model: type[Union[BaseModel, Enum]]) -> "set[type[Union[BaseModel, Enum]]]":
            successors: set[type[Union[BaseModel, Enum]]] = set(self._get_requested_models(current_model))
            for parametrizations in self._get_parametrizations_built_by_pydantic(current_model).values():
                successors.update(
                    parametrization
                    for parametrization in parametrizations
                    if self._reaches_changed_parametrization(parametrization)
                )
            return successors

        if model not in self._models_in_cycles_with_redefined_fields:
            _find_strongly_connected_components(
                model, get_successors, self._models_in_cycles_with_redefined_fields, lambda _, is_cycle: is_cycle
            )
        return self._models_in_cycles_with_redefined_fields[model]

    def _get_head_references(self, model: type[BaseModel]) -> "_ModelReferences":
        if model in self._head_references:
            return self._head_references[model]
        references = _get_head_model_references(model)
        # The references that are not complete can change once the missing names get defined
        if references.is_complete:
            self._head_references[model] = references
        return references

    def _get_requested_models(self, model: type[Union[BaseModel, Enum]]) -> frozenset[type[Union[BaseModel, Enum]]]:
        if model not in self._requested_models:
            if lenient_issubclass(model, BaseModel):
                references = self._get_head_references(model)
                self._requested_models[model] = references.models | references.base_models
            else:
                self._requested_models[model] = frozenset()
        return self._requested_models[model]


def _get_cycle_name(cycle: "collections.abc.Iterable[type]") -> str:
    return ", ".join(sorted(model.__qualname__ for model in cycle))


def _get_latest_generic_args_error(cycle: frozenset[type[BaseModel]]) -> SchemaGenerationError:
    return SchemaGenerationError(
        f"Cadwyn cannot generate the versioned copies of {_get_cycle_name(cycle)} because they refer to each other "
        "and the same version also keeps the latest models in the type args of generics that refer to them, e.g. "
        "in `Page[Model]` within a model with instructions that refers to them only through such generics. This is "
        "a known limitation: Cadwyn decides about the generics of the models with instructions before it knows the "
        "routes, and a route that uses these models directly needs their own copies. Until it is lifted, use these "
        "models in the routes only through such generics."
    )


# Unique across all versions and all attempts so that the names of different copies never get mixed up, e.g.
# through the caches of typing: Optional[ForwardRef("X")] is cached so its ForwardRef("X") is shared.
_reference_cycle_member_ids = itertools.count()


def _get_sort_key_of_model(model: type[BaseModel]) -> tuple[str, str]:
    return model.__module__, model.__qualname__


@final
class _CycleCopier:
    """Generates the copies of all models in a reference cycle at once (see SchemaGenerator._copy_cycle).

    Copying the models goes through it instead of the generator. In the annotations, the models of the cycle are
    replaced by forward refs to their copies, which pydantic resolves once all of the copies exist. Everywhere else,
    e.g. in bases and default factories, they are replaced by their copies themselves, which already exist because
    of the order of creation. All other models are requested from the generator as usual.
    """

    __slots__ = "_forward_refs", "annotation_transformer", "copies", "model_bundle", "schema_generator"

    def __init__(self, schema_generator: "SchemaGenerator", cycle: frozenset[type[BaseModel]]) -> None:
        self.schema_generator = schema_generator
        self.model_bundle = schema_generator.model_bundle
        self.annotation_transformer = _AnnotationTransformer(self)
        self.copies: dict[type[BaseModel], type[BaseModel]] = {}
        self._forward_refs = {
            model: ForwardRef(f"_CadwynReferenceCycleMember{next(_reference_cycle_member_ids)}") for model in cycle
        }

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> Any:
        if not _is_versionable_model(model) or _unwrap_model(model) not in self._forward_refs:
            return self.schema_generator[model]
        member = cast("type[BaseModel]", _unwrap_model(model))
        if self.annotation_transformer.is_in_hard_position:
            return self._get_copy(member)
        return self._forward_refs[member]

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(base) and _unwrap_model(base) in self._forward_refs:
            return cast("type[_T_ANY_MODEL]", self._get_copy(_unwrap_model(base)))
        return self.schema_generator.change_version_of_base(base)

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        return _change_version_of_generic_metadata(self, self.schema_generator, model)

    def _get_template(self, model: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        if _unwrap_model(model) not in self._forward_refs:
            return self.schema_generator._get_template(model)
        return self[model]

    def _get_copy(self, model: Any) -> type[BaseModel]:
        if model not in self.copies:  # pragma: no cover # The order of creation makes sure that it exists
            raise CadwynError(f"The copy of {model.__qualname__} was needed before it was created")
        return self.copies[model]

    def copy(self, creation_order: list[type[BaseModel]]) -> dict[type[BaseModel], type[BaseModel]]:
        for model in creation_order:
            wrapper = self.schema_generator._get_wrapper_for_model(model)
            self.copies[model] = wrapper.generate_model_copy(self)
        copies_by_name = {
            self._forward_refs[model].__forward_arg__: model_copy for model, model_copy in self.copies.items()
        }
        for model_copy in self.copies.values():
            # The parent namespace stays with the copy, unlike the namespace passed to model_rebuild, so pydantic
            # can also resolve the forward refs when it rebuilds the copy or parametrizes it later
            parent_namespace = unpack_lenient_weakvaluedict(model_copy.__pydantic_parent_namespace__) or {}
            model_copy.__pydantic_parent_namespace__ = build_lenient_weakvaluedict(parent_namespace | copies_by_name)
        for model in creation_order:
            # Pydantic resolves the models nested in a model with the namespace of the outermost model so we cannot
            # leave it to pydantic to build the copies when they are first used (even with defer_build=True)
            self.copies[model].model_rebuild(raise_errors=True, _types_namespace={})
            if not self.copies[model].__pydantic_complete__:  # pragma: no cover # raise_errors=True raises before
                raise SchemaGenerationError(f"Cadwyn could not build the versioned copy of {model.__qualname__}")
        for model_copy in self.copies.values():
            # Pydantic has already resolved the forward refs so the annotations can refer to the copies themselves,
            # e.g. for typing.get_type_hints
            annotations = vars(model_copy)["__annotations__"]
            for name, annotation in annotations.items():
                annotations[name] = _replace_forward_refs(annotation, copies_by_name)
        return self.copies


@final
class _BareGenericCopies:
    """The copies of generics used without type args that SchemaGenerator._get_bare_model generates at once.

    These copies refer to each other (e.g. `children: list["Node[T]"]` within `Node`) through forward refs that
    pydantic resolves once all of them exist, like the copies of reference cycles.
    """

    __slots__ = "copies", "forward_refs"

    def __init__(self) -> None:
        self.forward_refs: dict[type[BaseModel], ForwardRef] = {}
        self.copies: dict[type[BaseModel], type[BaseModel]] = {}

    def add(self, model: type[BaseModel]) -> None:
        self.forward_refs[model] = ForwardRef(f"_CadwynBareGeneric{next(_reference_cycle_member_ids)}")

    def finish(self) -> None:
        copies_by_name = {self.forward_refs[model].__forward_arg__: copy for model, copy in self.copies.items()}
        for model_copy in self.copies.values():
            parent_namespace = unpack_lenient_weakvaluedict(model_copy.__pydantic_parent_namespace__) or {}
            model_copy.__pydantic_parent_namespace__ = build_lenient_weakvaluedict(parent_namespace | copies_by_name)
        for model, model_copy in self.copies.items():
            model_copy.model_rebuild(force=True, raise_errors=True, _types_namespace={})
            if not model_copy.__pydantic_complete__:  # pragma: no cover # raise_errors=True raises before
                raise SchemaGenerationError(f"Cadwyn could not build the versioned copy of {model.__qualname__}")
        for model_copy in self.copies.values():
            annotations = vars(model_copy)["__annotations__"]
            for name, annotation in annotations.items():
                annotations[name] = _replace_forward_refs(annotation, copies_by_name)


@final
class _BareGenericCopier:
    """Generates the copy of a generic model used without type args, e.g. `Page` instead of `Page[Item]`.

    Pydantic validates the parameters of such a model as their bounds, constraints, or defaults. So the copy is
    generic in this version's copies of its parameters (see _AnnotationTransformer._copy_typevar) instead of the
    original ones, which the template of the model keeps for its parametrizations, such as `Page[Item]`. The bases
    and the nested generics that use its parameters are copies of generics used without type args too, so pydantic
    never parametrizes anything with the copied parameters, and every copy keeps the name of its model.
    """

    __slots__ = "_bare_copies", "_model", "annotation_transformer", "model_bundle", "schema_generator"

    def __init__(
        self,
        schema_generator: "SchemaGenerator",
        bare_copies: _BareGenericCopies,
        model: type[BaseModel],
        versioned_parameters: dict[Any, Any],
    ) -> None:
        self.schema_generator = schema_generator
        self.model_bundle = schema_generator.model_bundle
        self.annotation_transformer = _AnnotationTransformer(self)
        self.annotation_transformer.versioned_parameters = versioned_parameters
        self._bare_copies = bare_copies
        self._model = model

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> Any:
        if not _is_versionable_model(model) or _unwrap_model(model) not in self._bare_copies.forward_refs:
            return self.schema_generator[model]
        member = cast("type[BaseModel]", _unwrap_model(model))
        if member in self._bare_copies.copies:
            return self._bare_copies.copies[member]
        if not self.annotation_transformer.is_in_hard_position:
            return self._bare_copies.forward_refs[member]
        if self.annotation_transformer.is_in_pydantic_metadata:
            raise SchemaGenerationError(
                f"Cadwyn cannot version the generic {member.__qualname__} used without type args because it "
                "refers to itself in metadata that pydantic uses, which needs the class itself. Use explicit "
                "type arguments."
            )
        # Like in Cadwyn 8.2.0, e.g. default factories and the metadata that pydantic ignores get the template
        return self.schema_generator._get_template(member)

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        # The bases that share the parameters of the model, e.g. `Page` of `class ItemPage(Page)`, validate them too
        if self.annotation_transformer._is_template_reference(base):
            return cast("type[_T_ANY_MODEL]", self[base])
        return self.schema_generator.change_version_of_base(base)

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        versioned_parameters = self.annotation_transformer.versioned_parameters
        parameters = model.__pydantic_generic_metadata__["parameters"]
        return {
            "origin": None,
            "args": (),
            "parameters": tuple(versioned_parameters.get(param, param) for param in parameters),
        }

    def _get_template(self, model: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        # The nested generics that share the parameters of the model, e.g. `page: Page[T]` in `Outer[T]`
        if self.annotation_transformer._is_template_reference(model):
            return cast("type[_T_ANY_MODEL]", self[model])
        return self.schema_generator._get_template(model)

    def copy(self) -> type[BaseModel]:
        wrapper = self.schema_generator._get_wrapper_for_model(self._model)
        return wrapper.generate_model_copy(self)
