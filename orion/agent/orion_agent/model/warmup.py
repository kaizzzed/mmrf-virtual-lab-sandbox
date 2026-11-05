"""Warm-up per model role (C-41; A4: "Effort is part of the cached prompt, so each role warms at
its own effort. A `max_tokens: 0` warm-up rejects structured output, so each warm-up is a small
real call").

The `warmup` action calls `warm_up_roles(planner, composer)` when the Assistant opens. Each Bedrock
role sends its own cached prefix at its own effort with the same output schema it plans or
composes with; the reply is discarded and no ModelCallRecord is made (the turn creates no run).
A stub role, or any object without `warm_up`, is skipped. A warm-up never raises: a failure is
reported in its result and the turn still ends Warmed.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar, runtime_checkable

from ..models import ModelRole
from .config import Effort

T = TypeVar("T")
log = logging.getLogger("orion.model")
WarmUpOutcome = Literal["ok", "refused", "error", "skipped"]


@dataclass(frozen=True)
class WarmUpResult:
    role: ModelRole
    model: str | None
    effort: Effort | None
    outcome: WarmUpOutcome
    elapsed_ms: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    detail: str | None = None


@runtime_checkable
class Warmable(Protocol):
    async def warm_up(self) -> WarmUpResult: ...


async def warm_up_result(role: ModelRole, model: str, effort: Effort, started: float, call: Awaitable[object]) -> WarmUpResult:
    """Run one warm-up call and report it; imported lazily so this module needs no SDK."""
    from .client import CallResult
    from .errors import ModelRoleError

    try:
        result = await call
    except ModelRoleError as error:
        log.warning("warm-up of the %s failed: %s", role, error.reason)
        return WarmUpResult(role, model, effort, "error", int((time.monotonic() - started) * 1000), detail=error.reason)
    elapsed = int((time.monotonic() - started) * 1000)
    if not isinstance(result, CallResult):
        return WarmUpResult(role, model, effort, "error", elapsed, detail="unexpected warm-up result")
    served = next((hop for hop in reversed(result.hops) if hop.status_code == 200), None)
    return WarmUpResult(
        role,
        model,
        effort,
        "refused" if result.refused else "ok",
        elapsed,
        cache_read_tokens=served.cache_read_tokens if served else None,
        cache_write_tokens=served.cache_write_tokens if served else None,
    )


async def warm_up_roles(*roles: object) -> list[WarmUpResult]:
    """Warm every role that can be warmed, concurrently; stubs and unknown objects are skipped."""
    names: tuple[ModelRole, ...] = ("planner", "composer")

    async def skipped(name: ModelRole, role: object) -> WarmUpResult:
        return WarmUpResult(name, None, None, "skipped", 0, detail=f"{type(role).__name__} needs no warm-up")

    calls: list[Awaitable[WarmUpResult]] = [
        role.warm_up() if isinstance(role, Warmable) else skipped(names[min(i, len(names) - 1)], role)
        for i, role in enumerate(roles)
    ]
    return list(await asyncio.gather(*calls))
