"""Runtime tool-call allowlist enforcer.

Every graph node declares an explicit allowed_tools set.
This wrapper verifies the tool name is in the allowlist BEFORE
executing it — preventing prompt-injection and scope-creep attacks.

Security guarantee: a node literally cannot call a function outside
its declared set, even if the LLM tries to output one.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class AllowlistViolation(Exception):
    """Raised when a node attempts to call a tool outside its allowlist."""


def make_guarded_executor(
    node_name: str, allowed_tools: frozenset[str]
) -> Callable[..., Any]:
    """Return an executor function that enforces the allowlist.

    Usage:
        execute = make_guarded_executor("split_pay", ROUTE_ALLOWED_TOOLS["split_pay"])
        result = execute("create_payment_link", create_payment_link, amount=5000)
    """

    def execute(tool_name: str, tool_fn: Callable[..., Any], **kwargs: Any) -> Any:
        if tool_name not in allowed_tools:
            log.error(
                "allowlist.violation",
                node=node_name,
                tool_attempted=tool_name,
                allowed=sorted(allowed_tools),
            )
            raise AllowlistViolation(
                f"Node '{node_name}' attempted to call '{tool_name}' "
                f"which is not in its allowlist: {sorted(allowed_tools)}"
            )

        log.debug("allowlist.ok", node=node_name, tool=tool_name)
        return tool_fn(**kwargs)

    return execute


def enforce_amount_ceiling(
    original_amount_inr: Decimal,
    proposed_amount_inr: Decimal,
) -> None:
    """Assert that a recovery action never exceeds the original amount owed.

    Raises:
        ValueError if proposed > original (no discounts/waivers allowed).
    """
    if proposed_amount_inr > original_amount_inr:
        raise ValueError(
            f"Amount ceiling violated: proposed {proposed_amount_inr} INR "
            f"> original {original_amount_inr} INR. "
            f"Recovery actions may never exceed the original amount owed."
        )


def enforce_standalone_ceiling(proposed_amount_inr: Decimal) -> None:
    """Assert that a standalone link (no transaction context) never exceeds a safe max amount."""
    max_standalone_inr = Decimal("10000.00")  # 10k INR max
    if proposed_amount_inr > max_standalone_inr:
        raise ValueError(
            f"Standalone amount ceiling violated: proposed {proposed_amount_inr} INR "
            f"> max allowed {max_standalone_inr} INR for standalone links."
        )
