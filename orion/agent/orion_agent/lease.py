"""The run lease (RFA rev 25, A3 "Lease" and A4 timeout budget).

While a run is Planning, Executing or Answering, its controller renews a lease in the run
record on its own timer; entering any other status clears it. Only a dead controller lets a
lease lapse, and the next turn then ends the run Interrupted.
"""

import asyncio
import os
import socket
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .events import iso_utc


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


async def keep_renewed(writer: Renewable, every_s: float) -> None:
    """Renew on a fixed cadence that never waits for a step or a model call."""
    while True:
        await asyncio.sleep(every_s)
        await writer.renew_lease()
