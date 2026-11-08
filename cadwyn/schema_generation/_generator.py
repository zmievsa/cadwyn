import collections.abc  # noqa: TC003 # Needed by runtime annotation introspection.
import contextlib
import functools
import inspect
import threading
import typing
from collections.abc import Callable, Generator
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
    Union,
    assert_never,
    cast,
    final,
    overload,
)

from pydantic import BaseModel
from pydantic._internal._generics import PydanticGenericMetadata, replace_types

from cadwyn._utils import (
    lenient_issubclass,
)
from cadwyn.exceptions import CadwynError, InvalidGenerationInstructionError, SchemaGenerationError

from ._annotation_utils import (
    _contains_forward_refs,
    _eval_forward_refs,
    _get_annotations_without_evaluating,
    _get_typevar_fallbacks,
)
from ._annotations import (
    _AnnotationTransformer,
    _GenerationAnalysis,
    _get_models_validated_by_fields,
    _ModelReferences,
    _ReferencedModelCollector,
)
from ._bundle import (
    _ModelBundle,
)
from ._common import (
    _T_ANY_MODEL,
    _is_versionable_model,
    _unwrap_model,
)
from ._core_schema import (
    _get_classes_in_core_schema,
    _get_core_schema_definitions,
    _get_field_core_schemas,
)
from ._enums import (
    _EnumWrapper,
)
from ._generics import (
    _change_version_of_generic_metadata,
    _get_typevar_uses,
    _is_parametrization,
    _is_same_parametrization,
)
from ._instructions import (
    _copy_of_parent_has_field,
    _get_name_of_field,
    _get_parent_rename_error,
)
from ._models import (
    _BUILTIN_CONTAINER_TYPES,
    _PYDANTIC_CONSTRUCTORS,
    _get_subclasses_that_can_be_versioned,
    _is_pydantic_model_class,
    _iter_values_within,
    _only_changes_fields,
    _wrap_pydantic_model,
)
from ._references import (
    _BareGenericCopier,
    _BareGenericCopies,
    _CycleCopier,
    _find_strongly_connected_components,
    _get_cycle_name,
    _get_latest_generic_args_error,
    _get_sort_key_of_model,
)

if TYPE_CHECKING:
    from ._instructions import (
        _ParentRename,
    )
    from ._models import (
        _PydanticModelWrapper,
    )
    from ._references import (
        _ReferenceCycles,
    )


class _VersioningQuestion(NamedTuple):
    """A question about a model that SchemaGenerator._is_versioned answers"""

    model: type[Union[BaseModel, Enum]]
    # If False: whether the copy of the model would differ from the model.
    # If True: whether the copy of the model or of any of its subclasses would differ from them.
    includes_subclasses: bool


@final
class SchemaGenerator:
    __slots__ = (
        "_adds_references",
        "_bare_copies",
        "_bare_models",
        "_bare_models_being_generated",
        "_changed_models",
        "_copied_cycles",
        "_cycles_behind_latest_generic_args",
        "_cycles_being_copied",
        "_cycles_to_copy",
        "_generation_analyses",
        "_has_copied_eager_cycles",
        "_instructions_that_differ_from_head",
        "_lock",
        "_model_references",
        "_models_copied_eagerly",
        "_parent_renames",
        "_reference_cycles",
        "_root_models",
        "_route_versioning_thread",
        "_version",
        "_versioning_answers",
        "_versions_of_generic_args",
        "annotation_transformer",
        "concrete_models",
        "model_bundle",
        "typevar_copies",
    )

    def __init__(
        self,
        model_bundle: _ModelBundle,
        changed_models: "frozenset[type[Union[BaseModel, Enum]]]",
        reference_cycles: "_ReferenceCycles",
        instructions_that_differ_from_head: "collections.abc.Mapping[type[Union[BaseModel, Enum]], tuple[Any, ...]]",
        parent_renames: "tuple[_ParentRename, ...]",
        version: str,
    ) -> None:
        self.annotation_transformer = _AnnotationTransformer(self)
        self._bare_models: dict[type[BaseModel], type[BaseModel]] = {}
        # The TypeVars whose bounds, constraints, or defaults validate this version's models, by the original TypeVar
        # and its resolved bound, constraints, or default
        self.typevar_copies: dict[tuple[typing.TypeVar, tuple[Any, ...]], typing.TypeVar] = {}
        self._bare_models_being_generated: set[type[BaseModel]] = set()
        # The copies of generics used without type args that we are generating right now (see _get_bare_model)
        self._bare_copies: Union[_BareGenericCopies, None] = None
        self.model_bundle = model_bundle
        # The renames of this version and the newer ones, which the copies of the subclasses must follow
        self._parent_renames = parent_renames
        self._version = version
        self._changed_models = changed_models
        # The schema and enum instructions of each model in HeadVersion and in the versions that are newer than this one
        self._instructions_that_differ_from_head = instructions_that_differ_from_head
        self._model_references: dict[type[Union[BaseModel, Enum]], _ModelReferences] = {}
        self._versioning_answers: dict[_VersioningQuestion, bool] = {}
        self._reference_cycles = reference_cycles
        self._generation_analyses: dict[type[Union[BaseModel, Enum]], _GenerationAnalysis] = {}
        self._versions_of_generic_args: dict[type[BaseModel], bool] = {}
        # The reference cycles among the models that this version copies, or None for the models outside of them
        self._cycles_to_copy: dict[type[BaseModel], Union[frozenset[type[BaseModel]], None]] = {}
        # Whether the instructions of this version add references to models (see _adds_references_to_models)
        self._adds_references: Union[bool, None] = None
        # The reference cycles whose copies we are generating right now, innermost last
        self._cycles_being_copied: list[frozenset[type[BaseModel]]] = []
        # The models that this version copies in any case: the ones with instructions, which the generator copies
        # when it is created, and the ones that the routes use (see _add_route_models). Along with all of the models
        # that they refer to, they are the models copied eagerly (see _is_copied_eagerly)
        self._root_models: set[type[BaseModel]] = set(self.model_bundle.schemas)
        self._models_copied_eagerly: Union[set[type[BaseModel]], None] = None
        # The reference cycles that the generator has copied
        self._copied_cycles: set[frozenset[type[BaseModel]]] = set()
        # Whether the generator has copied a cycle that it copies eagerly (see _is_copied_eagerly)
        self._has_copied_eager_cycles = False
        # The thread that versions the routes of the version right now, if any (see versioning_routes)
        self._route_versioning_thread: Union[int, None] = None
        # The reference cycles that some generics keep the head models of in their type args
        self._cycles_behind_latest_generic_args: set[frozenset[type[BaseModel]]] = set()
        # Models are generated lazily (e.g. in migrate_response_body) so several threads can generate them at once
        self._lock = threading.RLock()
        self.concrete_models: dict[type[Union[BaseModel, Enum]], type[Union[BaseModel, Enum]]] = {}
        # Models are generated through __getitem__ so that the copies created along the way are reused
        # instead of being created again
        for model in [*self.model_bundle.schemas, *self.model_bundle.enums]:
            # Instructions describe templates too; they do not imply a bare validation use.
            self._get_template(model)

    @property
    def schema_generator(self) -> "SchemaGenerator":
        return self

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if not self._changed_models or not _is_versionable_model(model) or not issubclass(model, BaseModel):
            return self._get_template(model)
        original = _unwrap_model(model)
        if not original.__pydantic_generic_metadata__["parameters"]:
            return self._get_template(model)
        # The cache preserves the original class relationship, like concrete_models below.
        return cast("type[_T_ANY_MODEL]", self._get_bare_model(original))

    def _get_bare_model(self, original: type[BaseModel]) -> type[BaseModel]:
        """Return this version's copy of a generic model used without type args, e.g. `Page` instead of `Page[Item]`.

        It's the template unless the bounds, constraints, or defaults of the parameters that its fields use validate
        other models in this version than in head (see _BareGenericCopier).
        """
        with self._lock:
            if original in self._bare_models:
                return self._bare_models[original]
            if original in self._bare_models_being_generated:
                raise SchemaGenerationError(
                    f"Cadwyn cannot version the bare generic {original.__qualname__} because its type parameter "
                    "fallbacks refer back to it. Use explicit type arguments."
                )
            self._bare_models_being_generated.add(original)
            try:
                parameters = original.__pydantic_generic_metadata__["parameters"]
                template = self._get_template(original)
                extra_uses: set[typing.TypeVar] = set()
                used = _get_typevar_uses(template, extra_uses=extra_uses)
                args = [
                    self._change_version_of_bare_parameter(param, used[param]) if param in used else param
                    for param in parameters
                ]
                if all(arg is param for arg, param in zip(args, parameters, strict=True)):
                    self._bare_models[original] = template
                    return template
                if _is_parametrization(original):
                    changed_extra_uses = [
                        param.__name__
                        for param, arg in zip(parameters, args, strict=True)
                        if param in extra_uses and arg is not param
                    ]
                    if changed_extra_uses:
                        raise SchemaGenerationError(
                            f"Cadwyn cannot version the typed extra fields (`__pydantic_extra__`) of "
                            f"{template.__qualname__}: the bound, constraints, or default of "
                            f"{', '.join(changed_extra_uses)} change in version {self._version}, and Pydantic "
                            "versions differ in how they substitute parameters there. Use explicit type arguments "
                            "or a concrete annotation of `__pydantic_extra__`."
                        )
                    result = self._get_partially_parametrized_model(original, template, parameters, args)
                    self._bare_models[original] = result
                    return result
            finally:
                self._bare_models_being_generated.remove(original)
            is_outermost = self._bare_copies is None
            bare_copies = self._bare_copies = self._bare_copies or _BareGenericCopies()
            try:
                bare_copies.add(original)
                copier = _BareGenericCopier(self, bare_copies, original, dict(zip(parameters, args, strict=True)))
                bare_copies.copies[original] = copier.copy()
                if is_outermost:
                    bare_copies.finish()
                    self._bare_models |= bare_copies.copies
            finally:
                if is_outermost:
                    self._bare_copies = None
            return bare_copies.copies[original]

    def _get_partially_parametrized_model(
        self,
        original: type[BaseModel],
        template: type[BaseModel],
        parameters: tuple[Any, ...],
        args: list[Any],
    ) -> type[BaseModel]:
        """Return the copy of a generic that leaves some of its parameters without args, e.g. `Pair[int, T]`.

        Pydantic names such a generic after its args anyway, so it's the copy of the parametrization with this
        version's copies of the parameters, e.g. `Pair[int, T']`, under the name of the template.
        """
        specialized = replace_types(original, dict(zip(parameters, args, strict=True)))
        if not isinstance(specialized, type) or not issubclass(specialized, BaseModel):
            raise SchemaGenerationError(f"Cadwyn cannot safely parametrize the bare generic {original.__qualname__}")
        result = self._get_template(specialized)
        result.__name__, result.__qualname__, result.__doc__ = (
            template.__name__,
            template.__qualname__,
            template.__doc__,
        )
        result.model_rebuild(force=True)
        setattr(result, "__cadwyn_original_model__", original)  # noqa: B010 # Runtime marker on generated class.
        return result

    def _change_version_of_bare_parameter(self, param: typing.TypeVar, owners: set[type[BaseModel]]) -> typing.TypeVar:
        """Return the parameter whose bound, constraints, and default validate this version's models."""
        owner = self._get_typevar_owner(param, owners)
        if owner is None:
            return param
        transformer = self.annotation_transformer
        with transformer.typevar_namespace(owner):
            return transformer._change_version_of_typevar(param)

    def _get_typevar_owner(self, param: typing.TypeVar, owners: set[type[BaseModel]]) -> Union[type[BaseModel], None]:
        """Choose the namespace that validates a parameter, rejecting incompatible bare uses.

        Return None if its quoted fallbacks resolve to different classes for different users that are not versioned,
        so that the copy of each user resolves them in its own namespace.
        """
        owners = {_unwrap_model(owner) for owner in owners}
        owner = next(iter(owners))
        for value in _get_typevar_fallbacks(param)[1]:
            if not _contains_forward_refs(value):
                continue
            resolved = [_eval_forward_refs(value, context) for context in owners]
            if all(target == resolved[0] for target in resolved):
                continue
            collector = _ReferencedModelCollector()
            for target in resolved:
                collector.annotation_transformer.change_version_of_annotation(target)
            if any(self._is_versioned(model) for model in collector.models):
                raise SchemaGenerationError(
                    f"Cadwyn cannot version the bare generic {owner.__qualname__}: type parameter "
                    f"{param.__name__} resolves differently across nested model namespaces. "
                    "Use explicit type arguments."
                )
            return None
        return owner

    def _get_template(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if not _is_versionable_model(model):
            return model
        model = _unwrap_model(model)

        with self._lock:
            if not self._needs_copy(model):
                return model
            if model not in self.concrete_models:
                for cycle in self._cycles_being_copied:
                    if model in cycle:
                        raise CadwynError(
                            f"{model.__qualname__} was requested from outside of its reference cycle while Cadwyn "
                            "was copying the cycle. This is a bug in Cadwyn, or a hook of the model "
                            "(e.g. __init_subclass__) that asks Cadwyn for its versioned copies."
                        )
                cycle = self._get_cycle_to_copy(model)
                if cycle is None:
                    wrapper = self._get_wrapper_for_model(model)
                    model_copy = wrapper.generate_model_copy(self)
                    if lenient_issubclass(model_copy, BaseModel):
                        self._check_that_copy_follows_parent_renames(cast("type[BaseModel]", model), model_copy)
                    if self._copied_cycles and lenient_issubclass(model_copy, BaseModel):
                        self._check_copy_outside_of_cycles(cast("type[BaseModel]", model), model_copy)
                    self.concrete_models[model] = model_copy
                else:
                    self._copy_cycle(cycle)
            return cast("type[_T_ANY_MODEL]", self.concrete_models[model])

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        return self._get_template(base)

    def dependency_differs_from_head(self, dependency: Callable[..., Any]) -> bool:
        """Whether this version's copy of the dependency could validate requests differently from the dependency.

        That is, whether the dependency's parameters validate models or enums that this version changes, directly,
        through sub-dependencies, or through the types of the fields of models. Unlike the references of models
        (see _get_references), this only follows what pydantic validates: the resolved fields of each model, after
        its subclasses override them, so not the fields of bases that a subclass overrides, class variables,
        field attributes such as default factories, or the type args of generics and generic type aliases that no
        field uses. The typed extra fields (`__pydantic_extra__`) of models that allow extra fields count as fields.
        A model also differs if its bases have instructions, because its copy inherits from their copies, unless
        the instructions only change the fields that the model overrides.
        """
        if not self._instructions_that_differ_from_head:
            return False
        collector = _ReferencedModelCollector(expands_type_aliases=True)
        collector.annotation_transformer.record_dependency(dependency)
        return self.validates_models_that_differ_from_head(collector.models | collector.arg_models)

    def validates_models_that_differ_from_head(
        self, models: "collections.abc.Iterable[type[Union[BaseModel, Enum]]]"
    ) -> bool:
        """Whether the models validate differently in this version than in head.

        See dependency_differs_from_head for what counts.
        """
        if not self._instructions_that_differ_from_head:
            return False
        models_to_visit = list(models)
        visited_models: set[type[Union[BaseModel, Enum]]] = set()
        while models_to_visit:
            model = models_to_visit.pop()
            if model in visited_models:
                continue
            visited_models.add(model)
            if model in self._instructions_that_differ_from_head:
                return True
            if lenient_issubclass(model, BaseModel):
                if any(self._base_differs_from_head(model, base_index) for base_index in range(1, len(model.__mro__))):
                    return True
                models_to_visit.extend(_get_models_validated_by_fields(model))
        return False

    def _base_differs_from_head(self, model: type[BaseModel], base_index: int) -> bool:
        instructions = self._instructions_that_differ_from_head.get(model.__mro__[base_index])
        if not instructions:
            return False
        # The fields that the model or the bases between them override keep their own definitions in the copy
        overridden_names = {
            name for cls in model.__mro__[:base_index] for name in _get_annotations_without_evaluating(cls)
        }
        return any(not _only_changes_fields(instruction, overridden_names) for instruction in instructions)

    def _needs_copy(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether this version uses a copy of the model instead of the head model.

        Just like Cadwyn always did, we copy every model for every version except for the ones whose copies would
        be the same as the head models (see _is_versioned) and that we cannot or should not copy:
        * The enums, whose members must stay the same objects (e.g. strict models reject the members of a copy
          of their enum)
        * The models in reference cycles (e.g. `children: "list[Category]"`), whose copying would never finish.
          The copies of the models that refer to them use the head models too.
        We copy all other models even if their copies are the same as the head models because their copies
        differ from them in subtle ways that the older versions rely on, e.g. their OpenAPI names and their
        dunder methods such as __get_pydantic_json_schema__ that the copies do not have.
        The decisions are cached so the models that are defined after the generator has decided about the models
        that they refer to or inherit from do not change these decisions.
        """
        if self.model_bundle.models_with_config_instructions and lenient_issubclass(model, BaseModel):
            original = _unwrap_model(model)
            origin = original.__pydantic_generic_metadata__["origin"]
            for base in original.__mro__[1:]:
                if base in self.model_bundle.models_with_config_instructions and base is not origin:
                    raise InvalidGenerationInstructionError(
                        f'Cannot version config of "{base.__name__}" because "{model.__name__}" is a subclass '
                        "among the app's versioned models. Pydantic does not retain whether a child's config was "
                        "inherited or explicitly overridden. Put the config instruction on each subclass "
                        "(leaf model) instead."
                    )
        if self._cycles_to_copy.get(model) is not None:
            # We decided to copy its cycle so we keep that decision even if the subclasses that it depended on
            # change later (see _is_versioned)
            return True
        # Finding the cycles is much cheaper than looking at all of the subclasses in _is_versioned
        if lenient_issubclass(model, BaseModel) and not self._reference_cycles.is_in_cycle(model):
            return True
        return self._is_versioned(model)

    def _is_versioned(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether the copy of the model or of any of its subclasses could differ from them in some version.

        The copy of a model differs from it if the model has schema or enum instructions in any version, or if
        its bases differ, or if any of the models in its fields, annotations, generic args, etc. are versioned.
        The subclasses matter for two reasons. The copies of the subclasses must not inherit from the head model
        (e.g. its __init_subclass__ or __init__). And a model accepts the instances of its subclasses, so the head
        model would accept the head instances of a versioned subclass (e.g. with SerializeAsAny) while its copy
        rejects them. The copy of a model only differs from it because of its bases if the bases themselves
        differ, not if they only have subclasses that differ.
        If we do not know all of the models that the model refers to (see _ModelReferences.is_complete),
        we assume that it is versioned.
        The parametrizations of generics are not subclasses here (see _get_subclasses_that_can_be_versioned).
        The decision only depends on the references between the models and on the subclasses that exist when
        the generator first needs the decision, not on the order of generation. We do not keep a decision that
        depended on the subclasses that were still being created.
        """
        if not self._changed_models:
            # Nothing has instructions so we do not need to look into the models
            return False
        return self._reaches_changed_models(_VersioningQuestion(model, includes_subclasses=True))

    def _reaches_changed_models(self, question: "_VersioningQuestion") -> bool:
        if question in self._versioning_answers:
            return self._versioning_answers[question]
        visited_questions: set[_VersioningQuestion] = set()
        questions_to_visit = [question]
        answer = False
        depends_on_unknown_subclasses = False
        while questions_to_visit:
            current_question = questions_to_visit.pop()
            if current_question in visited_questions or self._versioning_answers.get(current_question) is False:
                continue
            visited_questions.add(current_question)
            if self._versioning_answers.get(current_question) or self._is_changed(current_question):
                answer = True
                break
            dependent_questions, are_all_known = self._get_dependent_questions(current_question)
            depends_on_unknown_subclasses |= not are_all_known
            questions_to_visit.extend(dependent_questions)
        if answer:
            self._versioning_answers[question] = True
        elif not depends_on_unknown_subclasses:
            # None of the visited questions lead to changed models either
            self._versioning_answers.update(dict.fromkeys(visited_questions, False))
        # Otherwise, we ask again next time because the subclasses that are still being created could change it
        return answer

    def _is_changed(self, question: "_VersioningQuestion") -> bool:
        if question.includes_subclasses:
            return False
        return question.model in self._changed_models or not self._get_references(question.model).is_complete

    def _get_dependent_questions(self, question: "_VersioningQuestion") -> "tuple[list[_VersioningQuestion], bool]":
        """Return the questions that the answer depends on, and whether we know all of them"""
        model = question.model
        if question.includes_subclasses:
            subclasses, are_all_known = _get_subclasses_that_can_be_versioned(model)
            return [
                _VersioningQuestion(model, includes_subclasses=False),
                *(_VersioningQuestion(_unwrap_model(subclass), includes_subclasses=True) for subclass in subclasses),
            ], are_all_known
        references = self._get_references(model)
        return [
            *(
                _VersioningQuestion(referenced_model, includes_subclasses=True)
                for referenced_model in references.models | references.arg_models
            ),
            *(_VersioningQuestion(base, includes_subclasses=False) for base in references.base_models),
        ], True

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        return _change_version_of_generic_metadata(self, self, model)

    def _should_version_generic_args(
        self, generic: type[BaseModel], generation_stack: list[type[Union[BaseModel, Enum]]]
    ) -> bool:
        """Whether the copy of the generic should use the copies of its type args instead of the head ones.

        We only version the args when they refer to models with schema or enum instructions so that all other
        generics stay exactly as they are. We also keep the head args when their copies could not be generated:
        when the args refer back to the generic (e.g. `children: Page["Category"]` within `Category`) or when
        generating their copies would recurse forever (e.g. `children: "list[Tree]"` within a versioned `Tree`).
        The decision only depends on the references between the models, not on the order of generation.
        """
        if not self._changed_models:
            # Nothing has instructions so no generic args need versioning and we do not need to look into them
            return False
        if generic not in self._versions_of_generic_args:
            references = self._get_references(generic)
            arg_models = references.arg_models
            # Versioning the args of a generic whose references are incomplete could request any model
            if not arg_models or not references.is_complete or self._is_referenced_through_args(generic):
                should_version = False
            else:
                analyses = [self._analyze_generation(model, generation_stack) for model in arg_models]
                should_version = all(analysis.terminates for analysis in analyses) and any(
                    analysis.refers_to_changed_models for analysis in analyses
                )
            self._versions_of_generic_args[generic] = should_version
        return self._versions_of_generic_args[generic]

    def _analyze_generation(
        self, model: type[Union[BaseModel, Enum]], generation_stack: list[type[Union[BaseModel, Enum]]]
    ) -> _GenerationAnalysis:
        if not self._needs_copy(model):
            # We use the head model so nothing gets generated, and it does not refer to models with instructions
            return _GenerationAnalysis(terminates=True, refers_to_changed_models=False)
        cycle = self._get_cycle_to_copy(model)
        if cycle is not None:
            return self._analyze_generation_of_cycle(cycle)
        if model in self._generation_analyses:
            return self._generation_analyses[model]
        if model in generation_stack:  # pragma: no cover # The cycles of copies are analyzed above
            # The models that generating this model requests lead back to it so its generation would never finish
            return _GenerationAnalysis(terminates=False, refers_to_changed_models=False)
        generation_stack.append(model)
        references = self._get_references(model)
        requested_models = set(references.models | references.base_models)
        if references.arg_models and self._should_version_generic_args(
            cast("type[BaseModel]", model), generation_stack
        ):
            requested_models |= references.arg_models
        analyses = [self._analyze_generation(requested_model, generation_stack) for requested_model in requested_models]
        generation_stack.pop()
        analysis = _GenerationAnalysis(
            terminates=references.is_complete and all(analysis.terminates for analysis in analyses),
            refers_to_changed_models=model in self._changed_models
            or any(analysis.refers_to_changed_models for analysis in analyses),
        )
        self._generation_analyses[model] = analysis
        return analysis

    def _analyze_generation_of_cycle(self, cycle: frozenset[type[BaseModel]]) -> _GenerationAnalysis:
        """Analyze the generation of the copies of a reference cycle that this version copies when they are needed.

        If this version copies the cycle in any case (see _is_copied_eagerly), where main failed, the generic args
        that refer to it use its copies like any other versioned args. Otherwise, they keep the head models, exactly
        as on main, which never copied such a cycle. Copying such a cycle later on its own (e.g. in
        migrate_response_body, where main failed) does not change them.
        """
        if self._is_copied_eagerly(cycle):
            # Its models need copies so they are versioned
            return _GenerationAnalysis(terminates=True, refers_to_changed_models=True)
        self._cycles_behind_latest_generic_args.add(cycle)
        return _GenerationAnalysis(terminates=False, refers_to_changed_models=False)

    @contextlib.contextmanager
    def versioning_routes(
        self, route_models: "collections.abc.Iterable[type[Union[BaseModel, Enum]]]"
    ) -> Generator[None, None, None]:
        """Register the models of the routes of the version while its routes are versioned (see _add_route_models)"""
        self._add_route_models(route_models)
        with self._lock:
            self._route_versioning_thread = threading.get_ident()
        try:
            yield
        finally:
            with self._lock:
                self._route_versioning_thread = None

    def _add_route_models(self, models: "collections.abc.Iterable[type[Union[BaseModel, Enum]]]") -> None:
        """Make the generator copy the cycles that the models of the routes refer to eagerly (see _is_copied_eagerly).

        Route generation registers all models of a version's routes before it versions any of them, so the
        decisions do not depend on the order of the routes.
        """
        with self._lock:
            route_models = {
                model for model in models if lenient_issubclass(model, BaseModel) and _is_versionable_model(model)
            }
            if route_models <= self._root_models:
                return
            # The models are only registered after the check so the next attempt to start the app fails the same way.
            # There are no cycles behind the latest generic args before the models copied eagerly are known because
            # finding such cycles needs them (see _analyze_generation_of_cycle)
            if self._models_copied_eagerly is not None:
                models_copied_eagerly = self._get_models_copied_eagerly(route_models, self._models_copied_eagerly)
                for cycle in self._cycles_behind_latest_generic_args:
                    if not cycle.isdisjoint(models_copied_eagerly):
                        # E.g. a model with instructions has `Page[Category]`, which kept the latest Category when the
                        # generator was created, and a route uses Category directly
                        raise _get_latest_generic_args_error(cycle)
                self._models_copied_eagerly = models_copied_eagerly
            self._root_models |= route_models

    def _is_copied_eagerly(self, cycle: frozenset[type[BaseModel]]) -> bool:
        """Whether this version copies the cycle in any case, where main failed.

        This version copies the models with instructions when its generator is created, the models of its routes,
        and every model that they refer to (except for the generic args that keep the head models). The decision
        only depends on the references between the models and on the models of the routes, which route generation
        registers before it versions them, so it does not depend on the order in which the models are requested.
        """
        if self._models_copied_eagerly is None:
            self._models_copied_eagerly = self._get_models_copied_eagerly(self._root_models)
        return not cycle.isdisjoint(self._models_copied_eagerly)

    def _get_models_copied_eagerly(
        self,
        root_models: "collections.abc.Iterable[type[BaseModel]]",
        models_copied_eagerly: "collections.abc.Set[type[BaseModel]]" = frozenset(),
    ) -> set[type[BaseModel]]:
        """Return the models copied eagerly along with the given ones: they and every model that they refer to"""
        models_to_visit = [model for model in root_models if self._needs_copy(model)]
        return self._get_models_reached_by_copying(models_to_visit, models_copied_eagerly)

    def _get_models_reached_by_copying(
        self,
        models_to_visit: list[type[BaseModel]],
        reached_models: "collections.abc.Set[type[BaseModel]]" = frozenset(),
    ) -> set[type[BaseModel]]:
        """Return the reached models, the models to visit, and every model whose copy copying them generates"""
        models = set(reached_models)
        while models_to_visit:
            model = models_to_visit.pop()
            if model not in models:
                models.add(model)
                models_to_visit.extend(self._get_models_that_copying_requests(model))
        return models

    def _is_in_cycle_used_by(
        self, cls: type, get_models_reached_by_copying: Callable[[], set[type[BaseModel]]]
    ) -> bool:
        """Whether the head model of the class (e.g. of a copy) is in a cycle whose copies the model uses"""
        cls = _unwrap_model(cast("type[BaseModel]", cls))
        if not _is_versionable_model(cls) or not lenient_issubclass(cls, BaseModel) or not self._needs_copy(cls):
            return False
        cycle = self._get_cycle_to_copy(cls)
        return cycle is not None and (
            self._is_copied_eagerly(cycle) or not cycle.isdisjoint(get_models_reached_by_copying())
        )

    def _check_that_copy_follows_parent_renames(self, model: type[BaseModel], model_copy: type[BaseModel]) -> None:
        """Fail if the copy of a subclass has a field that a parent renamed under another name next to the new one.

        Pydantic models can't drop the fields that they inherit, so a subclass keeps the field under the old name if
        it declares it itself (or changed it, which makes its copy declare it) or inherits it from another base, and
        inherits it under the new name from the parent. We decide from the copy that pydantic built, so only real
        fields count, not e.g. class vars. Only the fields that instructions added since the rename may have the old
        name, in the version of the rename and in the older ones.
        """
        for rename in self._parent_renames:
            parent, old_name = rename.parent, rename.old_name
            if model is parent or not issubclass(model, parent):
                continue
            new_name = _get_name_of_field(self.model_bundle.schemas[parent], rename.field_identity)
            if new_name in {None, old_name} or not self._gets_renamed_field_from_parent(
                model, model_copy, rename, new_name
            ):
                continue
            # The parent got a field with the old name back, or still inherits it, which the renames of the parent
            # itself report once all models of the version are copied
            if _copy_of_parent_has_field(model_copy, parent, old_name):
                continue
            for name in self._get_names_of_own_copies_of_renamed_field(model, rename):
                if name != new_name and name in model_copy.model_fields:
                    raise _get_parent_rename_error(model, model_copy, rename, name, new_name, self._version)
            if (
                old_name in model_copy.model_fields
                and old_name not in _get_annotations_without_evaluating(model_copy)
                # Like pydantic, we skip `object`
                and any(self._declared_field_before_rename(base, rename) for base in model.__mro__[1:-1])
            ):
                raise _get_parent_rename_error(model, model_copy, rename, old_name, new_name, self._version)

    def _gets_renamed_field_from_parent(
        self, model: type[BaseModel], model_copy: type[BaseModel], rename: "_ParentRename", new_name: str
    ) -> bool:
        """Whether the copy of the model has the field that the parent renamed under the new name.

        A field of the model itself with that name hides it, unless it is the model's copy of the parent's field
        (e.g. because the model changed it). So does a class before the copy of the parent in the MRO that declares
        the name, e.g. a first base.
        """
        if new_name not in model_copy.model_fields:
            return False
        if new_name in _get_annotations_without_evaluating(model_copy):
            wrapper = self.model_bundle.schemas.get(model)
            return wrapper is not None and any(
                field is not None and field.identity == rename.field_identity
                for field in (wrapper.fields.get(new_name), wrapper.fields_renamed_by_parents.get(new_name))
            )
        # The closest class that is the copy of the parent or declares the name. Like pydantic, we skip `object`
        closest = next(
            (
                cls
                for cls in model_copy.__mro__[1:-1]
                if vars(cls).get("__cadwyn_original_model__") is rename.parent
                or new_name in _get_annotations_without_evaluating(cls)
            ),
            None,
        )
        return closest is not None and vars(closest).get("__cadwyn_original_model__") is rename.parent

    def _get_names_of_own_copies_of_renamed_field(self, model: type[BaseModel], rename: "_ParentRename") -> list[str]:
        """Return the names of the fields of the model that are its own copies of the field that the parent renamed.

        That is the field that it declared under the old name before the version, and the parent's field itself if
        the model changed it, under their current names. A model without instructions keeps its declarations.
        """
        if model not in rename.models_with_instructions:
            return [rename.old_name] if rename.old_name in _get_annotations_without_evaluating(model) else []
        identities = {rename.field_identity, rename.field_identities_before_version[model].get(rename.old_name)}
        return [name for name, field in self.model_bundle.schemas[model].fields.items() if field.identity in identities]

    def _declared_field_before_rename(self, cls: type, rename: "_ParentRename") -> bool:
        """Whether the class declares a field with the old name that existed before the version of the rename"""
        if cls in rename.models_with_instructions:
            field = self.model_bundle.schemas[cast("type[BaseModel]", cls)].fields.get(rename.old_name)
            return field is not None and field.identity in rename.field_identities_of_version
        if rename.old_name not in _get_annotations_without_evaluating(cls):
            return False
        # Not a class var of a pydantic model
        return not lenient_issubclass(cls, BaseModel) or rename.old_name in cls.__pydantic_fields__

    def _check_copy_outside_of_cycles(self, model: type[BaseModel], model_copy: type[BaseModel]) -> None:
        """Check a copy outside of the copied cycles if it can use the head models of these cycles.

        Once a version copies a cycle eagerly, where main failed, we check every copy of the version. Otherwise,
        we only check the copies that reach the lazily copied cycles, so the copies that work as on main stay so.
        """
        get_models_reached_by_copying = functools.cache(
            lambda: self._get_models_reached_by_copying(list(self._get_models_that_copying_requests(model)))
        )
        if self._has_copied_eager_cycles or any(
            not cycle.isdisjoint(get_models_reached_by_copying()) for cycle in self._copied_cycles
        ):
            is_in_cycle_used_by = functools.partial(
                self._is_in_cycle_used_by, get_models_reached_by_copying=get_models_reached_by_copying
            )
            self._check_copy(
                model_copy,
                # The head classes in the cycles whose copies this version uses instead of them where the model is
                # used: the cycles that this version copies eagerly, and the ones that copying the model itself copies
                # (e.g. `category: Category` along with `related: Page[Category]`, whose args are the head models
                # when Category is copied lazily). Other cycles do not count because the generics around them keep
                # their head models, as on main.
                lambda cls: not hasattr(cls, "__cadwyn_original_model__") and is_in_cycle_used_by(cls),
                is_in_cycle_used_by,
                skipped_classes=set(self.concrete_models.values()),
            )

    def _check_copy(
        self,
        model_copy: type[BaseModel],
        is_latest_model: Callable[[type], bool],
        is_checked_field_model: Callable[[type], bool],
        *,
        is_latest_cycle_model: Union[Callable[[type], bool], None] = None,
        skipped_classes: "collections.abc.Set[type]" = frozenset(),
    ) -> None:
        """Raise if the copy validates, serializes, or defaults to head models that it should not use.

        Cadwyn only versions what it can see, so the copies would silently keep the head models wherever pydantic
        finds them on its own, e.g. in the return types of computed fields and serializers, in quoted names within
        other annotations (`list["Model"]`), or in generics whose args stay the head models. We look at what
        pydantic has actually built so we do not have to know every such place. We do not look into the schemas of
        skipped_classes, which are checked on their own. We check the defaults of the fields whose schemas use
        the classes for which is_checked_field_model is true, and the default values of all other fields for the
        head models for which is_latest_cycle_model (is_latest_model by default) is true.
        """
        if not model_copy.__pydantic_complete__:
            # E.g. defer_build=True. We only check the copies in the versions that copy cycles, where main failed
            model_copy.model_rebuild(raise_errors=False, _types_namespace={})
            if not model_copy.__pydantic_complete__:
                raise SchemaGenerationError(
                    f"Cadwyn cannot check the versioned copy of {model_copy.__qualname__} for the latest versions of "
                    "the models that refer to each other because pydantic cannot build it yet."
                )
        core_schema = model_copy.__pydantic_core_schema__
        skipped_classes = set(skipped_classes) - {model_copy}
        latest_models = [
            cls
            for cls in _get_classes_in_core_schema(core_schema, skipped_classes=skipped_classes)
            if is_latest_model(cls)
        ]
        if latest_models:
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copy of {model_copy.__qualname__} because it would use the "
                f"latest {_get_cycle_name(latest_models)}, which need versioning, where pydantic resolves them on its "
                "own: e.g. in the return types of computed fields or serializers, in names quoted within other "
                'annotations (`list["Model"]`, which you can write as `"list[Model]"` instead), or in the type args '
                "of generics that keep the latest models."
            )
        self._check_defaults(model_copy, core_schema, is_checked_field_model, is_latest_cycle_model or is_latest_model)

    def _check_defaults(
        self,
        model_copy: type[BaseModel],
        core_schema: Any,
        is_checked_field_model: Callable[[type], bool],
        is_latest_cycle_model: Callable[[type], bool],
    ) -> None:
        """Raise if the default of a field that uses the checked models could be a head model that needs versioning.

        Cadwyn does not version default values, so e.g. `settings: Settings = Settings()` would keep the head
        Settings and lose the changes of the older versions. We look into the default values themselves, but we
        cannot know what most default factories return, so we only accept the ones that create the copies.
        Enum members do not count because they are serialized by their values, as in all other models. The fields
        that do not use the checked models (e.g. `Any` or `dict`) keep their defaults as on main, except for the
        head models of the cycles in their values.
        """
        definitions = _get_core_schema_definitions(core_schema)
        field_schemas = _get_field_core_schemas(model_copy, core_schema, definitions)
        for name, field in model_copy.model_fields.items():
            field_schema = None if field_schemas is None else field_schemas.get(name)
            uses_checked_models = field_schema is None or any(
                is_checked_field_model(cls)
                for cls in _get_classes_in_core_schema(field_schema, definitions=definitions)
            )
            is_latest_value_model = self._is_latest_versioned_model if uses_checked_models else is_latest_cycle_model
            latest_values = [
                value
                for value in _iter_values_within(field.default)
                if isinstance(value, BaseModel) and is_latest_value_model(type(value))
            ]
            if latest_values:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copy of {model_copy.__qualname__} because the default of "
                    f"the field '{name}' of {model_copy.__qualname__} contains an instance of the latest "
                    f"{type(latest_values[0]).__qualname__}, which needs versioning. Please use a default factory "
                    "instead, e.g. the class of the field's model itself (`Field(default_factory=Model)`)."
                )
            if (
                uses_checked_models
                and field.default_factory is not None
                and not self._is_safe_default_factory(field.default_factory)
            ):
                raise SchemaGenerationError(
                    f"Cadwyn cannot verify what the default factory of the field '{name}' of "
                    f"{model_copy.__qualname__} returns, so its older versions could get the latest versions of the "
                    "models that need versioning. Please use `default_factory=list` or `default_factory=dict`, or "
                    "the class of the field's model itself (`Field(default_factory=Model)`)."
                )

    def _is_safe_default_factory(self, factory: Any) -> bool:
        """Whether the default factory provably creates the copies of this version rather than the head models"""
        factory = getattr(factory, "_original_callable", factory)
        # Subclasses of partial can override what calling them does
        if type(factory) is functools.partial:
            return self._is_safe_default_factory(factory.func) and not any(
                isinstance(value, BaseModel) and self._is_latest_versioned_model(type(value))
                for value in _iter_values_within([factory.args, factory.keywords])
            )
        if factory in _BUILTIN_CONTAINER_TYPES:
            return True
        if isinstance(factory, type):
            return self._is_class_of_this_version(factory)
        # Only pydantic's own constructors are known to create instances of the class that they are bound to
        return (
            inspect.ismethod(factory)
            and getattr(factory, "__func__", None) in _PYDANTIC_CONSTRUCTORS
            and isinstance(factory.__self__, type)
            and self._is_class_of_this_version(factory.__self__)
            # E.g. RootModel.model_construct calls the model_construct of the next class in the MRO
            and not any(
                factory.__name__ in vars(cls) for cls in factory.__self__.__mro__ if not _is_pydantic_model_class(cls)
            )
        )

    def _is_class_of_this_version(self, cls: type) -> bool:
        """Whether the class is a copy or a head model that this version uses as it is"""
        return hasattr(cls, "__cadwyn_original_model__") or (
            _is_versionable_model(cls) and not self._is_versioned(cast("type[BaseModel]", cls))
        )

    def _is_versioned_class(self, cls: type) -> bool:
        """Whether the class is a head model that needs versioning or a copy of one"""
        return _is_versionable_model(cls) and self._is_versioned(_unwrap_model(cast("type[BaseModel]", cls)))

    def _is_latest_versioned_model(self, cls: type) -> bool:
        """Whether the class is a head model that needs versioning"""
        return (
            _is_versionable_model(cls)
            and not hasattr(cls, "__cadwyn_original_model__")
            and self._is_versioned(cast("type[BaseModel]", cls))
        )

    def _is_referenced_through_args(self, generic: type[BaseModel]) -> bool:
        models_to_visit = list(self._get_references(generic).arg_models)
        visited_models = set()
        while models_to_visit:
            referenced_model = models_to_visit.pop()
            if _is_same_parametrization(referenced_model, generic):
                return True
            if referenced_model not in visited_models:
                visited_models.add(referenced_model)
                references = self._get_references(referenced_model)
                models_to_visit.extend(references.models | references.arg_models | references.base_models)
        return False

    def _get_cycle_to_copy(self, model: type[Union[BaseModel, Enum]]) -> Union[frozenset[type[BaseModel]], None]:
        """Return the reference cycle among the models that this version copies that the model is in, if any.

        Copying a model requests the copies of the models that it refers to, so copying a model in such a cycle
        on its own would never finish. We copy all models of the cycle at once instead (see _copy_cycle).
        The cycles are found statically in the references between the models that this version copies, which
        can differ from the head references because of instructions. Without instructions that add references
        to models, every such cycle is within a cycle among the head models, so we only look for cycles around
        the models in the head cycles, which keeps all other models exactly as on main.
        """
        if not lenient_issubclass(model, BaseModel):
            return None
        if model not in self._cycles_to_copy:
            if not self._reference_cycles.is_in_cycle(model) and not self._adds_references_to_models():
                self._cycles_to_copy[model] = None
            else:
                _find_strongly_connected_components(
                    model,
                    self._get_models_that_copying_requests,
                    self._cycles_to_copy,
                    lambda component, is_cycle: frozenset(component) if is_cycle else None,
                )
        return self._cycles_to_copy[model]

    def _get_models_that_copying_requests(self, model: type[BaseModel]) -> frozenset[type[BaseModel]]:
        """Return the models whose copies copying the model would generate.

        The models that this version does not copy end the chain of copying because the head models are used.
        """
        references = self._get_references(model)
        return frozenset(
            referenced_model
            for referenced_model in references.models | references.base_models
            if lenient_issubclass(referenced_model, BaseModel) and self._needs_copy(referenced_model)
        )

    def _adds_references_to_models(self) -> bool:
        """Whether the instructions of this version make any model refer to models that its head model does not.

        E.g. `schema(Category).field("parent").existed_as(type=Optional[Category])`. Only such instructions can
        create reference cycles that do not exist among the head models.
        """
        if self._adds_references is None:
            self._adds_references = False
            for model in self._changed_models:
                if lenient_issubclass(model, BaseModel) and model in self.model_bundle.schemas:
                    references = self._get_references(model)
                    head_references = self._reference_cycles.get_requested_models(model)
                    if not (references.models | references.base_models) <= head_references:
                        self._adds_references = True
                        break
        return self._adds_references

    def _copy_cycle(self, cycle: frozenset[type[BaseModel]]) -> None:
        """Generate the copies of all models of a reference cycle at once.

        Copying a model in the cycle refers to the other models of the cycle, and to itself, through forward refs
        in its annotations. Pydantic resolves them once all of the copies exist (see _CycleCopier).
        The copies are only published once all of them are complete and every check passed, so no other request
        can see them before, and a failed check leaves nothing behind: the next attempt fails the same way.
        """
        if (
            self._route_versioning_thread == threading.get_ident() and cycle in self._cycles_behind_latest_generic_args
        ):  # pragma: no cover # Recording the routes finds every model that versioning them requests (see conftest)
            raise _get_latest_generic_args_error(cycle)
        creation_order = self._get_creation_order_of_cycle(cycle)
        self._cycles_being_copied.append(cycle)
        try:
            copies = _CycleCopier(self, cycle).copy(creation_order)
            for model in creation_order:
                self._check_that_copy_follows_parent_renames(model, copies[model])
                # The defaults of the models in the cycle are checked for every model that needs versioning
                self._check_copy(
                    copies[model],
                    self._is_latest_versioned_model,
                    self._is_versioned_class,
                    is_latest_cycle_model=cycle.__contains__,
                )
        finally:
            self._cycles_being_copied.pop()
        is_copied_eagerly = self._is_copied_eagerly(cycle)
        if is_copied_eagerly:
            # The copies generated before could also use the head models of this cycle. They cannot reach the cycles
            # that are copied lazily because copying them would have copied these cycles first
            copied_classes = {*self.concrete_models.values(), *copies.values()}
            earlier_model_copies = [
                model_copy for model_copy in self.concrete_models.values() if lenient_issubclass(model_copy, BaseModel)
            ]
            for model_copy in earlier_model_copies:
                self._check_copy(
                    model_copy,
                    cycle.__contains__,
                    lambda cls: _unwrap_model(cast("type[BaseModel]", cls)) in cycle,
                    skipped_classes=copied_classes,
                )
        for model, model_copy in copies.items():
            self.concrete_models[model] = model_copy
        self._copied_cycles.add(cycle)
        if is_copied_eagerly:
            self._has_copied_eager_cycles = True

    def _get_creation_order_of_cycle(self, cycle: frozenset[type[BaseModel]]) -> list[type[BaseModel]]:
        """Return the order in which to create the copies of the models in the cycle, or raise if we cannot copy it.

        A copy must be created after the copies of the models in the cycle that it needs as classes, e.g. its bases
        or its default factories. Only its annotations can refer to the others through forward refs.
        """
        cycle_name = _get_cycle_name(cycle)
        models_needed_as_classes: dict[type[BaseModel], set[type[Union[BaseModel, Enum]]]] = {}
        for model in sorted(cycle, key=_get_sort_key_of_model):
            references = self._get_references(model)
            if not references.is_complete:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copies of {cycle_name} because they refer to each other "
                    f"and the annotations of {model.__qualname__} or of the functions that it uses refer to names "
                    "that are not defined yet or to type aliases that refer to themselves."
                )
            if (
                references.arg_models
                and self._should_version_generic_args(model, generation_stack=[])
                and references.arg_models & cycle
            ):  # pragma: no cover # _should_version_generic_args never versions args that lead back to the cycle
                raise CadwynError(f"The generic args of {model.__qualname__} lead back to its reference cycle")
            models_needed_as_classes[model] = set((references.hard_models | references.base_models) & cycle)

        creation_order = []
        while models_needed_as_classes:
            models_to_create = [model for model, needed in models_needed_as_classes.items() if not needed]
            if not models_to_create:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copies of {cycle_name} because they refer to each other "
                    "in positions that need the classes themselves rather than forward refs to them, e.g. in "
                    "default factories, in the metadata of Annotated, in type aliases, or in the args of generics "
                    "that are not from typing or collections.abc. Please refer to these models in the annotations "
                    "of fields instead."
                )
            for model in models_to_create:
                del models_needed_as_classes[model]
                for needed in models_needed_as_classes.values():
                    needed.discard(model)
            creation_order.extend(models_to_create)
        return creation_order

    def _is_copied_with_its_cycle(self, model: type[BaseModel]) -> bool:
        return self._needs_copy(model) and self._get_cycle_to_copy(model) is not None

    def _get_references(self, model: type[Union[BaseModel, Enum]]) -> _ModelReferences:
        """Return the models that copying the model requests from this generator (reference discovery).

        INVARIANT: discovery must find a superset of the models that copying the model actually requests.
        The decision to version generic args (the cycle rule) relies on these references to rule out the cycles
        that would make copying recurse forever, so any model that copying requests and discovery misses can lead
        to infinite recursion. This is why discovery runs the same function as copying
        (_change_version_of_model_references) and why tests/conftest.py checks the invariant for every model
        copied in the test suite. If discovery cannot know all of the references because some names are not defined
        yet (see _ModelReferences.is_complete), it must say so instead of returning a subset.
        Discovery must not change the model either because the model may never be copied.
        The references and the decisions based on them are cached for the lifetime of the generator, just like
        the copies, so a generic keeps the args that it got when the generator first needed it.
        """
        if model not in self._model_references:
            if lenient_issubclass(model, Enum):
                references = _ModelReferences(frozenset(), frozenset(), frozenset())
            elif model in self.model_bundle.schemas:
                references = self.model_bundle.schemas[model].get_references(self.model_bundle.schemas)
            else:
                # We do not wrap the model because wrapping copies parts of it and we may never need its copy
                references = self._reference_cycles.get_head_references(model)
            self._model_references[model] = references
        return self._model_references[model]

    @overload
    def _get_wrapper_for_model(self, model: type[BaseModel]) -> "_PydanticModelWrapper[BaseModel]": ...
    @overload
    def _get_wrapper_for_model(self, model: type[Enum]) -> "_EnumWrapper[Enum]": ...
    @overload
    def _get_wrapper_for_model(
        self, model: type[Union[BaseModel, Enum]]
    ) -> "Union[_PydanticModelWrapper[BaseModel], _EnumWrapper[Enum]]": ...

    def _get_wrapper_for_model(
        self, model: type[Union[BaseModel, Enum]]
    ) -> "Union[_PydanticModelWrapper[BaseModel], _EnumWrapper[Enum]]":
        model = _unwrap_model(model)

        if issubclass(model, BaseModel):
            if model in self.model_bundle.schemas:
                return self.model_bundle.schemas[model]
            # TODO: My god, what if one of its fields is in our concrete schemas and we don't use it? :O
            # TODO: Add an argument with our concrete schemas for _wrap_pydantic_model
            wrapper = _wrap_pydantic_model(model, self._reference_cycles.get_redefined_fields(model))
            self.model_bundle.schemas[model] = wrapper
        elif issubclass(model, Enum):
            if model in self.model_bundle.enums:
                return self.model_bundle.enums[model]
            wrapper = _EnumWrapper(model)
            self.model_bundle.enums[model] = wrapper
        else:
            assert_never(model)
        return wrapper
