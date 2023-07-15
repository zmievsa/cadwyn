# https://github.com/pyreadline/pyreadline/issues/65
import collections
from collections.abc import Callable

collections.Callable = Callable  # pyright: ignore[reportGeneralTypeIssues]
