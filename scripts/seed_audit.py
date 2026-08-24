"""Seed two audit chains: one intact, one deliberately broken.

`make audit-verify` on a fresh clone had nothing to verify. The hash chain is one of the
project's load-bearing claims and its demonstration was a command that printed "no audit
database" -- a dead moment in front of somebody, and worse, an unfalsifiable claim: a
verifier that has never been shown catching anything is a verifier nobody has seen work.

So this writes two fixtures and commits them:

``var/audit.db``          a real run's chain: decisions, an execution, a denial. Verifies.
``var/audit-tampered.db`` the same chain with one payload edited after the fact.

The tampered one is the point. Anybody can show a checksum passing. The interesting
demonstration is the one where somebody changed a number and the chain says exactly which
record and what it should have been -- which is the difference between "we hash things" and
"you cannot edit this without it showing".

The edit is made with plain SQL, deliberately. A reviewer with `sqlite3` can do the same
thing by hand and watch the verifier catch it, without running any of our code first.
"""

from __future__ import annotations

import pathlib
import shutil
import sqlite3

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import LineItem, MoneyRequest
from paynaka.mandate import MandateSigner, generate_keypair
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState
from paynaka.tty import BOLD, DIM, GREEN, OFF, RED, say

INTACT = pathlib.Path("var/audit.db")
TAMPERED = pathlib.Path("var/audit-tampered.db")

ATTA = "ATTA-5KG"
GIFT = "GIFT-50K"
HOME = "addr_home"
AUTHORISED = 199_900


def _order(*, sku: str, unit: int, key: str) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=sku, qty=1, unit_paise=unit),),
        currency="INR",
        destination=HOME,
    )


def build(path: pathlib.Path) -> int:
    """Run the demo scenario against a fresh chain at ``path``. Returns record count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    clock = FrozenClock.at_ist("2026-08-23 15:00")
    signer = MandateSigner(generate_keypair()[0])
    from paynaka.issuer import Issuer, ShopperIntent

    issuer = Issuer(signer)
    issued = issuer.issue(
        ShopperIntent(
            subject="cust_kirana_001",
            session_id="sess_fixture",
            budget_paise=AUTHORISED,
            skus=(ATTA,),
            destinations=(HOME,),
            max_qty_per_sku=1,
        ),
        clock=clock,
    )

    with SqliteState(":memory:", clock=clock) as state, AuditChain(str(path), clock=clock) as chain:
        naka = PayNaka(
            rail=SimRail(seed="fixture"),
            policy=Policy.from_yaml("policy.yaml"),
            state=state,
            audit=chain,
            verifier=signer.verifier(),
            clock=clock,
        )
        # An approval, then the attack the demo leads with. Both on the record, because a
        # chain that only holds successes is a receipt book.
        naka.execute(_order(sku=ATTA, unit=AUTHORISED, key="fixture_ok"), issued.signed)
        naka.execute(_order(sku=GIFT, unit=5_000_000, key="fixture_attack"), issued.signed)
        return len(chain)


def tamper(source: pathlib.Path, target: pathlib.Path) -> None:
    """Copy the chain and edit one record's payload, the way an attacker would.

    Plain SQL against the stored JSON. The record's own hash still covers what it *used* to
    say, so `verify()` recomputes it and finds the mismatch -- which is the whole mechanism,
    demonstrated rather than described.
    """
    target.unlink(missing_ok=True)
    shutil.copy(source, target)

    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT seq, payload FROM audit WHERE payload LIKE '%DENY%' ORDER BY seq LIMIT 1"
        ).fetchone()
        if row is None:  # pragma: no cover - the fixture always contains a denial
            raise SystemExit("no denial in the fixture chain to tamper with")
        edited = str(row["payload"]).replace('"DENY"', '"ALLOW"')
        conn.execute("UPDATE audit SET payload = ? WHERE seq = ?", (edited, row["seq"]))


def main() -> int:
    count = build(INTACT)
    say(f"{GREEN}wrote {INTACT}{OFF} {DIM}({count} records, chain intact){OFF}")

    tamper(INTACT, TAMPERED)
    with AuditChain(str(TAMPERED)) as chain:
        broken = chain.verify()
    say(f"{RED}wrote {TAMPERED}{OFF} {DIM}(one denial rewritten as an approval){OFF}")

    say()
    say(f"{BOLD}Try it:{OFF}")
    say(f"  {DIM}PAYNAKA_AUDIT_DB={INTACT}    python -m paynaka.audit --verify{OFF}")
    say(f"  {DIM}PAYNAKA_AUDIT_DB={TAMPERED}  python -m paynaka.audit --verify{OFF}")
    say()
    if broken is None:  # pragma: no cover - the tamper always breaks the chain
        say(f"{RED}the tampered chain still verifies; the fixture proves nothing{OFF}")
        return 1
    say(f"{DIM}the tampered chain reports: {broken}{OFF}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
