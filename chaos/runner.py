"""Six ways a payment gateway loses a merchant money without anybody attacking anything.

No model runs here. No prompt is injected. Nothing is adversarial in the security sense.
Every scenario below is ordinary distributed-systems weather -- at-least-once delivery,
two workers, a deploy, a timeout -- and each one is reproducible to the paise.

That is the point. PayNaka is usually shown stopping an injected agent, and injection is
the loud half of the problem. The quiet half is that a money-moving system talks to a
gateway over a network, and a network delivers things twice, out of order, and slowly.
The gate that contains a hostile agent is the same gate that contains a duplicate
webhook, because both are answered by the same question: *has this exact request already
been authorised?*

Read the two columns carefully. ``left the gateway`` is ground truth -- what the rail
actually did. ``ledger says`` is what the handler believes. In scenario six they disagree
for PayNaka, and that disagreement is correct: the response was lost, so the outcome is
genuinely unknown, and the audit chain says ``rail.indeterminate`` rather than inventing
a number. A ledger that guesses is worse than one that admits it does not know.

The naive handler is not a strawman. See ``chaos/handlers.py``.

    python -m chaos.runner
    python -m chaos.runner --scenario duplicate_concurrent --verbose
    python -m chaos.runner --json chaos-results.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from chaos.delivery import (
    Delivery,
    Outcome,
    deliver_concurrently,
    deliver_in_order,
)
from chaos.handlers import GatedHandler, LossyRail, NaiveHandler
from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.mandate import MandateSigner, generate_keypair
from paynaka.money import format_inr
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState
from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, YELLOW, say

# Imported late so the module docstring stays the first thing a reader meets.
from paynaka.engine import PayNaka  # isort: skip
from paynaka.mandate import IntentMandate  # isort: skip

__all__ = ["SCENARIOS", "ScenarioResult", "main", "run_scenario"]

#: 15:00 IST: outside the NPCI 10:00-13:00 debit blackout and inside the RBI contact
#: window, so no regulatory check fires and the scenarios measure delivery semantics
#: alone. The time-window rules have their own tests.
CHAOS_CLOCK = "2026-08-23 15:00"

CAPTURE = 199_900  #: Rs 1,999 -- the order the shopper actually placed
ENTITLED = 49_900  #: Rs 499 -- one item came back, and that is all that is owed
TAMPERED = 149_900  #: Rs 1,499 -- what an altered redelivery asks for instead

PAYMENT_METHOD = "upi"

#: How many refund responses the lossy rail swallows. Three is a gateway having a bad
#: minute, which is a thing gateways have.
LOST_RESPONSES = 3

# ====================================================================== the stacks

Handler = NaiveHandler | GatedHandler


@dataclass
class Stack:
    """One handler wired to its own rail, plus the ground truth that rail holds."""

    handler: Handler
    rail: SimRail
    payment_id: str

    def refunded_on_the_rail(self) -> int:
        """What actually left the gateway. Not what anyone believes left it."""
        return int(self.rail.fetch_payment(self.payment_id).raw.get("refunded", 0))

    def ledger_says(self) -> int:
        if isinstance(self.handler, GatedHandler):
            return self.handler.naka.state.refunded_amount(self.payment_id)
        return self.handler.moved


def _authorised_payment(rail: SimRail) -> str:
    """Checkout, up to the point where the webhooks take over."""
    order = rail.create_order(
        amount=CAPTURE, currency="INR", receipt="rcpt_chaos", idempotency_key="setup:order"
    )
    payment = rail.pay_order(
        order_id=order.order_id, method=PAYMENT_METHOD, idempotency_key="setup:pay"
    )
    return payment.payment_id


def naive_stack(seed: str, *, lossy: bool = False) -> Stack:
    rail = SimRail(seed=seed)
    payment_id = _authorised_payment(rail)
    handler = NaiveHandler(rail=LossyRail(rail, lose_first=LOST_RESPONSES) if lossy else rail)
    return Stack(handler=handler, rail=rail, payment_id=payment_id)


def gated_stack(seed: str, *, lossy: bool = False) -> Stack:
    clock = FrozenClock.at_ist(CHAOS_CLOCK)
    signer = MandateSigner(generate_keypair()[0])
    rail = SimRail(seed=seed)
    payment_id = _authorised_payment(rail)

    mandate = IntentMandate.create(
        clock=clock,
        subject="cust_kirana_001",
        session_id="sess_chaos",
        max_total=CAPTURE,
        allowed_skus=("ATTA-5KG",),
        allowed_destinations=("addr_home",),
        max_qty_per_sku=3,
        # A refund is irreversible, so it is opted into here rather than inherited.
        allowed_actions=("capture_payment", "create_refund"),
    )
    naka = PayNaka(
        rail=LossyRail(rail, lose_first=LOST_RESPONSES) if lossy else rail,
        policy=Policy.from_yaml("policy.yaml"),
        state=SqliteState(":memory:", clock=clock),
        audit=AuditChain(":memory:", clock=clock),
        verifier=signer.verifier(),
        clock=clock,
    )
    handler = GatedHandler(naka=naka, signed=signer.sign(mandate), clock=clock)
    return Stack(handler=handler, rail=rail, payment_id=payment_id)


# ====================================================================== deliveries


def capture(payment_id: str) -> Delivery:
    return Delivery(
        event="payment.captured", event_id="evt_cap_01", payment_id=payment_id, amount=CAPTURE
    )


def returned(payment_id: str) -> Delivery:
    return Delivery(event="return.received", event_id="evt_ret_01", payment_id=payment_id, amount=0)


def refund(payment_id: str, *, attempt: int = 1, amount: int = ENTITLED) -> Delivery:
    return Delivery(
        event="refund.requested",
        event_id="evt_rfnd_01",
        payment_id=payment_id,
        amount=amount,
        attempt=attempt,
    )


def _settle(stack: Stack) -> list[Outcome]:
    """Capture and record the return. The uncontroversial part, delivered cleanly."""
    return deliver_in_order(
        stack.handler.handle, [capture(stack.payment_id), returned(stack.payment_id)]
    )


# ====================================================================== scenarios


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    title: str
    hazard: str
    why: str
    drive: Callable[[Stack], list[Outcome]]
    lossy: bool = False
    #: What the shopper is actually owed. Anything above this left without authority.
    entitled: int = ENTITLED


def _duplicate_sequential(stack: Stack) -> list[Outcome]:
    out = _settle(stack)
    plan = [refund(stack.payment_id, attempt=1), refund(stack.payment_id, attempt=2)]
    return out + deliver_in_order(stack.handler.handle, plan)


def _duplicate_concurrent(stack: Stack) -> list[Outcome]:
    out = _settle(stack)

    # Hold both workers inside the naive handler's read-then-write window. Releasing two
    # threads at the top of the call is not enough: the barrier wakes them microseconds
    # apart and the first one usually finishes the whole refund before the second reaches
    # its membership test, so the bug hides four runs in five. The window being forced
    # here belongs to the handler; only the timing is the harness's doing, and a race that
    # reproduces once a week is not a demonstration of anything.
    if isinstance(stack.handler, NaiveHandler):
        inside = threading.Barrier(2)

        def hold() -> None:
            # A broken barrier means the partner died; proceeding is still correct.
            with contextlib.suppress(threading.BrokenBarrierError):
                inside.wait(timeout=5)

        stack.handler.gap = hold

    plan = [refund(stack.payment_id, attempt=1), refund(stack.payment_id, attempt=2)]
    return out + deliver_concurrently(stack.handler.handle, plan)


def _duplicate_after_restart(stack: Stack) -> list[Outcome]:
    out = _settle(stack)
    out += deliver_in_order(stack.handler.handle, [refund(stack.payment_id, attempt=1)])
    stack.handler.restart()
    out += deliver_in_order(stack.handler.handle, [refund(stack.payment_id, attempt=2)])
    return out


def _out_of_order(stack: Stack) -> list[Outcome]:
    plan = [
        returned(stack.payment_id),
        refund(stack.payment_id, attempt=1),  # arrives before the capture it depends on
        capture(stack.payment_id),
        refund(stack.payment_id, attempt=2),  # the gateway retries what returned 5xx
    ]
    return deliver_in_order(stack.handler.handle, plan)


def _tampered_replay(stack: Stack) -> list[Outcome]:
    out = _settle(stack)
    out += deliver_in_order(stack.handler.handle, [refund(stack.payment_id, attempt=1)])
    stack.handler.restart()
    tampered = refund(stack.payment_id, attempt=2, amount=TAMPERED)
    out += deliver_in_order(stack.handler.handle, [tampered])
    return out


def _timeout_retry_storm(stack: Stack) -> list[Outcome]:
    out = _settle(stack)
    plan = [refund(stack.payment_id, attempt=n) for n in (1, 2, 3, 4)]
    return out + deliver_in_order(stack.handler.handle, plan)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="duplicate_sequential",
        title="Redelivery, one worker, in order",
        hazard="the gateway delivered refund.requested twice",
        why=(
            "At-least-once is the only delivery guarantee a distributed system can honestly "
            "make. A lost ACK looks exactly like a lost request, so it sends again."
        ),
        drive=_duplicate_sequential,
    ),
    Scenario(
        key="duplicate_concurrent",
        title="Redelivery, two workers, at the same instant",
        hazard="both copies were picked up simultaneously",
        why=(
            "The naive handler deduplicates on a set it reads and then writes. Two threads "
            "fit between the read and the write, and both of them find the set empty."
        ),
        drive=_duplicate_concurrent,
    ),
    Scenario(
        key="duplicate_after_restart",
        title="Redelivery across a deploy",
        hazard="the process restarted between the two copies",
        why=(
            "Gateway retry windows are measured in hours and deploys are not rare. "
            "Deduplication that lives in RAM is not deduplication; it is a cache and some luck."
        ),
        drive=_duplicate_after_restart,
    ),
    Scenario(
        key="out_of_order",
        title="The refund arrives before the capture",
        hazard="refund.requested was delivered ahead of payment.captured",
        why=(
            "Gateways fan out across workers and do not serialise. Two events emitted in "
            "order can arrive in either order, and nobody ever promised otherwise."
        ),
        drive=_out_of_order,
    ),
    Scenario(
        key="tampered_replay",
        title="A redelivery whose amount was altered in flight",
        hazard="the same event id came back asking for Rs 1,499 instead of Rs 499",
        why=(
            "A real Razorpay webhook is HMAC-signed and a mutated body fails that check "
            "first. This asks what is left when it does not: a mis-set secret, a proxy that "
            "re-serialises the body, a replay off an internal queue that was never signed. "
            "Deduplicating on the event id alone gives no answer, because the id is the one "
            "field an attacker has every reason to leave alone."
        ),
        drive=_tampered_replay,
    ),
    Scenario(
        key="timeout_retry_storm",
        title="The refund succeeded and the response was lost",
        hazard="the gateway timed out after doing the work, three times retried",
        why=(
            "A timeout is not a decline. The money may well have moved. Retrying a timeout "
            "with a fresh idempotency key is how one refund becomes four, and the key "
            "looks correct in the code because it does identify the call -- just not the "
            "business event, which is the only thing that matters."
        ),
        drive=_timeout_retry_storm,
        lossy=True,
    ),
)


# ====================================================================== running


@dataclass
class Side:
    """One handler's outcome for one scenario. Money is int paise."""

    handler: str
    left_the_gateway: int
    ledger_says: int
    entitled: int
    outcomes: list[Outcome] = field(default_factory=list)
    #: The ``kind`` of every audit record written during the run, in order. Empty for the
    #: naive handler, which is itself the finding: it has no audit chain to write to.
    audit_kinds: list[str] = field(default_factory=list)

    @property
    def overspent(self) -> int:
        return max(0, self.left_the_gateway - self.entitled)

    @property
    def underpaid(self) -> int:
        """The customer's money that never came back. A quieter failure, still a failure."""
        return max(0, self.entitled - self.left_the_gateway)

    @property
    def books_disagree(self) -> int:
        return self.left_the_gateway - self.ledger_says

    @property
    def refunds(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.delivery.event == "refund.requested"]

    @property
    def named_refusals(self) -> list[str]:
        """Refusals that carry a check id, and therefore an audit record naming them."""
        return [o.check_id for o in self.refunds if o.check_id and not o.acted]

    @property
    def silent_drops(self) -> int:
        """Refunds that failed leaving nothing behind but a log line nobody reads.

        The money did not move, so nothing shows up in a reconciliation of the ledger --
        which is exactly why this class of failure survives for months. A refund that was
        owed and never paid is a customer complaint, not a metric.
        """
        return sum(1 for o in self.refunds if o.error and not o.check_id and not o.acted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handler": self.handler,
            "left_the_gateway": self.left_the_gateway,
            "ledger_says": self.ledger_says,
            "entitled": self.entitled,
            "overspent": self.overspent,
            "underpaid": self.underpaid,
            "books_disagree": self.books_disagree,
            "named_refusals": self.named_refusals,
            "silent_drops": self.silent_drops,
            "audit_kinds": self.audit_kinds,
        }


@dataclass
class ScenarioResult:
    scenario: Scenario
    naive: Side
    naka: Side

    @property
    def prevented(self) -> int:
        return max(0, self.naive.overspent - self.naka.overspent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.scenario.key,
            "title": self.scenario.title,
            "hazard": self.scenario.hazard,
            "why": self.scenario.why,
            "naive": self.naive.to_dict(),
            "paynaka": self.naka.to_dict(),
            "prevented": self.prevented,
        }


def _side(scenario: Scenario, stack: Stack, label: str) -> Side:
    outcomes = scenario.drive(stack)
    audit_kinds: list[str] = []
    if isinstance(stack.handler, GatedHandler):
        audit_kinds = [
            str(record.payload.get("kind", ""))
            for record in stack.handler.naka.audit.records(limit=500)
        ]
    return Side(
        handler=label,
        left_the_gateway=stack.refunded_on_the_rail(),
        ledger_says=stack.ledger_says(),
        entitled=scenario.entitled,
        outcomes=outcomes,
        audit_kinds=audit_kinds,
    )


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Run one scenario through both handlers, on identical fresh stacks."""
    seed = f"chaos:{scenario.key}"
    naive = _side(scenario, naive_stack(seed, lossy=scenario.lossy), "naive")
    naka = _side(scenario, gated_stack(seed, lossy=scenario.lossy), "paynaka")
    return ScenarioResult(scenario=scenario, naive=naive, naka=naka)


# ====================================================================== reporting


def _row(side: Side) -> str:
    colour = RED if side.overspent else (YELLOW if side.underpaid else GREEN)
    line = (
        f"    {side.handler:9s} left the gateway {colour}{format_inr(side.left_the_gateway):>12s}"
        f"{OFF}   owed {format_inr(side.entitled)}"
    )
    if side.overspent:
        line += f"   {RED}overspent {format_inr(side.overspent)}{OFF}"
    elif side.underpaid:
        line += f"   {YELLOW}customer short {format_inr(side.underpaid)}{OFF}"
    else:
        line += f"   {GREEN}exact{OFF}"
    return line


def _explain(side: Side) -> list[str]:
    """One line per delivery that did something interesting."""
    lines: list[str] = []
    for outcome in side.outcomes:
        d = outcome.delivery
        if d.event != "refund.requested":
            continue
        verdict = outcome.check_id or outcome.detail.get("verdict", "")
        what = outcome.reason or outcome.error or ""
        mark = f"{RED}moved{OFF}" if outcome.moved else f"{GREEN}held {OFF}"
        lines.append(
            f"      attempt {d.attempt}  {mark} {format_inr(outcome.moved):>10s}  "
            f"{DIM}{verdict or '-'}: {what[:64]}{OFF}"
        )
    return lines


def report(results: Sequence[ScenarioResult], *, verbose: bool = False) -> None:
    say()
    say(f"{BOLD}Chaos{OFF}  {DIM}duplicate, reordered and lost webhooks. No model runs here.{OFF}")
    say(
        f"{DIM}One order of {format_inr(CAPTURE)}. One item comes back, worth "
        f"{format_inr(ENTITLED)}.{OFF}"
    )
    say()

    for result in results:
        say(f"  {BOLD}{result.scenario.title}{OFF}")
        say(f"    {DIM}{result.scenario.hazard}{OFF}")
        say(_row(result.naive))
        if verbose:
            for line in _explain(result.naive):
                say(line)
        say(_row(result.naka))
        if verbose:
            for line in _explain(result.naka):
                say(line)

        # When both sides move the same money the interesting difference is what survives
        # to be reconciled afterwards. A tie in rupees is not a tie in accountability.
        if result.naive.left_the_gateway == result.naka.left_the_gateway:
            if result.naive.silent_drops and result.naka.named_refusals:
                say(
                    f"    {DIM}same money, different books: naive dropped "
                    f"{result.naive.silent_drops} refund(s) leaving no record; paynaka "
                    f"refused with {', '.join(sorted(set(result.naka.named_refusals)))} "
                    f"and put it on the chain.{OFF}"
                )
            else:
                say(f"    {DIM}both handlers came out exact. This one is not the problem.{OFF}")

        gap = result.naka.books_disagree
        if gap:
            say(
                f"    {DIM}paynaka's ledger is {format_inr(abs(gap))} behind the rail on "
                f"purpose: the response was lost, so the outcome is unknown and the audit "
                f"chain says so rather than guessing.{OFF}"
            )
        say(f"    {DIM}why it happens: {result.scenario.why}{OFF}")
        say()

    naive_total = sum(r.naive.overspent for r in results)
    naka_total = sum(r.naka.overspent for r in results)
    short_total = sum(r.naive.underpaid for r in results)

    say(f"  {BOLD}Totals across {len(results)} scenario(s){OFF}")
    say(f"    naive     overspent {RED}{format_inr(naive_total)}{OFF}")
    say(f"    paynaka   overspent {GREEN}{format_inr(naka_total)}{OFF}")
    if short_total:
        say(f"    naive     left the customer short {YELLOW}{format_inr(short_total)}{OFF}")
    say()
    say(f"{DIM}Every rupee above was moved by ordinary delivery semantics. Nothing here was{OFF}")
    say(f"{DIM}injected, prompted or adversarial. The gate that contains a hostile agent is{OFF}")
    say(f"{DIM}the same gate that contains a duplicate webhook, and for the same reason.{OFF}")
    say()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chaos.runner")
    parser.add_argument("--scenario", help="run one by key; default is all of them")
    parser.add_argument("--list", action="store_true", help="print the scenario keys and exit")
    parser.add_argument("--verbose", action="store_true", help="show every refund delivery")
    parser.add_argument("--json", dest="json_path", help="also write machine-readable results")
    args = parser.parse_args(argv)

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.key:24s} {scenario.title}")
        return 0

    chosen = SCENARIOS
    if args.scenario:
        chosen = tuple(s for s in SCENARIOS if s.key == args.scenario)
        if not chosen:
            print(f"no scenario named {args.scenario!r}. --list shows them all", file=sys.stderr)
            return 2

    results = [run_scenario(scenario) for scenario in chosen]
    report(results, verbose=args.verbose)

    if args.json_path:
        payload = {
            "capture": CAPTURE,
            "entitled": ENTITLED,
            "clock_ist": CHAOS_CLOCK,
            "scenarios": [r.to_dict() for r in results],
            "totals": {
                "naive_overspent": sum(r.naive.overspent for r in results),
                "paynaka_overspent": sum(r.naka.overspent for r in results),
            },
        }
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        say(f"{DIM}wrote {args.json_path}{OFF}")

    # A non-zero exit if PayNaka let anything through. This target belongs in CI.
    return 1 if any(r.naka.overspent for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
