"""Lets block002.py import block001.py by the file name that the page gives it."""

from .block001 import RemoveTaxIdEndpoints

__all__ = ["RemoveTaxIdEndpoints"]
