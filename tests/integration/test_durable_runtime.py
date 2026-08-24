"""Does anything survive a restart?

`.env.example` documented `PAYNAKA_AUDIT_DB` and `PAYNAKA_SIGNING_KEY_PATH` and the app read
neither. It ran on a frozen clock, two in-memory databases, and a signing key generated
fresh on every boot — so a restart erased idempotency, mandate spend, escalations, the audit
chain, and the identity that had signed everything in it. The checkpoint forgot every
promise it had made, and an independent review was right that this is most of why
"production readiness" scored what it did.

The demo defaults are unchanged. What is under test here is that *configuring* the paths
turns this into something that remembers, because a documented setting the code ignores is
worse than an undocumented one: somebody sets it and believes it worked.

The property that matters is not "a file exists". It is **a promise made before the restart
is still binding after it** — the same idempotency key still refuses, the same mandate is
still exhausted, the same chain still verifies against the same head.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paynaka.audit import AuditChain
from paynaka.clock import FrozenClock
from paynaka.engine import PayNaka
from paynaka.gate import LineItem, MoneyRequest, Verdict
from paynaka.issuer import Issuer, ShopperIntent
from paynaka.mandate import MandateSigner, SignedMandate, load_or_create_signing_key
from paynaka.policy import Policy
from paynaka.rails.sim import SimRail
from paynaka.state import SqliteState

ATTA = "ATTA-5KG"
HOME = "addr_home"
BUDGET = 199_900


def order(key: str, unit: int = BUDGET) -> MoneyRequest:
    return MoneyRequest(
        action="create_order",
        request_id=f"req_{key}",
        idempotency_key=key,
        items=(LineItem(sku=ATTA, qty=1, unit_paise=unit),),
        currency="INR",
        destination=HOME,
    )


class Deployment:
    """One process lifetime, over storage that outlives it."""

    def __init__(self, root: Path, signer: MandateSigner) -> None:
        self.clock = FrozenClock.at_ist("2026-08-23 15:00")
        self.state = SqliteState(str(root / "state.db"), clock=self.clock)
        self.audit = AuditChain(str(root / "audit.db"), clock=self.clock)
        self.naka = PayNaka(
            rail=SimRail(seed="durable"),
            policy=Policy.from_yaml("policy.yaml"),
            state=self.state,
            audit=self.audit,
            verifier=signer.verifier(),
            clock=self.clock,
        )

    def close(self) -> None:
        self.state.close()
        self.audit.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def signer(tmp_path: Path) -> MandateSigner:
    """Loaded from a path, so every restart signs as the same identity."""
    return MandateSigner(load_or_create_signing_key(str(tmp_path / "signing.key")))


@pytest.fixture
def signed(signer: MandateSigner) -> SignedMandate:
    return (
        Issuer(signer)
        .issue(
            ShopperIntent(
                subject="cust_1",
                session_id="sess_durable",
                budget_paise=BUDGET,
                skus=(ATTA,),
                destinations=(HOME,),
                max_qty_per_sku=1,
            ),
            clock=FrozenClock.at_ist("2026-08-23 15:00"),
        )
        .signed
    )


class TestAPromiseSurvivesTheRestart:
    def test_an_idempotency_key_still_refuses_after_a_restart(
        self, root: Path, signer: MandateSigner, signed: SignedMandate
    ) -> None:
        """The one that costs real money when it is wrong: a gateway retrying across a
        deploy is exactly the case `make chaos` measures, and in-memory state is not
        deduplication -- it is a cache and some luck."""
        first = Deployment(root, signer)
        original = first.naka.execute(order("k"), signed)
        first.close()

        second = Deployment(root, signer)
        replay = second.naka.execute(order("k"), signed)
        second.close()

        assert original.executed is True
        assert replay.decision.replayed is True
        assert replay.executed is False

    def test_the_original_result_is_recoverable_after_a_restart(
        self, root: Path, signer: MandateSigner, signed: SignedMandate
    ) -> None:
        first = Deployment(root, signer)
        original = first.naka.execute(order("k"), signed)
        order_id = original.rail_result.order_id
        first.close()

        second = Deployment(root, signer)
        replay = second.naka.execute(order("k"), signed)
        second.close()

        assert replay.original_result is not None
        assert replay.original_result.order_id == order_id

    def test_a_spent_mandate_is_still_spent_after_a_restart(
        self, root: Path, signer: MandateSigner, signed: SignedMandate
    ) -> None:
        """Otherwise restarting the service is a way to refill a budget, which is the
        double-spend again with extra steps."""
        first = Deployment(root, signer)
        assert first.naka.execute(order("a"), signed).value_at_risk == BUDGET
        first.close()

        second = Deployment(root, signer)
        refused = second.naka.execute(order("b"), signed).decision
        second.close()

        assert refused.verdict is Verdict.DENY
        assert refused.check_id == "envelope.mandate_exhausted"

    def test_a_revocation_outlives_the_process(
        self, root: Path, signer: MandateSigner, signed: SignedMandate
    ) -> None:
        """A kill switch that a restart clears is not a kill switch."""
        first = Deployment(root, signer)
        first.naka.state.revoke("sess_durable")
        first.close()

        second = Deployment(root, signer)
        refused = second.naka.execute(order("after"), signed).decision
        second.close()
        assert refused.verdict is Verdict.DENY

    def test_the_chain_still_verifies_and_continues(
        self, root: Path, signer: MandateSigner, signed: SignedMandate
    ) -> None:
        """A chain that restarts from zero is a receipt book, not a chain."""
        first = Deployment(root, signer)
        first.naka.execute(order("a"), signed)
        before = len(first.audit)
        head = first.audit.head()
        first.close()

        second = Deployment(root, signer)
        assert second.audit.verify() is None
        assert len(second.audit) == before
        assert second.audit.head() == head
        second.naka.execute(order("b"), signed)
        assert len(second.audit) > before
        assert second.audit.verify() is None
        second.close()


class TestTheIdentitySurvivesToo:
    def test_a_persisted_key_signs_as_the_same_identity(self, tmp_path: Path) -> None:
        """A key regenerated at boot invalidates every mandate ever issued, which makes the
        signature check meaningless across a restart."""
        path = str(tmp_path / "signing.key")
        first = MandateSigner(load_or_create_signing_key(path))
        second = MandateSigner(load_or_create_signing_key(path))

        issued = Issuer(first).issue(
            ShopperIntent(
                subject="c",
                session_id="s",
                budget_paise=BUDGET,
                skus=(ATTA,),
                destinations=(HOME,),
            ),
            clock=FrozenClock.at_ist("2026-08-23 15:00"),
        )
        # The mandate the *first* process signed verifies against the *second* process.
        assert second.verifier().verify(issued.signed).mandate_id

    def test_a_generated_key_does_not(self, tmp_path: Path) -> None:
        """The control, and the behaviour the demo still has by default."""
        from paynaka.mandate import generate_keypair

        first = MandateSigner(generate_keypair()[0])
        second = MandateSigner(generate_keypair()[0])
        issued = Issuer(first).issue(
            ShopperIntent(
                subject="c",
                session_id="s",
                budget_paise=BUDGET,
                skus=(ATTA,),
                destinations=(HOME,),
            ),
            clock=FrozenClock.at_ist("2026-08-23 15:00"),
        )
        with pytest.raises(Exception, match="signature does not verify"):
            second.verifier().verify(issued.signed)


class TestTheServiceReportsWhichRuntimeItIs:
    """An operator should not have to guess whether what they are running remembers."""

    def test_the_default_demo_runtime_says_it_is_ephemeral(self) -> None:
        """Storage and clock, which are what "does this remember anything" turns on.

        The signing key is asserted separately below: the suite pins it to a throwaway
        path so a test run does not sign with the developer's real key, so this process
        legitimately reports `persistent` and that is not what is under test here.
        """
        from fastapi.testclient import TestClient

        from paynaka.app import app

        with TestClient(app) as client:
            body = client.get("/api/health").json()
        assert body["durable"] is False
        assert body["clock"] == "frozen"

    def test_an_unconfigured_key_is_reported_as_generated_at_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`load_env()` re-reads `.env` at startup, so the variable is cleared *and* the
        reload is stubbed. Deleting it alone looks like it works and does not -- which is
        how a test run ended up writing 28 records into a committed audit fixture."""
        from fastapi.testclient import TestClient

        monkeypatch.setattr("paynaka.app.load_env", lambda *a, **k: {})
        monkeypatch.delenv("PAYNAKA_SIGNING_KEY_PATH", raising=False)

        from paynaka.app import app

        with TestClient(app) as client:
            body = client.get("/api/health").json()
        assert body["signing_key"] == "generated-at-boot"

    def test_configuring_storage_flips_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PAYNAKA_STATE_DB", str(tmp_path / "state.db"))
        monkeypatch.setenv("PAYNAKA_AUDIT_DB", str(tmp_path / "audit.db"))
        monkeypatch.setenv("PAYNAKA_SIGNING_KEY_PATH", str(tmp_path / "signing.key"))

        from paynaka.app import app

        with TestClient(app) as client:
            body = client.get("/api/health").json()
        assert body["durable"] is True
        assert body["clock"] == "system"
        assert body["signing_key"] == "persistent"
