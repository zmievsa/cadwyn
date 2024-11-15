import importlib
from typing import Any

from cadwyn.exceptions import ImportFromStringError


def import_attribute_from_string(import_str: str) -> Any:
    module_str, _, attrs_str = import_str.partition(":")
    if not module_str or not attrs_str:
        message = f'Import string "{import_str}" must be in format "<module>:<attribute>".'
        raise ImportFromStringError(message.format(import_str=import_str))

    module = import_module_from_string(module_str)

    instance = module
    try:
        for attr_str in attrs_str.split("."):
            instance = getattr(instance, attr_str)
    except AttributeError as e:
        raise ImportFromStringError(f'Attribute "{attrs_str}" not found in module "{module_str}".') from e

    return instance


def import_module_from_string(module_str: str):
    try:
        return importlib.import_module(module_str)
    except ModuleNotFoundError as e:
        if e.name != module_str:  # pragma: no cover
            raise e from None
        raise ImportFromStringError(f'Could not import module "{module_str}".') from e
