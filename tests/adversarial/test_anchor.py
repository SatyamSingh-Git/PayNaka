"""Two attacks the hash chain cannot catch on its own, and the witnesses that catch them.

``AuditChain.verify()`` checks the chain against itself, so it is blind by construction to
an attacker who rewrites the whole thing and to one who lops records off the end. Both
produce a chain that verifies perfectly, and the threat model said so in plain words for
as long as that was true.

Every test below therefore asserts **both** halves: that ``chain.verify()`` still reports
no break -- because it genuinely cannot -- and that the witnesses do. A test that only
asserted the second half would let somebody later "fix" this by making ``verify()`` claim
more than it can prove.
"""

from __future__ import annotations

import dataclasses

import pytest

from paynaka.anchor import (
    Anchor,
    AnchorError,
    AnchorLog,
    Notary,
    head_at,
    rail_note,
    verify_against_anchors,
    verify_against_rail,
    witnesses_from_rail,
)
from paynaka.audit import GENESIS, AuditChain
from paynaka.clock import FrozenClock

pytestmark = pytest.mark.adversarial

NOW = "2026-08-24 10:00"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at_ist(NOW)


@pytest.fixture
def chain(clock: FrozenClock) -> AuditChain:
    return AuditChain(":memory:", clock=clock)


@pytest.fixture
def notary() -> Notary:
    return Notary.generate()


@pytest.fixture
def log() -> AnchorLog:
    return AnchorLog(":memory:")


def grow(chain: AuditChain, n: int, *, marker: str = "honest") -> None:
    for i in range(n):
        chain.append({"kind": "decision", "n": i, "marker": marker})


def _rewrite(chain: AuditChain, n: int, *, marker: str = "forged") -> None:
    """What an attacker with total write access does: delete everything, recompute."""
    chain._conn.execute("DELETE FROM audit")
    chain._conn.execute("DELETE FROM sqlite_sequence WHERE name='audit'")
    grow(chain, n, marker=marker)


def _truncate(chain: AuditChain, keep: int) -> None:
    chain._conn.execute("DELETE FROM audit WHERE seq > ?", (keep,))


# ====================================================================== the two attacks


class TestWholesaleRewrite:
    def test_the_chain_alone_cannot_see_it(self, chain: AuditChain) -> None:
        """Stated as a test so nobody later claims the chain does more than it does."""
        grow(chain, 8)
        _rewrite(chain, 8)
        assert chain.verify() is None

    def test_a_witness_sees_it(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        grow(chain, 8)
        log.append(notary.witness(chain, clock=clock))
        _rewrite(chain, 8)

        assert chain.verify() is None, "the rewritten chain is internally perfect"
        found = verify_against_anchors(chain, log, notary.verifier())
        assert found is not None
        assert found.kind == "rewritten"

    def test_one_witness_taken_early_still_catches_it(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        """An anchor at length 2 pins everything at or before length 2, forever."""
        grow(chain, 2)
        log.append(notary.witness(chain, clock=clock))
        grow(chain, 20)
        _rewrite(chain, 22)

        found = verify_against_anchors(chain, log, notary.verifier())
        assert found is not None
        assert found.anchor.length == 2

    def test_rewriting_to_the_same_length_does_not_help(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        """Matching the count is easy; matching the hash at that count is the hard part."""
        grow(chain, 5)
        log.append(notary.witness(chain, clock=clock))
        _rewrite(chain, 5)

        assert len(chain) == 5
        assert verify_against_anchors(chain, log, notary.verifier()) is not None


class TestTrailingTruncation:
    def test_the_chain_alone_cannot_see_it(self, chain: AuditChain) -> None:
        grow(chain, 10)
        _truncate(chain, 6)
        assert chain.verify() is None

    def test_a_witness_sees_it(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        grow(chain, 10)
        log.append(notary.witness(chain, clock=clock))
        _truncate(chain, 6)

        assert chain.verify() is None
        found = verify_against_anchors(chain, log, notary.verifier())
        assert found is not None
        assert found.kind == "truncated"
        assert "at least 10" in found.expected

    @pytest.mark.parametrize("keep", [0, 1, 5, 9])
    def test_any_amount_of_truncation_is_caught(
        self,
        chain: AuditChain,
        notary: Notary,
        log: AnchorLog,
        clock: FrozenClock,
        keep: int,
    ) -> None:
        grow(chain, 10)
        log.append(notary.witness(chain, clock=clock))
        _truncate(chain, keep)
        assert verify_against_anchors(chain, log, notary.verifier()) is not None

    def test_removing_one_record_is_caught(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        grow(chain, 10)
        log.append(notary.witness(chain, clock=clock))
        _truncate(chain, 9)
        assert verify_against_anchors(chain, log, notary.verifier()) is not None


# ====================================================================== forging witnesses


class TestForgedWitnesses:
    def test_an_anchor_from_another_key_does_not_count(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        """Rewriting the chain *and* writing matching anchors still needs the key."""
        grow(chain, 5)
        attacker = Notary.generate()
        log.append(attacker.witness(chain, clock=clock))

        found = verify_against_anchors(chain, log, notary.verifier())
        assert found is not None
        assert found.kind == "forged witness"

    @pytest.mark.parametrize("field", ["at", "length", "head"])
    def test_editing_any_signed_field_invalidates_it(
        self,
        chain: AuditChain,
        notary: Notary,
        log: AnchorLog,
        clock: FrozenClock,
        field: str,
    ) -> None:
        grow(chain, 5)
        good = notary.witness(chain, clock=clock)
        edits: dict[str, object] = {"at": good.at + 1, "length": 3, "head": "f" * 64}
        log.append(dataclasses.replace(good, **{field: edits[field]}))  # type: ignore[arg-type]

        found = verify_against_anchors(chain, log, notary.verifier())
        assert found is not None
        assert found.kind == "forged witness"

    def test_the_attackers_whole_plan_fails_at_the_signature(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        """Rewrite the chain, then re-anchor it with the attacker's own notary."""
        grow(chain, 6)
        log.append(notary.witness(chain, clock=clock))
        _rewrite(chain, 6)

        attacker = Notary.generate()
        forged = AnchorLog(":memory:")
        forged.append(attacker.witness(chain, clock=clock))

        # Against the attacker's own log, everything looks fine. That is the point of
        # asking *which* key signed, rather than merely whether something signed.
        assert verify_against_anchors(chain, forged, attacker.verifier()) is None
        assert verify_against_anchors(chain, forged, notary.verifier()) is not None


# ====================================================================== honest chains


class TestNoFalseAlarms:
    """A witness that fires on an untampered chain is worse than no witness."""

    def test_an_intact_chain_verifies(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        grow(chain, 5)
        log.append(notary.witness(chain, clock=clock))
        assert verify_against_anchors(chain, log, notary.verifier()) is None

    def test_growth_after_the_last_witness_is_fine(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        """Anchors pin the past. They must not forbid a future."""
        grow(chain, 5)
        log.append(notary.witness(chain, clock=clock))
        grow(chain, 50)
        assert verify_against_anchors(chain, log, notary.verifier()) is None

    def test_many_witnesses_over_a_growing_chain(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        for _ in range(20):
            grow(chain, 3)
            log.append(notary.witness(chain, clock=clock))
        assert len(log) == 20
        assert verify_against_anchors(chain, log, notary.verifier()) is None

    def test_an_empty_chain_with_no_witnesses(
        self, chain: AuditChain, notary: Notary, log: AnchorLog
    ) -> None:
        assert verify_against_anchors(chain, log, notary.verifier()) is None

    def test_witnessing_an_empty_chain_is_not_an_error(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        log.append(notary.witness(chain, clock=clock))
        assert verify_against_anchors(chain, log, notary.verifier()) is None

    def test_a_duplicate_witness_is_stored_once(
        self, chain: AuditChain, notary: Notary, log: AnchorLog, clock: FrozenClock
    ) -> None:
        grow(chain, 4)
        anchor = notary.witness(chain, clock=clock)
        log.append(anchor)
        log.append(anchor)
        assert len(log) == 1


# ====================================================================== the rail tier


class TestWitnessedByTheGateway:
    """The tier that survives an attacker who owns every local file, including the key."""

    def _gateway_records(self, chain: AuditChain, lengths: list[int]) -> list[dict[str, object]]:
        return [
            {
                "entity": "payment",
                "notes": {
                    "paynaka_audit_len": str(length),
                    "paynaka_audit_head": rail_note(head_at(chain, length)),
                },
            }
            for length in lengths
        ]

    def test_an_intact_chain_matches_what_the_gateway_stored(self, chain: AuditChain) -> None:
        grow(chain, 10)
        assert verify_against_rail(chain, self._gateway_records(chain, [2, 5, 9])) is None

    def test_a_rewrite_contradicts_the_gateway(self, chain: AuditChain) -> None:
        grow(chain, 10)
        records = self._gateway_records(chain, [2, 5, 9])
        _rewrite(chain, 10)

        assert chain.verify() is None
        found = verify_against_rail(chain, records)
        assert found is not None
        assert found.kind == "rewritten"

    def test_truncation_contradicts_the_gateway(self, chain: AuditChain) -> None:
        grow(chain, 10)
        records = self._gateway_records(chain, [9])
        _truncate(chain, 4)

        found = verify_against_rail(chain, records)
        assert found is not None
        assert found.kind == "truncated"

    def test_notes_written_by_something_else_are_ignored(self, chain: AuditChain) -> None:
        """Other systems write notes. A note we did not write is not evidence of anything."""
        grow(chain, 5)
        records: list[dict[str, object]] = [
            {"notes": {"shopify_order": "1234"}},
            {"notes": {}},
            {},
            {"notes": {"paynaka_audit_len": "not-a-number", "paynaka_audit_head": "abc"}},
        ]
        assert verify_against_rail(chain, records) is None

    def test_records_are_read_from_either_shape(self, chain: AuditChain) -> None:
        """A gateway object, or one of our own results with its raw payload nested."""
        grow(chain, 4)
        note = {"paynaka_audit_len": "4", "paynaka_audit_head": rail_note(chain.head())}
        assert witnesses_from_rail([{"notes": note}]) == [(4, note["paynaka_audit_head"])]
        assert witnesses_from_rail([{"raw": {"notes": note}}]) == [(4, note["paynaka_audit_head"])]


# ====================================================================== the pieces


class TestHeadAt:
    def test_it_returns_the_tip_at_that_length(self, chain: AuditChain) -> None:
        heads = []
        for i in range(5):
            chain.append({"n": i})
            heads.append(chain.head())
        for index, expected in enumerate(heads, start=1):
            assert head_at(chain, index) == expected

    def test_length_zero_is_the_genesis_hash(self, chain: AuditChain) -> None:
        assert head_at(chain, 0) == GENESIS

    def test_asking_beyond_the_end_raises(self, chain: AuditChain) -> None:
        grow(chain, 3)
        with pytest.raises(AnchorError, match="cannot reach length"):
            head_at(chain, 4)


class TestAnchorValidation:
    @pytest.mark.parametrize("head", ["", "abc", "f" * 63, "f" * 65])
    def test_a_head_must_be_a_full_digest(self, head: str) -> None:
        with pytest.raises(AnchorError, match="64-character"):
            Anchor(at=0, length=1, head=head, notary="n", signature="")

    @pytest.mark.parametrize("length", [-1, -100])
    def test_a_negative_length_is_refused(self, length: int) -> None:
        with pytest.raises(AnchorError, match="negative"):
            Anchor(at=0, length=length, head="a" * 64, notary="n", signature="")

    @pytest.mark.parametrize("length", [1.5, "4", True])
    def test_a_length_must_be_an_int(self, length: object) -> None:
        with pytest.raises(AnchorError, match="must be an int"):
            Anchor(at=0, length=length, head="a" * 64, notary="n", signature="")  # type: ignore[arg-type]

    @pytest.mark.parametrize("head", ["", "short", "z" * 65])
    def test_rail_note_refuses_a_partial_head(self, head: str) -> None:
        with pytest.raises(AnchorError):
            rail_note(head)

    def test_a_rail_note_is_sixteen_hex_characters(self) -> None:
        assert len(rail_note("a" * 64)) == 16


class TestDomainSeparation:
    def test_an_anchor_signature_is_not_a_mandate_signature(self, chain: AuditChain) -> None:
        """Different domain prefix, so nothing signed here can be replayed as authority."""
        from paynaka.anchor import DOMAIN as ANCHOR_DOMAIN
        from paynaka.mandate import DOMAIN as MANDATE_DOMAIN

        assert ANCHOR_DOMAIN != MANDATE_DOMAIN
