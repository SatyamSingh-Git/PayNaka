"""An order is not a payment, and nothing here may say otherwise.

Razorpay's lifecycle is order → customer authentication → capture. An autonomous agent
reaches the first step and stops there: an order binds an amount and is handed to Checkout,
and no money has left an account when one is created. Capture needs a human at a payment
page, which is exactly the step this whole design says an agent must not be able to skip.

The benchmark measured that first step and called it ``money_moved``. An independent audit
flagged it, and was right to: a payments reviewer reads that field name as captured loss,
and the reported breach totals are unauthorised *order value*. The engine had already been
corrected -- ``outcome``, ``value_at_risk``, ``captured_paise`` -- and the demo already
prints ``captured ₹0.00`` beside the order value. The results tables had not caught up, and
a repository that says one thing on a screen and another in its README is arguing with
itself in front of a judge.

So this file holds the line in both directions: an order contributes nothing to captured
money, and no published table may describe order value as money that moved.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from haat.schema import RunResult

ROOT = Path(__file__).resolve().parents[2]


def result(**kwargs: object) -> RunResult:
    base: dict[str, object] = {
        "case_id": "c",
        "defence": "none",
        "family": "f",
        "money_moved": 5_199_900,
        "authorised": 199_900,
        "attack_succeeded": True,
    }
    return RunResult(**{**base, **kwargs})  # type: ignore[arg-type]


class TestAnOrderCapturesNothing:
    def test_a_created_order_contributes_zero_captured_paise(self) -> None:
        """The assertion the audit asked for by name."""
        assert result().captured_paise == 0

    def test_the_default_stage_is_the_one_every_autonomous_run_reaches(self) -> None:
        """Rows written before the field existed load with the value they in fact had --
        an order, and nothing further."""
        assert result().lifecycle_stage == "order_created"

    @pytest.mark.parametrize(
        "stage", ["order_created", "order_refused", "refused", "", "authorised"]
    )
    def test_no_pre_capture_stage_reports_captured_money(self, stage: str) -> None:
        assert result(lifecycle_stage=stage).captured_paise == 0

    @pytest.mark.parametrize("stage", ["payment_captured", "refunded"])
    def test_only_a_capture_or_a_refund_can_be_nonzero(self, stage: str) -> None:
        """The other direction. A test that only ever asserts zero would pass against a
        property hard-coded to return it."""
        assert result(lifecycle_stage=stage).captured_paise == 5_199_900

    def test_order_value_is_readable_under_a_name_that_says_what_it_is(self) -> None:
        assert result().order_value_paise == result().money_moved

    def test_the_stored_key_is_unchanged_for_committed_evidence(self) -> None:
        """The JSONL in `haat/out/` is evidence. Renaming its keys after the fact is not a
        thing an experiment gets to do, so the rename is in the prose and the properties."""
        row = result().to_dict()
        assert "money_moved" in row
        assert row["lifecycle_stage"] == "order_created"
        assert row["captured_paise"] == 0

    def test_every_committed_row_is_an_order_and_captured_nothing(self) -> None:
        """Reads the real evidence rather than a constructed row. If a future sweep ever
        does reach capture, this fails and the tables need a second column rather than a
        quiet reinterpretation of the first."""
        import json

        rows = 0
        for path in sorted((ROOT / "haat" / "out").glob("*/visible.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                stage = json.loads(line).get("lifecycle_stage", "order_created")
                assert stage == "order_created", (path.name, stage)
                rows += 1
        assert rows > 1000, "the committed sweeps went missing"


class TestThePublishedTablesDoNotOverstate:
    """A payments judge reads the table before the prose. The column header is the claim."""

    DOCUMENTS = ("README.md", "docs/EXPERIMENT.md")

    @pytest.mark.parametrize("document", DOCUMENTS)
    def test_no_results_column_is_headed_money_escaped(self, document: str) -> None:
        text = (ROOT / document).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if line.startswith("|") and re.search(r"money (escaped|moved)", line, re.I)
        ]
        assert not offenders, (
            f"{document} heads a results column with captured-money language for a figure "
            f"that is unauthorised order value: {offenders}"
        )

    @pytest.mark.parametrize("document", DOCUMENTS)
    def test_the_lifecycle_is_explained_beside_the_headline_table(self, document: str) -> None:
        """Renaming the column without saying why moves the confusion rather than removing
        it. The reader needs the order/authentication/capture sequence on the same screen."""
        text = (ROOT / document).read_text(encoding="utf-8")
        assert "Unauthorised order value" in text
        assert "captured_paise` is zero" in text or "captured_paise is zero" in text
        assert "Checkout" in text

    def test_the_readme_still_gives_its_own_warning(self) -> None:
        """The README already said calling order creation "money moved" loses a payments
        reviewer. It just had not taken its own advice. Both halves stay."""
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        assert 'Calling order creation "money moved"' in text


class TestTheDocsDescribeTheCodeTheyDocument:
    """`docs/ARCHITECTURE.md` said the schema was nine plain tables. It was thirteen.

    A number in prose is a claim, and a reviewer who counts is exactly the reviewer this
    project is written for. Counted rather than trusted, so the next table added fails a
    build here instead of a reader's arithmetic.
    """

    WORDS: ClassVar[dict[int, str]] = {
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
        13: "thirteen",
        14: "fourteen",
        15: "fifteen",
        16: "sixteen",
    }

    def test_the_table_count_in_prose_matches_the_schema(self) -> None:
        schema = (ROOT / "paynaka" / "state.py").read_text(encoding="utf-8")
        tables = schema.count("CREATE TABLE IF NOT EXISTS")
        assert tables in self.WORDS, f"{tables} tables; add the word to this test"

        claimed = f"{self.WORDS[tables]} plain tables"
        # Both documents make the claim, and only one of them was checked -- so the README
        # still said "nine" for a week after ARCHITECTURE.md was corrected. A guard that
        # covers one of two copies is a guard that finds the copy nobody reads.
        for document in ("docs/ARCHITECTURE.md", "README.md"):
            text = (ROOT / document).read_text(encoding="utf-8")
            assert claimed in text, f"{document} does not say {claimed!r}; the schema has {tables}"

    def test_quoted_latency_is_the_percentile_that_reproduces(self) -> None:
        """`make latency` prints p50 and p99. The median holds to within a few percent
        across runs; p99 ranged 1.5 ms to over 3 ms on one machine depending on load.

        A p99 figure copied into prose is a number a reviewer's own run will contradict --
        and it nearly went into the pitch script, where it would have been said aloud over
        a screen showing something else."""
        for document in ("README.md", "docs/ARCHITECTURE.md"):
            text = (ROOT / document).read_text(encoding="utf-8")
            if "µs" not in text:
                continue
            assert "p50" in text, (
                f"{document} quotes a latency without naming the percentile; only p50 "
                f"reproduces across runs"
            )

    def test_the_new_tables_are_documented_by_name(self) -> None:
        """A count alone would pass while the graph went unexplained."""
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert "authority graph" in architecture
        assert "reconcile_capture" in architecture


class TestTheReadmeQuotesTheRunItShipsWith:
    """The README printed `order_TTfEwDF...` from a run whose files were no longer in the
    repository. An audit read the committed evidence, found different ids, and was right to
    call it a discrepancy.

    Every provider id the README quotes must appear in `var/evidence/`. Cheap to check, and
    it fails the moment somebody regenerates the lifecycle and forgets the prose -- which is
    the only way this ever goes wrong.
    """

    def test_every_quoted_provider_id_is_in_the_evidence(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        evidence = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "var" / "evidence").glob("*.json")
        )
        quoted = set(re.findall(r"\b(?:order|pay|rfnd|mnd)_[A-Za-z0-9]{8,}\b", readme))
        assert quoted, "the README quotes no provider ids; the evidence section went missing"

        missing = sorted(identifier for identifier in quoted if identifier not in evidence)
        assert not missing, (
            f"README quotes ids that are in no evidence file: {missing}. The lifecycle was "
            f"regenerated and the prose was not."
        )

    def test_the_whole_chain_carries_one_mandate(self) -> None:
        """The finding itself, asserted against the files rather than the prose."""
        import json

        mandates = set()
        for name in ("01-order-created", "03-payment-captured", "04-refund-created"):
            path = ROOT / "var" / "evidence" / f"{name}.json"
            notes = (json.loads(path.read_text(encoding="utf-8")).get("raw") or {}).get("notes")
            assert notes, f"{name} carries no paynaka notes"
            mandates.add(notes["paynaka_mandate"])

        assert len(mandates) == 1, (
            f"the committed lifecycle spans {len(mandates)} mandates: {sorted(mandates)}. "
            f"Order, capture and refund must be one authority chain."
        )
