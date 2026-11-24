"""Dependencies with lazily evaluated annotations (Python 3.14+) that use names which only exist for type checkers.

Importing this module on older Python versions fails because they evaluate annotations right away.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal  # noqa: TC004


def get_limit(limit: int) -> Decimal:
    return limit  # ty: ignore[invalid-explicit-return]


def get_limit_with_price(limit: int, price: Decimal | None = None) -> int:
    return limit
