"""What the checkpoint costs. The question a payments team asks before any other.

A defence nobody will deploy is not a defence, and "we do not know what it adds to the
money path" is a reason not to deploy. So this measures it, on the same commit as every
other number in this repository, with no keys and no network.

Three layers are timed separately, because they have different answers and conflating them
would hide the interesting one:

``envelope`` the checks that are pure functions of ``(request, mandate)`` -- items,
             quantities, total, reference price, destination, currency, structure. No I/O
             at all. This is the cost of the design's central claim.
``gate``     the whole of ``evaluate()``. Same checks plus the ones that must consult
             stored state: revocation, the daily refund cap, the refundable balance. This
             number is dominated by SQLite reads rather than by check logic, which is the
             most useful thing this file has to say -- the checks are nearly free and the
             state store is where the time goes. An adopter swapping SQLite for a shared
             database is changing the number that matters.
``engine``   the full enforced path: verify the signature, decide, write the audit record,
             claim the nonce, call the rail, write the ledger. Everything a real deployment
             adds *except* the network hop to Razorpay.
``rail``     the simulator, so the engine number can be read net of it.

**Method, stated so the number can be argued with.** Warm-up iterations are discarded, and
percentiles are reported rather than a mean -- a mean over a latency distribution is a
number that hides its own tail, and the tail is the part that pages somebody. The audit
chain and the state store run against real files under a temporary directory rather than
``:memory:``, because an in-memory SQLite figure is a benchmark of the wrong thing.

**What this is not.** One machine, one process, one thread, and the clock is frozen. It
does not measure contention, and a claim about behaviour under concurrent load is not one
this file can support. The honest reading is a floor: the checkpoint's own work, measured
carefully, on hardware whose name is printed beside the result.
"""

from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import (
    LineItem,
    MoneyRequest,
    check_currency,
    check_destination,
    check_items_subset,
    check_quantities,
    check_reference_price,
    check_structure,
    check_total,
    evaluate,
)
from paynaka.mandate import IntentMandate, MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState
from paynaka.tty import BOLD, DIM, GREEN, OFF, YELLOW, say

__all__ = ["Timing", "main", "measure"]

#: A typical Razorpay API call over the public internet, for reading the engine figure
#: against something. Deliberately the *optimistic* end of the range quoted for a hosted
#: payments API: a checkpoint that looks cheap only next to a slow network is not a result.
RAIL_CALL_MS: float = 120.0

AUTHORISED = 199_900
ATTA = "ATTA-5KG"
HOME = "addr_home"


@dataclass(frozen=True, slots=True)
class Timing:
    """Percentiles for one layer, in microseconds."""

    label: str
    samples: int
    p50: float
    p95: float
    p99: float
    worst: float

    @property
    def p99_ms(self) -> float:
        return self.p99 / 1000.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"p99_ms": self.p99_ms}


def _percentiles(label: str, samples_ns: list[int]) -> Timing:
    """Nearest-rank percentiles over the raw nanosecond samples.

    Nearest-rank rather than interpolated: an interpolated p99 reports a duration that no
    single call actually took, which is the wrong shape for a latency claim.
    """
    ordered = sorted(samples_ns)
    n = len(ordered)

    def at(fraction: float) -> float:
        index = min(n - 1, max(0, round(fraction * n) - 1))
        return ordered[index] / 1000.0

    return Timing(
        label=label,
        samples=n,
        p50=at(0.50),
        p95=at(0.95),
        p99=at(0.99),
        worst=ordered[-1] / 1000.0,
    )


def _order(key: str) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=ATTA, qty=1, unit_paise=AUTHORISED),),
        currency="INR",
        destination=HOME,
    )


def measure(*, iterations: int = 2_000, warmup: int = 200) -> dict[str, Timing]:
    """Time each layer and return its percentiles.

    Every iteration uses a fresh idempotency key. Reusing one would make the second call
    onward a replay, which short-circuits before the rail and would time a cache rather
    than the path.
    """
    clock = FrozenClock.at_ist("2026-08-23 15:00")
    policy = Policy.from_yaml("policy.yaml")
    signer = MandateSigner(generate_keypair()[0])
    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_latency",
        max_total=AUTHORISED,
        allowed_skus=(ATTA,),
        allowed_destinations=(HOME,),
        max_qty_per_sku=3,
        allowed_actions=("create_order", "capture_payment"),
    )
    signed = signer.sign(mandate)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            SqliteState(str(root / "state.db"), clock=clock) as state,
            AuditChain(str(root / "audit.db"), clock=clock) as audit,
        ):
            rail = SimRail(seed="latency")
            naka = PayNaka(
                rail=rail,
                policy=policy,
                state=state,
                audit=audit,
                verifier=signer.verifier(),
                clock=clock,
            )

            # ------------------------------------------------------ envelope only
            # The checks that need nothing but the request and the mandate. Called
            # directly rather than through evaluate(), because the point is to separate
            # the cost of the logic from the cost of reaching the state store.
            pure = (
                check_structure,
                check_currency,
                check_items_subset,
                check_quantities,
                check_reference_price,
                check_destination,
            )
            for i in range(warmup):
                request = _order(f"pw{i}")
                for check in pure:
                    check(request, mandate)
            envelope_ns: list[int] = []
            for i in range(iterations):
                request = _order(f"p{i}")
                start = time.perf_counter_ns()
                for check in pure:
                    check(request, mandate)
                check_total(request, mandate, policy)
                envelope_ns.append(time.perf_counter_ns() - start)

            # ---------------------------------------------------------- gate
            for i in range(warmup):
                evaluate(_order(f"w{i}"), mandate, state=state, policy=policy, clock=clock)
            gate_ns: list[int] = []
            for i in range(iterations):
                request = _order(f"g{i}")
                start = time.perf_counter_ns()
                evaluate(request, mandate, state=state, policy=policy, clock=clock)
                gate_ns.append(time.perf_counter_ns() - start)

            # ---------------------------------------------------------- rail alone
            for i in range(warmup):
                rail.create_order(
                    amount=AUTHORISED,
                    currency="INR",
                    receipt=f"rcpt_w{i}",
                    idempotency_key=f"rw{i}",
                )
            rail_ns: list[int] = []
            for i in range(iterations):
                start = time.perf_counter_ns()
                rail.create_order(
                    amount=AUTHORISED,
                    currency="INR",
                    receipt=f"rcpt_{i}",
                    idempotency_key=f"r{i}",
                )
                rail_ns.append(time.perf_counter_ns() - start)

            # ---------------------------------------------------------- full path
            for i in range(warmup):
                naka.execute(_order(f"ew{i}"), signed)
            engine_ns: list[int] = []
            for i in range(iterations):
                request = _order(f"e{i}")
                start = time.perf_counter_ns()
                naka.execute(request, signed)
                engine_ns.append(time.perf_counter_ns() - start)

    return {
        "envelope": _percentiles("envelope checks (no I/O)", envelope_ns),
        "gate": _percentiles("full gate (reads state)", gate_ns),
        "rail": _percentiles("simulated rail call", rail_ns),
        "engine": _percentiles("full enforced path", engine_ns),
    }


def render(timings: dict[str, Timing]) -> None:
    say()
    say(f"{BOLD}What the checkpoint costs{OFF}")
    say(
        f"{DIM}{platform.python_version()} on {platform.machine()}, "
        f"{timings['gate'].samples} iterations per layer, warm-up discarded{OFF}"
    )
    say()
    say(f"{'layer':<28}{'p50':>10}{'p95':>10}{'p99':>10}{'worst':>10}   {DIM}microseconds{OFF}")
    for key in ("envelope", "gate", "rail", "engine"):
        t = timings[key]
        say(f"{t.label:<28}{t.p50:>10.1f}{t.p95:>10.1f}{t.p99:>10.1f}{t.worst:>10.1f}")

    engine_p99 = timings["engine"].p99_ms
    share = engine_p99 / (engine_p99 + RAIL_CALL_MS) * 100
    say()
    say(
        f"The mandate checks -- the design's whole claim -- cost "
        f"{GREEN}{timings['envelope'].p99_ms:.3f} ms{OFF} at p99, with no I/O at all."
    )
    say(
        f"The full gate is {GREEN}{timings['gate'].p99_ms:.3f} ms{OFF}, so almost all of a "
        f"decision is the state store, not the checking."
    )
    say(
        f"The whole enforced path, audit write and ledger included, is "
        f"{GREEN}{engine_p99:.2f} ms{OFF} at p99."
    )
    say(
        f"Against a {RAIL_CALL_MS:.0f} ms call to a hosted payments API, that is "
        f"{BOLD}{share:.1f}%{OFF} of the round trip."
    )
    say()
    say(f"{YELLOW}Read it as a floor.{OFF} {DIM}One machine, one thread, a frozen clock and")
    say(f"{DIM}a local rail. This measures the checkpoint's own work carefully; it says")
    say(f"{DIM}nothing about behaviour under concurrent load.{OFF}")
    say()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--json", type=Path, help="also write the raw percentiles here")
    args = parser.parse_args(argv)

    if args.iterations < 1 or args.warmup < 0:
        parser.error("iterations must be positive and warmup non-negative")

    timings = measure(iterations=args.iterations, warmup=args.warmup)
    render(timings)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                    "rail_call_ms_assumed": RAIL_CALL_MS,
                    "layers": {key: t.to_dict() for key, t in timings.items()},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        say(f"{DIM}wrote {args.json}{OFF}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
