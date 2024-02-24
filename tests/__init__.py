# Note that this bug also affects 3.10+ but only on windows
# https://github.com/pyreadline/pyreadline/issues/65
import collections
from collections.abc import Callable

collections.Callable = Callable  # pyright: ignore[reportAttributeAccessIssue]
