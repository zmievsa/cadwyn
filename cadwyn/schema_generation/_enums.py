import contextlib
import enum
import pathlib
import types
import uuid
import weakref
from collections.abc import Callable, Generator, Sequence
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum, Flag, FlagBoundary
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Union,
    assert_never,
    cast,
    final,
)

from typing_extensions import (
    TypeVar,
)

from cadwyn._utils import (
    get_member_with_same_value,
    is_enum_looked_up_by_value,
    is_same_value,
)
from cadwyn.exceptions import InvalidGenerationInstructionError, SchemaGenerationError
from cadwyn.structure.enums import AlterEnumSubInstruction, EnumDidntHaveMembersInstruction, EnumHadMembersInstruction

from ._common import (
    _unwrap_model,
)

if TYPE_CHECKING:
    from ._schema_generator import (
        SchemaGenerator,
    )

_T_ENUM = TypeVar("_T_ENUM", bound=Enum)


def _apply_alter_enum_instructions(
    enums: "dict[type[Enum], _EnumWrapper[Enum]]",
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
    version_change_name: str,
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = enums[alter_enum_instruction.enum]
        if isinstance(alter_enum_instruction, EnumDidntHaveMembersInstruction):
            for member in alter_enum_instruction.members:
                if member not in enum.members:
                    raise InvalidGenerationInstructionError(
                        f'You tried to delete a member "{member}" from "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but it doesn\'t have such a member.',
                    )
                enum.members.pop(member)
        elif isinstance(alter_enum_instruction, EnumHadMembersInstruction):
            for member, member_value in alter_enum_instruction.members.items():
                if member in enum.members and is_same_value(enum.members[member], member_value):
                    raise InvalidGenerationInstructionError(
                        f'You tried to add a member "{member}" to "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but there is already a member with that name and value.',
                    )
                enum.members[member] = member_value
        else:
            assert_never(alter_enum_instruction)


class _DummyEnum(Enum):
    pass


class _DummyFlag(Flag):
    pass


# The enum machinery puts `_generate_next_value_` in the namespace of every enum, but the user can define it too.
# The copy needs it to compute the values of the members that `had()` adds with `auto()`
_USER_DEFINABLE_ENUM_ATTRIBUTES = frozenset({"_generate_next_value_"})

# The attributes such as `_member_map_` that the enum machinery puts on every enum. It computes them for the copy again.
# `type()` also makes its own `__dict__` and `__weakref__` for the copy, and the ones of the original don't apply to it
_ENUM_INTERNAL_ATTRIBUTES = (
    frozenset(_DummyEnum.__dict__) | {"__dict__", "__weakref__"}
) - _USER_DEFINABLE_ENUM_ATTRIBUTES

# The attributes such as `_flag_mask_` that the enum machinery puts on every Flag.
# It computes them for the copy again and doesn't accept them in a class body
_FLAG_INTERNAL_ATTRIBUTES = (
    frozenset(
        name
        for name in _DummyFlag.__dict__
        if name.startswith("_") and not name.startswith("__") and name.endswith("_")
    )
    - _USER_DEFINABLE_ENUM_ATTRIBUTES
)


# The attributes that the enum machinery puts on the members: on all of them, or on some (`_inverted_` caches `~member`
# for flags). It sets them for the members of the copy again
_ENUM_MEMBER_INTERNAL_ATTRIBUTES = frozenset({"_value_", "_name_", "__objclass__", "_sort_order_", "_inverted_"})

# The `__new__`s that the enum types of the standard library create their members with. They only need the values
_STDLIB_ENUM_NEWS = (
    object.__new__,
    *(
        getattr(enum_type, attr_name)
        for enum_type in (Enum, enum.IntEnum, enum.StrEnum, Flag, enum.IntFlag, enum.ReprEnum)
        for attr_name in ("_new_member_", "__new_member__")
        if attr_name in enum_type.__dict__
    ),
)

# The value of an unset slot, or of an attribute that the member doesn't have
_UNSET = object()

_MemberStateKey = tuple[Any, ...]


@final
class _FactsAboutLatestEnum:
    """What Cadwyn learns about the latest enum. It's the same in every version, so the wrapper's copies share it"""

    __slots__ = "changes_after_construction", "members_with_only_their_values"

    def __init__(self) -> None:
        self.members_with_only_their_values: Union[frozenset[str], None] = None
        self.changes_after_construction: Union[dict[str, tuple[tuple[_MemberStateKey, Any], ...]], None] = None


@final
class _EnumWrapper(Generic[_T_ENUM]):
    __slots__ = "_facts", "cls", "members", "name"

    def __init__(self, cls: type[_T_ENUM]) -> None:
        self.cls = _unwrap_model(cls)
        self.name = cls.__name__
        # `__members__` has the aliases too, in the order of the definition, so the copy defines them the same way
        self.members = {name: member.value for name, member in cls.__members__.items()}
        self._facts = _FactsAboutLatestEnum()

    def __deepcopy__(self, memo: Any) -> "_EnumWrapper[_T_ENUM]":
        result = _EnumWrapper(self.cls)
        result.members = self.members.copy()
        result._facts = self._facts
        memo[id(self)] = result
        return result

    def generate_model_copy(self, generator: "SchemaGenerator") -> type[_T_ENUM]:
        changes_after_construction = self._get_changes_after_construction()
        with self._explain_errors_of_custom_new() if _has_custom_new(self.cls) else contextlib.nullcontext():
            model_copy = self._create_enum(self.members)
        for name, changes in changes_after_construction.items():
            # Only the unchanged members keep them. Removing the canonical member of an alias gives the alias a new
            # name, and the alias gets the state that the constructor gives it
            if name in self.members and self.members[name] is self.cls.__members__[name].value:
                copied = model_copy.__members__[name]
                if copied.name == name:
                    _restore_member_state(copied, changes)
        model_copy.__cadwyn_original_model__ = self.cls  # ty: ignore[missing-attribute]
        _take_head_members_by_value(model_copy, self.cls)
        return model_copy

    def _get_members_with_only_their_values(self) -> frozenset[str]:
        if (names := self._facts.members_with_only_their_values) is None:
            names = self._facts.members_with_only_their_values = frozenset(
                name
                for name, member in self.cls.__members__.items()
                if _has_custom_new(self.cls) and _has_only_its_value(member)
            )
        return names

    def _get_changes_after_construction(self) -> dict[str, tuple[tuple[_MemberStateKey, Any], ...]]:
        if (changes := self._facts.changes_after_construction) is None:
            changes = self._facts.changes_after_construction = self._find_changes_after_construction()
        return changes

    def _find_changes_after_construction(self) -> dict[str, tuple[tuple[_MemberStateKey, Any], ...]]:
        """Return the state that the members of the enum got after the constructor made them, by member names.

        We build the enum again from the values of all of its members, so its constructor makes the members with the
        same args as in the class body. The state of the original members that differs from what the constructor
        made was set or deleted after that, e.g. a label assigned after the class was created or by a metaclass.
        """
        has_custom_new = _has_custom_new(self.cls)
        all_members = {name: member.value for name, member in self.cls.__members__.items()}
        with self._explain_errors_of_custom_new() if has_custom_new else contextlib.nullcontext():
            rebuilt = self._create_enum(all_members)
        if has_custom_new:
            self._check_that_new_recreates_the_members_from_their_values(rebuilt)
        rebuilt_again: Union[type[_T_ENUM], None] = None
        changes: dict[str, tuple[tuple[_MemberStateKey, Any], ...]] = {}
        for name, original in self.cls.__members__.items():
            if original.name != name:  # An alias. It shares the state of its canonical member
                continue
            original_state = _get_member_state(original)
            constructed_state = _get_member_state(rebuilt.__members__[name])
            member_changes: list[tuple[_MemberStateKey, Any]] = []
            for key in dict.fromkeys([*constructed_state, *original_state]):
                value, constructed = original_state.get(key, _UNSET), constructed_state.get(key, _UNSET)
                if _is_same_or_interchangeable(constructed, value):
                    continue
                if constructed is not _UNSET:
                    # Only an enum without a custom `__new__` gets here: the check above rejects such differences for
                    # the others. Its constructor got the same args as in the class body, so the value was changed
                    # after construction, unless the constructor makes a different value every time (e.g. a new
                    # object). Then we can't tell, and the copy keeps what its constructor makes
                    if rebuilt_again is None:
                        rebuilt_again = self._create_enum(all_members)
                    constructed_again = _get_member_state(rebuilt_again.__members__[name]).get(key, _UNSET)
                    if not _is_same_or_interchangeable(constructed_again, constructed):
                        continue
                member_changes.append((key, value))
            if member_changes:
                changes[name] = tuple(member_changes)
        return changes

    def _create_enum(self, members: dict[str, Any]) -> type[_T_ENUM]:
        enum_dict = Enum.__prepare__(self.name, self.cls.__bases__)
        namespace = self._get_initialization_namespace_for_enum(self.cls) | self._get_member_definitions(members)
        has_custom_new = _has_custom_new(self.cls)
        if has_custom_new:
            namespace["__new__"] = _make_new_that_keeps_members_with_only_their_values(self.cls)
            namespace["__init__"] = _make_init_that_skips_members_with_only_their_values(self.cls)
        for attr_name, attr in namespace.items():
            enum_dict[attr_name] = attr
        enum_dict["__doc__"] = self.cls.__doc__
        class_kwargs: dict[str, Any] = {}
        # A Flag's boundary is a class keyword, not an attribute of its body
        if issubclass(self.cls, Flag) and isinstance(boundary := getattr(self.cls, "_boundary_", None), FlagBoundary):
            class_kwargs["boundary"] = boundary
        enum_copy = cast("type[_T_ENUM]", type(self.name, self.cls.__bases__, enum_dict, **class_kwargs))
        if has_custom_new:
            self._restore_new_and_init(enum_copy)
        return enum_copy

    def _restore_new_and_init(self, enum_copy: type[_T_ENUM]) -> None:
        """Give the copy the __new__ and __init__ of the enum, e.g. for the flags that `_missing_` creates later"""
        new_member = self.cls._new_member_  # ty: ignore[missing-attribute]
        enum_copy._new_member_ = new_member  # ty: ignore[missing-attribute]
        if "__new_member__" in self.cls.__dict__:
            enum_copy.__new_member__ = new_member  # ty: ignore[missing-attribute]
        else:
            del enum_copy.__new_member__  # ty: ignore[missing-attribute]
        if "__init__" in self.cls.__dict__:
            enum_copy.__init__ = self.cls.__dict__["__init__"]
        else:
            del enum_copy.__init__

    def _get_member_definitions(self, members: dict[str, Any]) -> dict[str, Any]:
        original_members = self.cls.__members__
        members_with_only_their_values = self._get_members_with_only_their_values()
        definitions: dict[str, Any] = {}
        for name, value in members.items():
            if name in original_members and value is original_members[name].value:
                if name in members_with_only_their_values:
                    # We know everything about such a member, so we take its exact value instead of calling __new__
                    # with it, which can need other args (e.g. a precision that changes the exponent of a decimal)
                    definitions[name] = enum.member(_ValueOfMemberWithOnlyItsValue(value))
                else:
                    # A member of the original stays a member even if its value is a function or a class (`@member`)
                    definitions[name] = enum.member(value.value if isinstance(value, Enum) else value)
            else:
                # The value that `had()` got, which the copy defines like the class body of the enum would
                definitions[name] = value.value if isinstance(value, Enum) else value
        return definitions

    @contextlib.contextmanager
    def _explain_errors_of_custom_new(self) -> Generator[None, None, None]:
        try:
            yield
        except Exception as e:
            raise SchemaGenerationError(
                f'Cadwyn can\'t copy "{self.name}" for its versions because its __new__ failed to create the members '
                f"from their values: {e!r}. {_CUSTOM_NEW_ADVICE}"
            ) from e

    def _check_that_new_recreates_the_members_from_their_values(self, rebuilt_members: type[_T_ENUM]) -> None:
        """Make sure that the custom __new__ of the enum recreates its members from their values.

        Cadwyn only knows the values of the members, not the arguments that __new__ got in the class body. So we build
        the enum from the values of all of its members and compare it to the enum. If they're the same, the copies
        that the instructions change are what the class body of their version would give.
        """
        original_members = self.cls.__members__
        for name, original_member in original_members.items():
            if not _is_same_member(rebuilt_members.__members__[name], original_member):
                raise SchemaGenerationError(
                    f"Cadwyn can't copy \"{self.name}\" for its versions because its __new__ doesn't recreate the "
                    f'member "{name}" from its value {original_member.value!r}. {_CUSTOM_NEW_ADVICE}'
                )

    @staticmethod
    def _get_initialization_namespace_for_enum(enum_cls: type[Enum]) -> dict[str, Any]:
        mro_without_the_class_itself = enum_cls.mro()[1:]

        mro_dict = {}
        for cls in reversed(mro_without_the_class_itself):
            mro_dict.update(cls.__dict__)

        own_slots = _get_own_slot_names(enum_cls)
        namespace = {
            k: v
            for k, v in enum_cls.__dict__.items()
            # `_member_map_` has the aliases too
            if k not in enum_cls._member_map_
            and k not in _ENUM_INTERNAL_ATTRIBUTES
            and k not in _FLAG_INTERNAL_ATTRIBUTES
            # `type()` makes the descriptors of the slots of the copy from its `__slots__`
            and k not in own_slots
            # A `_generate_next_value_` that the enum takes from a mixin is the mixin's function on Python 3.11, but
            # the copy would take the one of its base enum type
            and (k in _USER_DEFINABLE_ENUM_ATTRIBUTES or k not in mro_dict or mro_dict[k] is not v)
        }
        # Python consumes `__slots__` when it creates the class, e.g. when it's an iterator, so we take the names of
        # its slots from their descriptors
        if "__slots__" in enum_cls.__dict__:
            namespace["__slots__"] = own_slots
        # The enum machinery saves the custom __new__ of the enum as `__new_member__`, and only takes `__new__`
        # from the class body
        if "__new_member__" in enum_cls.__dict__:
            namespace["__new__"] = enum_cls.__dict__["__new_member__"]
        return {k: _wrap_enum_nonmember(enum_cls, k, v) for k, v in namespace.items()}


# The checks of the enum machinery for the names that never become members. They fail at import if they change
_is_enum_dunder = enum._is_dunder  # ty: ignore[missing-attribute]
_is_enum_sunder = enum._is_sunder  # ty: ignore[missing-attribute]
_is_enum_private = enum._is_private  # ty: ignore[missing-attribute]

_CUSTOM_NEW_ADVICE = (
    "Cadwyn only knows the values of the members, not the arguments that __new__ got. "
    "Compute the other attributes of the members from their values instead, e.g. with a property."
)


def _has_custom_new(enum_cls: type[Enum]) -> bool:
    """Whether the enum creates its members with a `__new__` of the user's, which can need more than the values"""
    new_member = enum_cls._new_member_  # ty: ignore[missing-attribute]
    member_type_new = enum_cls._member_type_.__new__  # ty: ignore[missing-attribute]
    return new_member is not member_type_new and not any(new_member is new for new in _STDLIB_ENUM_NEWS)


@final
class _ValueOfMemberWithOnlyItsValue:
    """The value of a member of the original enum that has no state other than its value.

    The `__new__` and `__init__` of the copy create such a member from the value without calling the enum's own ones.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _get_value_of_member_with_only_its_value(args: tuple[Any, ...]) -> Union[_ValueOfMemberWithOnlyItsValue, None]:
    # The enum machinery passes the definition of a member as the args, and wraps them in a tuple for tuple enums
    if len(args) == 1 and type(args[0]) is tuple:
        args = args[0]
    if len(args) == 1 and isinstance(args[0], _ValueOfMemberWithOnlyItsValue):
        return args[0]
    return None


def _make_new_that_keeps_members_with_only_their_values(enum_cls: type[Enum]) -> Callable[..., Any]:
    new_member = enum_cls._new_member_  # ty: ignore[missing-attribute]
    member_type = enum_cls._member_type_  # ty: ignore[missing-attribute]

    def __new__(cls: type[Enum], *args: Any) -> Any:  # noqa: N807
        value = _get_value_of_member_with_only_its_value(args)
        if value is None:
            return new_member(cls, *args)
        member = object.__new__(cls) if member_type is object else member_type.__new__(cls, value.value)
        member._value_ = value.value
        return member

    return __new__


def _make_init_that_skips_members_with_only_their_values(enum_cls: type[Enum]) -> Callable[..., None]:
    init = enum_cls.__init__

    def __init__(self: Enum, *args: Any, **kwargs: Any) -> None:  # noqa: N807
        # `object.__init__` does nothing, but rejects args when it's called directly
        if init is not object.__init__ and _get_value_of_member_with_only_its_value(args) is None:
            init(self, *args, **kwargs)

    return __init__


def _has_only_its_value(member: Enum) -> bool:
    """Whether the member has no attributes or set slots, and its mixin data (e.g. its `str`) is its value"""
    if any(value is not _UNSET for value in _get_member_state(member).values()):
        return False
    member_type = type(member)._member_type_  # ty: ignore[missing-attribute]
    return member_type is object or member_type.__eq__(member, member._value_) is True


def _is_same_member(rebuilt_member: Enum, original_member: Enum) -> bool:
    rebuilt_state, original_state = _get_member_state(rebuilt_member), _get_member_state(original_member)
    member_type = type(original_member)._member_type_  # ty: ignore[missing-attribute]
    return (
        # E.g. not an alias of another member
        rebuilt_member.name == original_member.name
        and _is_same_or_interchangeable(rebuilt_member._value_, original_member._value_)
        # Attributes absent from the reconstruction can have been assigned after class creation, e.g. by a
        # metaclass. They are restored separately; attributes that the constructor does set must still match.
        and rebuilt_state.keys() <= original_state.keys()
        and all(
            value is _UNSET or _is_same_or_interchangeable(value, original_state[k])
            for k, value in rebuilt_state.items()
        )
        # E.g. the `str` that `str.__new__` got, which can differ from the value
        and (member_type is object or member_type.__eq__(rebuilt_member, original_member) is True)
    )


# The immutable types whose equal values with the same text are interchangeable, e.g. a `date` that `__init__` makes
# again. Their values are compared exactly (`type(value) is ...`), not their subclasses
_INTERCHANGEABLE_VALUE_TYPES = frozenset(
    {
        date,
        time,
        datetime,
        timedelta,
        uuid.UUID,
        pathlib.PurePosixPath,
        pathlib.PureWindowsPath,
        pathlib.PosixPath,
        pathlib.WindowsPath,
    }
)


def _is_same_or_interchangeable(rebuilt: Any, original: Any) -> bool:
    """Whether the rebuilt value is the same as the original one, or an equal value of a type in which they're the same.

    Everything else, e.g. containers, decimals (`Decimal("1.0") == Decimal("1.00")`), and the objects of other classes,
    is compared with `is_same_value`, because equal values of them can serialize differently.
    """
    if is_same_value(rebuilt, original):
        return True
    value_type = type(original)
    if type(rebuilt) is not value_type or value_type not in _INTERCHANGEABLE_VALUE_TYPES or rebuilt != original:
        return False
    if value_type in {time, datetime}:
        return (
            rebuilt.fold == original.fold
            and _is_same_timezone(rebuilt.tzinfo, original.tzinfo)
            and rebuilt.isoformat() == original.isoformat()
        )
    return str(rebuilt) == str(original)


def _is_same_timezone(rebuilt: Any, original: Any) -> bool:
    if rebuilt is original:
        return True
    return (
        type(rebuilt) is type(original) is timezone
        and rebuilt.utcoffset(None) == original.utcoffset(None)
        and rebuilt.tzname(None) == original.tzname(None)
    )


def _get_member_state(member: Enum) -> dict[_MemberStateKey, Any]:
    """Return the attributes of the member in its `__dict__` and in the slots of its classes, except enum internals"""
    state: dict[_MemberStateKey, Any] = {
        ("attribute", k): v for k, v in vars(member).items() if k not in _ENUM_MEMBER_INTERNAL_ATTRIBUTES
    }
    mro = type(member).__mro__
    for key in _get_slot_keys(type(member)):
        try:
            state[key] = mro[key[1]].__dict__[key[2]].__get__(member, type(member))
        except AttributeError:  # The slot isn't set
            state[key] = _UNSET
    return state


# Only plain data: a slot descriptor refers to its class, which would keep the key of its class alive
_SLOT_KEYS_BY_CLASS: "weakref.WeakKeyDictionary[type, tuple[_MemberStateKey, ...]]" = weakref.WeakKeyDictionary()


def _get_slot_keys(cls: type) -> tuple[_MemberStateKey, ...]:
    """Return the keys of the slots of the instances of the class in their state: `("slot", position, name)`.

    A subclass can declare a slot with the same name as a slot of its base, and the member has both. The copy has the
    same bases as the original, so we tell the slots apart by the position of their class in the MRO.
    """
    if (keys := _SLOT_KEYS_BY_CLASS.get(cls)) is None:
        keys = _SLOT_KEYS_BY_CLASS[cls] = tuple(
            ("slot", position, slot_name)
            for position, base in enumerate(cls.__mro__)
            for slot_name in _get_own_slot_names(base)
        )
    return keys


def _get_own_slot_names(cls: type) -> tuple[str, ...]:
    """Return the names of the slots that the class itself declares, as Python stores them (private names mangled)"""
    return tuple(
        name
        for name, descriptor in cls.__dict__.items()
        if isinstance(descriptor, types.MemberDescriptorType) and descriptor.__objclass__ is cls
    )


def _restore_member_state(copied: Enum, changes: tuple[tuple[_MemberStateKey, Any], ...]) -> None:
    """Set the state that the latest member got after construction on its copy, or delete what was deleted"""
    for key, value in changes:
        if key[0] == "attribute":
            if value is _UNSET:
                vars(copied).pop(key[1], None)
            else:
                vars(copied)[key[1]] = value
        else:
            # The descriptor itself, because a subclass can shadow a slot of its base with a slot of the same name
            descriptor = type(copied).__mro__[key[1]].__dict__[key[2]]
            if value is not _UNSET:
                descriptor.__set__(copied, value)
            else:
                with contextlib.suppress(AttributeError):  # The constructor of the version didn't set it either
                    descriptor.__delete__(copied)


def _wrap_enum_nonmember(enum_cls: type[Enum], name: str, value: Any) -> Any:
    """Keep the attributes that aren't members (e.g. `nonmember(10)`) from becoming members of the copy.

    The enum machinery doesn't make members of dunder, sunder, and private names, and expects their values unwrapped.
    """
    if _is_enum_dunder(name) or _is_enum_sunder(name) or _is_enum_private(enum_cls.__name__, name):
        return value
    return enum.nonmember(value)


def _take_head_members_by_value(enum_copy: type[Enum], head_enum: type[Enum]) -> None:
    """Make the copy of a plain enum or flag take the members of its head enum that have the values of its members.

    The responses that the endpoints return have the head members, and so do the defaults of params. Pydantic looks up
    the members of `str`, `int`, and `float` enums by their values, so we leave them as they are. For plain enums and
    flags, it only calls `_missing_`, so their copies would reject the head members. The enum's own `_missing_` gets
    the head members that the copy has no member with the same value for, as before.
    """
    if is_enum_looked_up_by_value(head_enum):
        return
    inherited_missing = enum_copy._missing_

    def _missing_(cls: type[Enum], value: object) -> Any:
        if isinstance(value, head_enum) and (member := get_member_with_same_value(cls, value)) is not None:
            return member
        return inherited_missing(value)

    enum_copy._missing_ = classmethod(_missing_)  # ty: ignore[invalid-assignment]
