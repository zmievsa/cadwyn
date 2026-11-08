import collections.abc
import dataclasses
import functools
import inspect
import math
import sys
import types
import typing
from collections.abc import Callable
from decimal import Decimal
from enum import Enum, Flag
from inspect import signature
from typing import TYPE_CHECKING, Any, Concatenate, Generic, TypeVar, Union

import annotated_types
import fastapi.params
import pydantic
from pydantic._internal._decorators import DecoratedType, unwrap_wrapped_function
from typing_extensions import ParamSpec, override

if TYPE_CHECKING:
    from typing_extensions import TypeIs

Sentinel: Any = object()

_P = ParamSpec("_P")
_R = TypeVar("_R")
_SourceSelf = TypeVar("_SourceSelf")


_P_T = TypeVar("_P_T")
_P_R = TypeVar("_P_R")

# Match FastAPI's version-dependent coroutine detection. Before Python 3.13,
# asyncio also recognizes the legacy _is_coroutine marker. Using inspect instead
# would make Cadwyn treat some dependencies as synchronous while FastAPI awaits them.
# Keep this split aligned with FastAPI, even while ty reports asyncio's API as deprecated.
if sys.version_info >= (3, 13):  # pragma: no cover
    from inspect import iscoroutinefunction
else:  # pragma: no cover
    from asyncio import iscoroutinefunction  # noqa: F401


UnionType = type(int | str) | type(Union[int, str])
DATACLASS_SLOTS: dict[str, Any] = {"slots": True}
DATACLASS_KW_ONLY: dict[str, Any] = {"kw_only": True}


def get_name_of_function_wrapped_in_pydantic_validator(func: Any) -> str:
    if hasattr(func, "wrapped"):
        return get_name_of_function_wrapped_in_pydantic_validator(func.wrapped)
    if hasattr(func, "__func__"):
        return get_name_of_function_wrapped_in_pydantic_validator(func.__func__)
    return func.__name__


class classproperty(Generic[_P_T, _P_R]):  # noqa: N801
    def __init__(self, func: Callable[[_P_T], _P_R]) -> None:
        super().__init__()
        self.func = func

    def __get__(self, obj: Any, cls: _P_T) -> _P_R:
        return self.func(cls)


class PlainRepr(str):
    """String class where repr doesn't include quotes"""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return str(self)


def same_method_definition_as_in(
    t: Callable[Concatenate[_SourceSelf, _P], _R],
) -> Callable[[Callable[..., _R]], Callable[Concatenate[object, _P], _R]]:
    def decorator(f: Callable[..., _R]) -> Callable[Concatenate[object, _P], _R]:
        f.__signature__ = signature(t)  # ty: ignore[unresolved-attribute]
        return f

    return decorator


def fully_unwrap_decorator(
    func: "DecoratedType[object]",
    is_pydantic_v1_style_validator: Any,
) -> Callable[..., object]:
    func = unwrap_wrapped_function(func)
    if is_pydantic_v1_style_validator and func.__closure__:
        func = func.__closure__[0].cell_contents
    return unwrap_wrapped_function(func)


_SubclassT = TypeVar("_SubclassT")


# Unlike issubclass, accepts non-class annotations (unions, generic aliases, etc.) and returns False for them
def lenient_issubclass(
    cls: object, other: Union[type[_SubclassT], tuple[type[_SubclassT], ...]]
) -> "TypeIs[type[_SubclassT]]":
    try:
        # Non-class annotations raise TypeError, which is handled below
        return issubclass(cls, other)  # ty: ignore[invalid-argument-type]
    except TypeError:  # pragma: no cover
        return False


def present_private_submodules_as_package(package_name: str) -> None:
    """Make a package look like the single module it was split from.

    `cadwyn.structure.versions` and `cadwyn.schema_generation` used to be single modules. Everything defined
    or imported in their private submodules stays importable from the package, and every class, function,
    and type variable defined there keeps the package as its `__module__`. So error messages, reprs,
    pickling, `typing.get_type_hints()`, and documentation tools behave as they did before the split.
    """
    namespace = vars(sys.modules[package_name])
    submodules = [
        value
        for value in namespace.values()
        if isinstance(value, types.ModuleType) and value.__name__.startswith(f"{package_name}._")
    ]
    for submodule in submodules:
        for name, value in vars(submodule).items():
            if name.startswith("__"):
                continue
            if _is_module_level_definition(value) and value.__module__ == submodule.__name__:
                _move_definition(value, submodule.__name__, package_name)
            if namespace.setdefault(name, value) is not value:  # pragma: no cover # Guards future module splits
                raise ImportError(f"{package_name}.{name} differs from {submodule.__name__}.{name}")


def _is_module_level_definition(value: object) -> bool:
    return isinstance(value, type | types.FunctionType | TypeVar | ParamSpec) or inspect.isfunction(
        getattr(value, "__wrapped__", None)
    )


def _move_definition(definition: Any, old_module: str, new_module: str) -> None:
    definition.__module__ = new_module
    members = [getattr(definition, "__wrapped__", None)]
    if isinstance(definition, type):
        for member in vars(definition).values():
            if isinstance(member, classmethod | staticmethod):
                members.append(member.__func__)
            elif isinstance(member, property):
                members.extend((member.fget, member.fset, member.fdel))
            elif isinstance(member, functools.cached_property | classproperty):
                members.append(member.func)
            else:
                members.append(member)
    for member in members:
        if isinstance(member, type | types.FunctionType) and member.__module__ == old_module:
            _move_definition(member, old_module, new_module)


def _callable_name(call: Callable[..., object]) -> str:
    name = getattr(call, "__name__", None)
    return name if isinstance(name, str) else type(call).__name__


def is_sqlalchemy_model(model: type) -> bool:
    # __mapper__ covers imperative mappings; _sa_registry also covers declarative
    # bases and SQLModel classes without table=True. Avoid importing optional dependencies.
    return hasattr(model, "__mapper__") or hasattr(model, "_sa_registry")


def _get_type_alias_value(alias: Any) -> Any:
    # The values of typing.TypeAliasType (but not of typing_extensions.TypeAliasType) are evaluated lazily so they
    # can contain names that are only imported for type checking
    if sys.version_info >= (3, 14) and hasattr(alias, "evaluate_value"):  # pragma: no cover
        from annotationlib import Format, call_evaluate_function

        return call_evaluate_function(alias.evaluate_value, Format.FORWARDREF)
    return alias.__value__


# We look types up by their ids in tables built here, because hashing or comparing a type, or reading its attributes,
# can run the code of the user's metaclass
_SCALAR_TYPE_IDS = frozenset(map(id, (type(None), bool, int, float, complex, str, bytes, Decimal)))
# The dataclasses of pydantic, FastAPI, and annotated_types that can be in field attributes and endpoint attributes,
# with the fields that hold all of their state
_ALLOWED_TYPE_FIELDS: dict[int, tuple[str, ...]] = {
    id(cls): tuple(field.name for field in dataclasses.fields(cls))
    for cls in (
        pydantic.AliasChoices,
        pydantic.AliasPath,
        pydantic.Discriminator,
        pydantic.StringConstraints,
        pydantic.Strict,
        pydantic.AllowInfNan,
        pydantic.FailFast,
        fastapi.params.Depends,
        fastapi.params.Security,
        annotated_types.Gt,
        annotated_types.Ge,
        annotated_types.Lt,
        annotated_types.Le,
        annotated_types.MultipleOf,
        annotated_types.MinLen,
        annotated_types.MaxLen,
        annotated_types.Len,
        annotated_types.Interval,
        annotated_types.Timezone,
        annotated_types.Predicate,
    )
}
_TYPE_VAR_TUPLE = typing.TypeVarTuple("_TYPE_VAR_TUPLE")
_PARAM_SPEC = typing.ParamSpec("_PARAM_SPEC")
# Type hints implemented in C, such as list[int], *tuple[int, ...], and int | str, with the attributes that hold all of
# their state
_C_TYPE_HINT_ATTRIBUTES: dict[int, tuple[str, ...]] = {
    id(types.GenericAlias): ("__origin__", "__args__", "__unpacked__"),
    id(type(collections.abc.Callable[[int], str])): ("__origin__", "__args__", "__unpacked__"),
    id(types.UnionType): ("__args__",),
}
# Type hints of the typing module, such as List[int], Literal[1], and Annotated[int, Gt(1)], whose instance __dict__
# holds all of their state
_PYTHON_TYPE_HINT_TYPE_IDS = frozenset(
    id(hint_type)
    for hint_type in {
        type(typing.List[int]),  # noqa: UP006
        type(typing.Union[int, str]),
        type(typing.Literal[1]),
        type(typing.Annotated[int, 1]),
        type(typing.Callable[[int], str]),
        type(typing.Concatenate[int, _PARAM_SPEC]),
        type(typing.Unpack[_TYPE_VAR_TUPLE]),
    }
    if id(hint_type) not in _C_TYPE_HINT_ATTRIBUTES
)


def is_same_value(left: Any, right: Any) -> bool:
    """Whether Cadwyn can be sure that the values are the same, so changing one to the other changes nothing.

    `==` is not enough: `True == 1`, `1.0 == 1`, `-0.0 == 0.0`, `Decimal("1.0") == Decimal("1")`, and
    `Union[int, str] == Union[str, int]`, while OpenAPI and serialization tell them apart. Different objects are the
    same only if both have exactly the same type and they are:

    * `None`, `bool`, `int`, `float`, `complex`, `str`, `bytes`, or `Decimal` values with the same value, sign, and
      exponent (NaN is never the same as another NaN object)
    * `list`, `tuple`, or `dict` values whose items (and keys) are the same in the same order, or `set` or `frozenset`
      values whose items are the same and are all such builtin values
    * instances of the pydantic, FastAPI, and annotated_types dataclasses in `_ALLOWED_TYPE_FIELDS` whose fields are
      the same
    * type hints of `typing` and `types` whose origins, args, and other attributes are the same

    Anything else, including instances of other dataclasses, classes, and values that refer to themselves, is the
    same only if it is the same object. We never run the user's code: we check types only by identity, and we read
    the attributes of the types above only. When we can't be sure, the values are different, so we never reject a
    real change. Accepting an instruction that changes nothing is harmless because it applies nothing.
    """
    try:
        return _is_same_value(left, right, set())
    except Exception:  # noqa: BLE001  # pragma: no cover
        # We run no user code, so only a value nested too deeply to compare (RecursionError) can get here
        return False


def _is_same_value(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    if left is right:
        return True
    value_type = type(left)
    if value_type is not type(right):
        return False
    if id(value_type) in _SCALAR_TYPE_IDS:
        return _are_same_scalars(left, right)
    pair = (id(left), id(right))
    if pair in pairs_being_compared:
        # The values refer to themselves, e.g. a dict that contains itself
        return False
    pairs_being_compared.add(pair)
    try:
        return _have_same_contents(left, right, pairs_being_compared)
    finally:
        pairs_being_compared.discard(pair)


def _are_same_scalars(left: Any, right: Any) -> bool:
    value_type = type(left)
    if value_type is float:
        # -0.0 == 0.0. NaN is never equal to another NaN
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if value_type is complex:
        return _are_same_scalars(left.real, right.real) and _are_same_scalars(left.imag, right.imag)
    if value_type is Decimal:
        # Decimal("1.0") == Decimal("1") and a signaling NaN raises in ==
        return not left.is_nan() and left.as_tuple() == right.as_tuple()
    return left == right


def _have_same_contents(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    value_type = type(left)
    if value_type is list or value_type is tuple:
        return _are_same_sequences(left, right, pairs_being_compared)
    if value_type is dict:
        # The order of the keys matters because it is the order of the keys in JSON
        return _are_same_sequences(list(left.items()), list(right.items()), pairs_being_compared)
    if value_type is set or value_type is frozenset:
        # Hashing and comparing builtin values never runs the user's code
        if not all(map(_is_builtin_hashable, left)) or not all(map(_is_builtin_hashable, right)):
            return False
        if len(left) != len(right) or left != right:
            return False
        # The sets are equal, so each item has an equal item (e.g. 1 for True) in the other set
        right_items = {item: item for item in right}
        return all(_is_same_value(item, right_items[item], pairs_being_compared) for item in left)
    type_id = id(value_type)
    if type_id in _ALLOWED_TYPE_FIELDS:
        return _are_same_sequences(
            [_read_attribute_statically(left, name) for name in _ALLOWED_TYPE_FIELDS[type_id]],
            [_read_attribute_statically(right, name) for name in _ALLOWED_TYPE_FIELDS[type_id]],
            pairs_being_compared,
        )
    if type_id in _C_TYPE_HINT_ATTRIBUTES:
        return _are_same_sequences(
            [getattr(left, name) for name in _C_TYPE_HINT_ATTRIBUTES[type_id]],
            [getattr(right, name) for name in _C_TYPE_HINT_ATTRIBUTES[type_id]],
            pairs_being_compared,
        )
    if type_id in _PYTHON_TYPE_HINT_TYPE_IDS:
        return _is_same_value(vars(left), vars(right), pairs_being_compared)
    return False


def _are_same_sequences(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    return len(left) == len(right) and all(
        _is_same_value(left_item, right_item, pairs_being_compared)
        for left_item, right_item in zip(left, right, strict=True)
    )


def _read_attribute_statically(value: Any, name: str) -> Any:
    attribute = inspect.getattr_static(value, name)
    # A slot of the instance
    return attribute.__get__(value) if type(attribute) is types.MemberDescriptorType else attribute


def _is_builtin_hashable(value: Any) -> bool:
    value_type = type(value)
    return id(value_type) in _SCALAR_TYPE_IDS or (
        (value_type is tuple or value_type is frozenset) and all(map(_is_builtin_hashable, value))
    )


def is_enum_looked_up_by_value(enum_cls: type[Enum]) -> bool:
    """Whether pydantic takes any member of the enum's type that has the value of a member, e.g. a member of a copy.

    Pydantic looks up the members of `str`, `int`, and `float` enums by their values. Plain enums and flags only take
    their own members, and pass anything else to `_missing_`.
    """
    return issubclass(enum_cls, (int, str, float))


def get_member_with_same_value(enum_cls: type[Enum], member: Enum) -> Union[Enum, None]:
    """Return the member of `enum_cls` that has exactly the value of `member`, or None if it has no such member.

    Unlike `enum_cls(member.value)`, this never calls the `_missing_` of `enum_cls`, which can map the value to another
    member, and it never takes a member whose value only equals the value of `member` (e.g. `1` for `True`). For flags,
    it takes the combinations of members that have the value, but not the ones that the boundary of `enum_cls` makes of
    a value with other bits (e.g. `CONFORM` drops the bits that `enum_cls` doesn't have).
    """
    value = member._value_
    try:
        try:
            candidate = enum_cls._value2member_map_.get(value)
        except TypeError:  # Enums keep the members with unhashable values only in their member map
            candidate = next((other for other in enum_cls._member_map_.values() if other._value_ == value), None)
        if candidate is None and issubclass(enum_cls, Flag) and type(value) is int:
            # Flags create the combinations of their members on lookup
            candidate = Flag._missing_.__func__(enum_cls, value)
        is_same_value = (
            type(candidate) is enum_cls and type(candidate._value_) is type(value) and candidate._value_ == value
        )
    except Exception:  # noqa: BLE001 # E.g. a value whose `__eq__` fails, or a flag boundary that rejects the bits
        return None
    return candidate if is_same_value else None
