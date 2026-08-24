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


def stack() -> tuple[PayNaka, SignedMandate]:
    """A checkpoint wired to the real rail, on durable storage, with a persistent key.

    Durable on purpose: phase two runs in a different process, and a mandate the second
    process cannot verify -- or an idempotency key it has never heard of -- would make the
    two halves unrelated runs rather than one lifecycle.
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
    return naka, issued.signed


def record(name: str, payload: dict[str, Any]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
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
        f"Open {checkout} in a browser and pay with Razorpay's test card.",
        "Card 4111 1111 1111 1111, any future expiry, any CVV, any OTP.",
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
</style>
<h1>Complete the test payment</h1>
<p>Order <code>{order_id}</code> for &#8377;{PRICE / 100:,.2f}, created through PayNaka
against Razorpay test mode.</p>
<p>This page exists because a buying agent <em>must not</em> be able to authenticate as the
customer. That step is yours.</p>
<p>Test card <code>4111 1111 1111 1111</code>, any future expiry, any CVV, any OTP.</p>
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
        "--payment-id",
        help="a payment id from Checkout. Runs phase two: capture, refund, over-refund.",
    )
    args = parser.parse_args(argv)
    return phase_two(args.payment_id) if args.payment_id else phase_one()


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
