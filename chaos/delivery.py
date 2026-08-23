"""What a payment gateway's webhook delivery actually looks like from the receiving end.

Every gateway worth using delivers **at least once**. Not exactly once -- at least once.
Razorpay retries a webhook that did not return 2xx, and it will retry one that *did*
return 2xx if the ACK was lost on the way back. That is not a gateway bug; it is the only
honest thing a distributed system can promise, and it means the receiving end is where
correctness has to live.

Four hazards, each of which has cost somebody real money:

**Redelivery.** The same event arrives twice. Handling this needs deduplication that
survives the thing doing the deduplicating.

**Concurrency.** Two workers pull the same redelivery at the same moment. An in-memory
``seen`` set has a window between the read and the write, and two threads fit through it.

**Restart.** The redelivery lands after a deploy. Anything the old process remembered is
gone. Deduplication that lives in RAM is not deduplication; it is a cache with good luck.

**Mutation in flight.** A redelivery whose body was altered. The signature check is the
first line here, but a system that only compares event *ids* will silently accept it,
because the id is exactly the field an attacker leaves alone.

Nothing in this module is random. A chaos harness whose failures cannot be reproduced is
a slot machine, not a test.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "Delivery",
    "Outcome",
    "deliver_concurrently",
    "deliver_in_order",
    "duplicate_every",
    "reorder_pairs",
    "tamper_amount",
]


@dataclass(frozen=True, slots=True)
class Delivery:
    """One POST from the gateway to our webhook endpoint.

    ``event_id`` is the gateway's identifier for the *business event*, stable across every
    redelivery of it. ``attempt`` counts our receipt of it, so a handler can tell a first
    delivery from a fourth -- though it must never *rely* on that, since attempt 1 can
    arrive after attempt 2.
    """

    event: str
    event_id: str
    payment_id: str
    amount: int
    attempt: int = 1
    note: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise TypeError(f"amount must be int paise, got {type(self.amount).__name__}")
        if self.amount < 0:
            raise ValueError("amount must not be negative")
        for name in ("event", "event_id", "payment_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")

    @property
    def redelivery(self) -> bool:
        return self.attempt > 1


@dataclass(slots=True)
class Outcome:
    """What one handler did with one delivery. Money is int paise."""

    delivery: Delivery
    acted: bool
    moved: int = 0
    reason: str = ""
    check_id: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ====================================================================== hazards
# Each takes a delivery plan and returns a new one. Composable, and pure -- the same
# input gives the same output, so a failing scenario reproduces exactly.


def duplicate_every(plan: Sequence[Delivery], n: int) -> list[Delivery]:
    """Redeliver every ``n``-th event once more, marked as a later attempt.

    ``n=1`` redelivers everything, which is the pessimistic case and the one worth
    testing: a gateway having a bad afternoon really does look like this.
    """
    if n <= 0:
        return list(plan)
    out: list[Delivery] = []
    for index, delivery in enumerate(plan, start=1):
        out.append(delivery)
        if index % n == 0:
            out.append(replace(delivery, attempt=delivery.attempt + 1))
    return out


def reorder_pairs(plan: Sequence[Delivery]) -> list[Delivery]:
    """Swap each adjacent pair.

    Gateways deliver over many workers and do not serialise. A capture and the refund
    that follows it can arrive in either order, and a handler that assumes otherwise is
    relying on a guarantee nobody made.
    """
    out = list(plan)
    for i in range(0, len(out) - 1, 2):
        out[i], out[i + 1] = out[i + 1], out[i]
    return out


def tamper_amount(plan: Sequence[Delivery], *, event_id: str, to: int) -> list[Delivery]:
    """Rewrite the amount on the *last* delivery carrying ``event_id``.

    The id is left alone on purpose. Anyone altering a webhook in flight alters the field
    that pays them, not the field that identifies the message -- so a handler that
    deduplicates on the id alone never looks at what it just accepted.
    """
    out = list(plan)
    for i in range(len(out) - 1, -1, -1):
        if out[i].event_id == event_id:
            out[i] = replace(out[i], amount=to, note="amount altered in flight")
            return out
    raise KeyError(f"no delivery carries event_id {event_id!r}")


# ====================================================================== delivery


def deliver_in_order(
    handler: Callable[[Delivery], Outcome], plan: Iterable[Delivery]
) -> list[Outcome]:
    """One worker, one at a time. The easy case, and the one everybody tests."""
    return [handler(delivery) for delivery in plan]


def deliver_concurrently(
    handler: Callable[[Delivery], Outcome], plan: Sequence[Delivery], *, timeout: float = 5.0
) -> list[Outcome]:
    """Every delivery at once, released together.

    A thread pool alone does not reproduce the race: it hands out work fast enough that
    the first task usually finishes before the second starts, and the bug hides. The
    barrier below holds every worker until all of them have arrived, so they enter the
    handler's read-then-write window together -- which is the only way a check-then-act
    bug shows up reliably rather than one run in fifty.

    One thread per delivery, deliberately. Chaos plans are a handful of events; a pool
    smaller than the plan would let the barrier trip in waves and the later waves would
    each wait out the full timeout for partners that never come.
    """
    if not plan:
        return []

    barrier = threading.Barrier(len(plan))
    results: list[Outcome | None] = [None] * len(plan)

    def run(index: int) -> None:
        # A broken barrier means a worker died; the rest still deliver.
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=timeout)
        results[index] = handler(plan[index])

    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        list(pool.map(run, range(len(plan))))

    return [r for r in results if r is not None]
