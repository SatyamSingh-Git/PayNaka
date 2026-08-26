"""One genuine Razorpay test-mode lifecycle, driven through the checkpoint.

Every number this project publishes comes from `rails/sim.py`. That is defensible for
measuring a gate's semantics and it is not an answer to the question a payments reviewer
asks first, which is *show me the payment in the dashboard*. This script produces that.

**What is autonomous and what is not.** The agent path reaches `create_order` and stops
there, and that is the design rather than a limitation this hides:

    order      created here, through the gate, against api.razorpay.com     AUTONOMOUS
    payment    requires the customer to authenticate at Checkout            A HUMAN
    capture    API call, gated, needs the payment id from the step above    AUTONOMOUS
    refund     API call, gated, balance-claimed against the ledger          AUTONOMOUS

The middle row is the point. A buying agent that could authenticate as the customer would
be an agent holding the customer's payment credentials, which is the thing this project
exists to prevent. So the script runs in two phases with a human in between, and the human
step is a feature of the argument.

    python -m scripts.razorpay_test_lifecycle              # phase 1: order + checkout page
    python -m scripts.razorpay_test_lifecycle --payment-id pay_xxx   # phase 2: capture, refund

**Test mode only.** `RazorpayRail` refuses to construct against anything that is not an
`rzp_test_` key, with no override and no environment escape, and that refusal is a test.

Everything written to `var/evidence/` is committed: the raw Razorpay responses, the gate
decisions, and the audit chain. Numbers in prose are assertions; numbers with a response
body beside them are evidence.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from paynaka.audit import AuditChain
from paynaka.clock import SystemClock
from paynaka.engine import PayNaka
from paynaka.env import load_env
from paynaka.gate import LineItem, MoneyRequest
from paynaka.issuer import Issuer, ShopperIntent
from paynaka.mandate import MandateSigner, SignedMandate, load_or_create_signing_key
from paynaka.policy import Policy
from paynaka.rails.razorpay_rail import RazorpayRail
from paynaka.redact import redact
from paynaka.state import SqliteState
from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, YELLOW, say

EVIDENCE = pathlib.Path("var/evidence")
AUDIT_DB = EVIDENCE / "razorpay-lifecycle.db"
STATE_DB = EVIDENCE / "razorpay-lifecycle-state.db"
KEY_PATH = EVIDENCE / "lifecycle-signing.key"

ATTA = "ATTA-5KG"
HOME = "addr_home"
#: Rs 1,999 -- the same basket the whole project uses, so this run is comparable to every
#: simulated one beside it.
PRICE = 199_900


#: The signed mandate, kept between phases. Without this the file below described a
#: continuity it did not have.
MANDATE_PATH = pathlib.Path("var/lifecycle-mandate.json")


def _remembered_mandate(verifier: object) -> SignedMandate | None:
    """The mandate an earlier phase issued, if it is still on disk and still valid.

    Re-verified rather than trusted: a file is not a credential, and one that has been
    edited, truncated or expired must produce a fresh mandate rather than a confusing
    failure three calls later.
    """
    if not MANDATE_PATH.is_file():
        return None
    try:
        signed = SignedMandate.from_dict(json.loads(MANDATE_PATH.read_text(encoding="utf-8")))
        verifier.verify(signed)  # type: ignore[attr-defined]
    except Exception:  # any failure means "issue a new one", and it says so below
        say(f"{DIM}  the remembered mandate no longer verifies; issuing a fresh one{OFF}")
        return None
    return signed


def stack() -> tuple[PayNaka, SignedMandate]:
    """A checkpoint wired to the real rail, on durable storage, with a persistent key.

    Durable on purpose: phase two runs in a different process, and a mandate the second
    process cannot verify -- or an idempotency key it has never heard of -- would make the
    two halves unrelated runs rather than one lifecycle.

    That sentence used to be aspiration. Every call issued a *new* mandate, so the
    committed evidence showed `mnd_24ac...` on the order and the capture and `mnd_36a0...`
    on the refund -- two mandates in one lifecycle, anchored in Razorpay's own notes where
    anybody could read them. An audit did. The mandate is now written down when it is
    issued and re-verified when it is reloaded, so the whole chain carries one id.
    """
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    signer = MandateSigner(load_or_create_signing_key(str(KEY_PATH)))
    naka = PayNaka(
        rail=RazorpayRail(),
        policy=Policy.from_yaml("policy.yaml"),
        state=SqliteState(str(STATE_DB), clock=clock),
        audit=AuditChain(str(AUDIT_DB), clock=clock),
        verifier=signer.verifier(),
        clock=clock,
    )
    remembered = _remembered_mandate(signer.verifier())
    if remembered is not None:
        return naka, remembered

    issued = Issuer(signer).issue(
        ShopperIntent(
            subject="cust_kirana_001",
            session_id="sess_razorpay_test",
            budget_paise=PRICE,
            skus=(ATTA,),
            destinations=(HOME,),
            max_qty_per_sku=1,
            # Refunds are part of the lifecycle being demonstrated, so this shopper asked
            # for one. They are off by default.
            allow_refunds=True,
            ttl_seconds=24 * 60 * 60,
        ),
        clock=clock,
    )
    MANDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANDATE_PATH.write_text(json.dumps(issued.signed.to_dict(), indent=2), encoding="utf-8")
    return naka, issued.signed


def record(name: str, payload: dict[str, Any]) -> None:
    """Write one piece of evidence, with personal data stripped on the way out.

    Everything here is committed and pushed, and a Razorpay payment response carries
    whatever the shopper typed at checkout. A real mobile number reached a public
    repository this way. `redact` is applied at the point of writing rather than left to
    whoever runs the script, because the person running it is looking at a terminal and
    not at the file.
    """
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"{name}.json"
    safe = redact(payload)
    path.write_text(json.dumps(safe, indent=2, sort_keys=True, default=str), encoding="utf-8")
    say(f"{DIM}  wrote {path}{OFF}")


def show(label: str, result: Any) -> None:
    verdict = result.decision.verdict.value
    colour = GREEN if verdict == "ALLOW" else RED
    say(f"  {label:<18}{colour}{verdict:<8}{OFF}{DIM}{result.outcome}{OFF}")
    if result.decision.check_id:
        say(f"  {'':<18}{DIM}{result.decision.check_id}: {result.decision.reason}{OFF}")


# ====================================================================== phase one
def phase_one() -> int:
    naka, signed = stack()
    say()
    say(f"{BOLD}Phase 1 -- a real order, through the checkpoint{OFF}")
    say(f"{DIM}api.razorpay.com, test mode. The mandate is checked before the call.{OFF}")
    say()

    key = f"paynaka_lifecycle_{signed.mandate.mandate_id[-8:]}"
    request = MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=ATTA, qty=1, unit_paise=PRICE),),
        currency="INR",
        destination=HOME,
    )
    result = naka.execute(request, signed)
    show("create_order", result)

    if not result.executed or result.rail_result is None:
        say(f"{RED}the order was not created; nothing to check out{OFF}")
        return 1

    order_id = result.rail_result.order_id
    say()
    say(f"  {BOLD}order id{OFF}   {GREEN}{order_id}{OFF}")
    say(f"  {DIM}amount     {result.value_at_risk} paise{OFF}")
    say(f"  {DIM}outcome    {result.outcome}  <- an order, not a payment{OFF}")

    record(
        "01-order-created",
        {
            "order_id": order_id,
            "amount_paise": result.value_at_risk,
            "outcome": result.outcome,
            "decision": result.decision.to_dict(),
            "raw": getattr(result.rail_result, "raw", {}),
        },
    )

    # The gate is also asked for something the mandate forbids, on the same real rail, so
    # the evidence contains a refusal that never reached Razorpay at all.
    denied = naka.execute(
        MoneyRequest(
            action="create_order",
            request_id="req_lifecycle_attack",
            idempotency_key=f"{key}_attack",
            items=(LineItem(sku="GIFT-50K", qty=1, unit_paise=5_000_000),),
            currency="INR",
            destination=HOME,
        ),
        signed,
    )
    say()
    show("attack order", denied)
    record("02-attack-refused", {"decision": denied.decision.to_dict(), "outcome": denied.outcome})

    checkout = write_checkout(order_id)
    say()
    say(f"{BOLD}Now the part an agent must not be able to do{OFF}")
    for line in (
        f"Open {checkout} in a browser.",
        "Netbanking -> any bank -> Success is the reliable path. UPI: success@razorpay.",
        "Cards may be refused: 4111... is an INTERNATIONAL Visa and a domestic-only test",
        "account rejects it. The page explains where to find a domestic one.",
        "The page prints a payment id. Then run:",
    ):
        say(f"{DIM}  {line}{OFF}")
    say(f"  {YELLOW}python -m scripts.razorpay_test_lifecycle --payment-id pay_xxxxx{OFF}")
    say()
    return 0


def write_checkout(order_id: str) -> pathlib.Path:
    """A minimal Checkout page, because authentication belongs to the customer.

    Deliberately not part of the product. It exists so the one step PayNaka refuses to
    automate can be completed by a person, which is the whole reason it is refused.
    """
    import os

    key_id = os.environ["RAZORPAY_KEY_ID"]
    path = EVIDENCE / "checkout.html"
    path.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<title>PayNaka - complete the test payment</title>
<style>
  body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 42rem; margin: 4rem auto;
         padding: 0 1rem; }}
  code {{ background: #eee; padding: .1rem .3rem; }}
  #out {{ margin-top: 2rem; padding: 1rem; background: #f4f4f4; white-space: pre-wrap; }}
  .warn {{ border-left: 3px solid #c47f00; padding-left: .8rem; }}
  h2 {{ margin-top: 2rem; font-size: 1.1rem; }}
</style>
<h1>Complete the test payment</h1>
<p>Order <code>{order_id}</code> for &#8377;{PRICE / 100:,.2f}, created through PayNaka
against Razorpay test mode.</p>
<p>This page exists because a buying agent <em>must not</em> be able to authenticate as the
customer. That step is yours.</p>

<h2>How to pay</h2>
<p><b>Netbanking is the reliable path.</b> Choose <em>Netbanking</em>, pick any bank, and
click <em>Success</em> on the simulator page that follows. Nothing is charged.</p>
<p><b>UPI</b> also works: enter the test VPA <code>success@razorpay</code>.</p>
<p class="warn"><b>Cards may be refused.</b> Razorpay's widely-quoted test card
<code>4111 1111 1111 1111</code> is an <em>international</em> Visa, and a domestic-only test
account rejects it with &ldquo;International cards are not supported&rdquo;. If you want to
use a card, take one from the domestic list on Razorpay&rsquo;s
<a href="https://razorpay.com/docs/payments/payments/test-card-details/">test card details</a>
page rather than from memory.</p>
<button id="pay">Pay &#8377;{PRICE / 100:,.2f}</button>
<div id="out"></div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay').onclick = function () {{
  new Razorpay({{
    key: {key_id!r},
    order_id: {order_id!r},
    amount: {PRICE},
    currency: 'INR',
    name: 'Kirana Corner (fictitious)',
    description: '5kg atta',
    handler: function (r) {{
      document.getElementById('out').textContent =
        'payment_id: ' + r.razorpay_payment_id +
        '\\nsignature:  ' + r.razorpay_signature +
        '\\n\\nNow run:\\n  python -m scripts.razorpay_test_lifecycle --payment-id ' +
        r.razorpay_payment_id;
    }},
  }}).open();
}};
</script>
""",
        encoding="utf-8",
    )
    return path


# ====================================================================== phase two
def phase_two(payment_id: str) -> int:
    naka, signed = stack()
    say()
    say(f"{BOLD}Phase 2 -- capture and refund, through the checkpoint{OFF}")
    say(f"{DIM}payment {payment_id}, authenticated by a person at Checkout.{OFF}")
    say()

    # Razorpay may have captured it already. An order created without `payment_capture: 0`
    # captures on authentication, which is the common integration and is what happened
    # here: the payment came back `status: captured` before this script asked for anything.
    # Recording that truthfully matters more than forcing a capture call to appear -- a
    # lifecycle that pretends to have done a step the provider already did is the same
    # class of overclaim as calling an order "money moved".
    from paynaka.rails.razorpay_rail import RazorpayRail

    probe = RazorpayRail().fetch_payment(payment_id)

    # The provider's half of the authority graph. Razorpay has just told us which order
    # this payment settled, and that fact is what lets the gate answer "whose payment is
    # this" on the refund below. In a deployment it arrives in a `payment.captured`
    # webhook; here the same fact arrives on a fetch, because this script is standing in
    # for the webhook that a local run has no public URL to receive.
    #
    # Without it the refund is refused as payment.unknown_origin -- which is the correct
    # answer to "refund a payment that came from nowhere", and was the wrong answer here:
    # this payment came from an order PayNaka created two minutes earlier under a mandate
    # it still holds. Missing this link is what the real lifecycle exposed that no unit
    # test had.
    settled_order = str(probe.raw.get("order_id") or "")
    if settled_order:
        naka.state.link_payment(payment_id, settled_order)
        say(
            f"  {DIM}payment belongs to {settled_order}, under mandate "
            f"{signed.mandate.mandate_id}{OFF}"
        )

    if probe.raw.get("captured"):
        say(
            f"  {'capture_payment':<18}{YELLOW}{'SKIPPED':<8}{OFF}{DIM}already captured by "
            f"Razorpay on authentication{OFF}"
        )
        say(f"  {DIM}captured   {probe.amount} paise  <- money genuinely moved{OFF}")
        record(
            "03-payment-captured",
            {
                "payment_id": payment_id,
                "captured_paise": probe.amount,
                "outcome": "payment_captured",
                "captured_by": "razorpay-on-authentication",
                "note": (
                    "The order was created with Razorpay's default automatic capture, so "
                    "the payment was captured when the customer authenticated. PayNaka did "
                    "not issue the capture call and does not claim to have."
                ),
                "raw": probe.raw,
            },
        )
        # The ledger has to know, or the refund below has no balance to claim against.
        # Reconcile to the provider's reported total rather than appending what it
        # reports each time we look. Found by running this script twice: the second run
        # recorded a second capture for a payment that was captured once, and the ledger
        # said Rs 3,998 had been captured on a Rs 1,999 payment. Every downstream bound is
        # computed from that number, so an over-refund of the full amount then fitted
        # inside the inflated balance and the evidence stopped demonstrating what it
        # claimed.
        #
        # "The provider says this payment's captured total is X" is a statement about a
        # total, not an event, and observing it twice must not double it. The webhook path
        # is deduplicated by event id; a fetch has no event id, so it reconciles instead.
        naka.state.reconcile_capture(payment_id, probe.amount, clock=naka.clock)
        return _refund_phase(naka, signed, payment_id)

    captured = naka.execute(
        MoneyRequest(
            action="capture_payment",
            request_id=f"req_capture_{payment_id}",
            idempotency_key=f"capture_{payment_id}",
            amount=PRICE,
            currency="INR",
            payment_id=payment_id,
        ),
        signed,
    )
    show("capture_payment", captured)
    record(
        "03-payment-captured",
        {
            "payment_id": payment_id,
            "captured_paise": captured.captured_paise,
            "outcome": captured.outcome,
            "decision": captured.decision.to_dict(),
            "raw": getattr(captured.rail_result, "raw", {}),
        },
    )

    if not captured.executed:
        say(f"{RED}capture did not go through; skipping the refund{OFF}")
        return 1

    say(f"  {DIM}captured   {captured.captured_paise} paise  <- money genuinely moved{OFF}")
    return _refund_phase(naka, signed, payment_id)


def _refund_phase(naka: PayNaka, signed: SignedMandate, payment_id: str) -> int:
    # A partial refund, so the ledger has to do arithmetic rather than mirror the capture.
    refund_amount = 49_900
    naka.state.record_return(payment_id)
    refunded = naka.execute(
        MoneyRequest(
            action="create_refund",
            request_id=f"req_refund_{payment_id}",
            idempotency_key=f"refund_{payment_id}",
            amount=refund_amount,
            currency="INR",
            payment_id=payment_id,
        ),
        signed,
    )
    say()
    show("create_refund", refunded)
    record(
        "04-refund-created",
        {
            "payment_id": payment_id,
            "refunded_paise": refunded.captured_paise,
            "outcome": refunded.outcome,
            "decision": refunded.decision.to_dict(),
            "raw": getattr(refunded.rail_result, "raw", {}),
        },
    )

    # An over-refund on the same real payment: the ledger refuses it, and Razorpay is never
    # asked. A bound the payment provider enforces for us is not a bound we enforce.
    over = naka.execute(
        MoneyRequest(
            action="create_refund",
            request_id=f"req_overrefund_{payment_id}",
            idempotency_key=f"overrefund_{payment_id}",
            amount=PRICE,
            currency="INR",
            payment_id=payment_id,
        ),
        signed,
    )
    say()
    show("over-refund", over)
    record("05-over-refund-refused", {"decision": over.decision.to_dict()})

    say()
    say(f"{BOLD}Chain{OFF}")
    say(f"  {DIM}records   {len(naka.audit)}{OFF}")
    say(f"  {DIM}verifies  {'yes' if naka.audit.verify() is None else 'NO'}{OFF}")
    say(f"  {DIM}head      {naka.audit.head()[:16]}...{OFF}")
    say()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m scripts.razorpay_test_lifecycle")
    parser.add_argument(
        "--checkout-for",
        metavar="ORDER_ID",
        help="rewrite the checkout page for an order that already exists. For when a "
        "payment method was refused and the order is still perfectly good -- creating "
        "another one would just litter the dashboard.",
    )
    parser.add_argument(
        "--payment-id",
        help="a payment id from Checkout. Runs phase two: capture, refund, over-refund.",
    )
    args = parser.parse_args(argv)
    if args.checkout_for:
        path = write_checkout(args.checkout_for)
        say(f"{GREEN}rewrote {path}{OFF} {DIM}for {args.checkout_for}{OFF}")
        return 0
    return phase_two(args.payment_id) if args.payment_id else phase_one()


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
