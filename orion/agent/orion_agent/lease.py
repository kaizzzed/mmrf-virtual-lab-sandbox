"""The run lease (RFA rev 25, A3 "Lease" and A4 timeout budget).

While a run is Planning, Executing or Answering, its controller renews a lease in the run
record on its own timer; entering any other status clears it. Only a dead controller lets a
lease lapse, and the next turn then ends the run Interrupted.
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .events import iso_utc

log = logging.getLogger("orion.lease")


class Lease(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    owner: str
    renewed_at: str
    expires_at: str


class Renewable(Protocol):
    async def renew_lease(self) -> bool: ...


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def new_lease(owner: str, now: datetime, ttl_s: float) -> Lease:
    return Lease(owner=owner, renewed_at=iso_utc(now), expires_at=iso_utc(now + timedelta(seconds=ttl_s)))


def is_expired(lease: Lease | None, now: datetime) -> bool:
    """A missing lease counts as expired: an active status without one has no live controller."""
    if lease is None:
        return True
    return datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00")) <= now


RETRY_AFTER_FAILURE_S: Final = 5.0


async def keep_renewed(writer: Renewable, every_s: float) -> None:
    """Renew on a fixed cadence that never waits for a step or a model call.

    A failed renewal (a store write error) is logged and tried again sooner, never the end of the loop:
    one lost PUT must not let a live run's lease lapse and show it Interrupted.
    """
    delay = every_s
    while True:
        await asyncio.sleep(delay)
        try:
            await writer.renew_lease()
        except Exception as error:
            log.warning("lease renewal failed; retrying: %r", error)
            delay = min(every_s, RETRY_AFTER_FAILURE_S)
        else:
            delay = every_s
