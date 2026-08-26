"""Nothing personal may reach `var/evidence/`, which is committed and public.

The evidence directory is the project's strongest artefact: raw Razorpay responses sitting
beside the gate's decisions, so a reviewer can check one against the other instead of
believing either. It is also written by somebody else's API, and a payment response carries
whatever the shopper typed at a checkout.

`03-payment-captured.json` was committed and pushed carrying a real mobile number in
`raw.contact`. Test mode does not make a phone number synthetic, and public git history
keeps it after the file is fixed. The redaction is an allow-list rather than a block-list
because a block-list is a list of the fields somebody thought of, and the next provider
release adds one nobody thought of.

Two directions here. Personal data must not survive redaction -- and the evidence must
still be evidence afterwards, because a scrubber that removed the amounts and the audit
head would leave a directory of empty files that proved nothing and failed no test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from paynaka.redact import PERSONAL_FIELDS, REDACTED, redact

pytestmark = pytest.mark.adversarial

EVIDENCE = Path(__file__).resolve().parents[2] / "var" / "evidence"

#: Deliberately broader than the allow-list. The allow-list decides what gets removed; this
#: asks whether anything that *looks* like a person survived by another route -- a new
#: provider field, a nested copy, a value pasted into a description.
PII_PATTERNS = (
    ("phone", re.compile(r"\+\d{1,3}[\s-]?\d{10}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)

#: Razorpay's own documented placeholder for a test-mode payment with no real customer.
#: It is not a person, and refusing it would fail the suite over a synthetic value.
ALLOWED_ADDRESSES = {"void@razorpay.com"}


def evidence_files() -> list[Path]:
    if not EVIDENCE.is_dir():  # pragma: no cover - the directory is committed
        pytest.skip("no committed evidence directory")
    return sorted(EVIDENCE.glob("*.json"))


class TestNothingPersonalIsCommitted:
    @pytest.mark.parametrize("pattern_name", [name for name, _ in PII_PATTERNS])
    def test_no_evidence_file_matches_a_personal_pattern(self, pattern_name: str) -> None:
        pattern = dict(PII_PATTERNS)[pattern_name]
        offenders: list[str] = []
        for path in evidence_files():
            for hit in pattern.findall(path.read_text(encoding="utf-8")):
                if hit in ALLOWED_ADDRESSES:
                    continue
                offenders.append(f"{path.name}: {hit}")
        assert not offenders, (
            f"{pattern_name} in committed evidence: {offenders}. This directory is public. "
            "Add the field to PERSONAL_FIELDS and re-run the lifecycle, or redact in place."
        )

    def test_the_field_that_leaked_is_redacted_and_visibly_so(self) -> None:
        """`raw.contact` specifically. A missing key would read as a provider that never
        sent one; the marker says a value was removed and invites the question."""
        captured = EVIDENCE / "03-payment-captured.json"
        if not captured.is_file():  # pragma: no cover
            pytest.skip("no captured-payment evidence")
        raw = json.loads(captured.read_text(encoding="utf-8"))["raw"]
        assert "contact" in raw, "the field was deleted rather than redacted"
        assert raw["contact"] == REDACTED


class TestTheEvidenceIsStillEvidence:
    """The other direction. A scrubber that emptied these files would pass every test
    above and destroy the only artefact that proves the code ran against a real API."""

    def test_the_money_and_the_lifecycle_survive(self) -> None:
        raw = json.loads((EVIDENCE / "03-payment-captured.json").read_text(encoding="utf-8"))
        assert raw["captured_paise"] == 199_900
        assert raw["outcome"] == "payment_captured"
        assert raw["raw"]["status"] == "captured"
        assert raw["raw"]["order_id"].startswith("order_")

    def test_the_audit_anchor_survives(self) -> None:
        """The notes Razorpay stored on our behalf are the cross-check a reviewer runs
        against the local hash chain. Redacting them would remove the proof."""
        notes = json.loads((EVIDENCE / "03-payment-captured.json").read_text(encoding="utf-8"))[
            "raw"
        ]["notes"]
        assert notes["paynaka_mandate"].startswith("mnd_")
        assert notes["paynaka_audit_head"]
        assert "redacted" not in json.dumps(notes).lower()


class TestTheRedactorItself:
    @pytest.mark.parametrize("field", sorted(PERSONAL_FIELDS))
    def test_every_named_field_is_removed_wherever_it_sits(self, field: str) -> None:
        """Nested, because a provider response nests: a contact arrives under `raw`, under
        `customer`, or inside a list of payments, and a shallow pass catches whichever one
        the test happened to produce."""
        payload = {"a": {field: "sensitive"}, "b": [{"c": {field: "sensitive"}}]}
        cleaned = json.dumps(redact(payload))
        assert "sensitive" not in cleaned, field

    def test_a_field_the_provider_left_empty_stays_empty(self) -> None:
        """`"vpa": null` is not personal data. Marking it redacted would invent a person
        who was never there, in a file whose whole value is that it was not edited."""
        assert redact({"vpa": None}) == {"vpa": None}

    def test_it_does_not_touch_anything_else(self) -> None:
        payload = {"amount": 199_900, "notes": {"paynaka_mandate": "mnd_x"}, "status": "captured"}
        assert redact(payload) == payload

    @pytest.mark.parametrize("payload", [None, 0, "", [], {}, [None], {"a": None}])
    def test_odd_shapes_do_not_raise(self, payload: object) -> None:
        redact(payload)

    def test_it_is_idempotent(self) -> None:
        """Evidence gets rewritten by re-runs. Redacting a redacted file must not turn the
        marker itself into something else."""
        once = redact({"contact": "+911234567890"})
        assert redact(once) == once

    def test_the_writer_redacts_rather_than_trusting_the_caller(self) -> None:
        """At the point of writing, not left to whoever runs the script -- they are looking
        at a terminal, not at the file."""
        source = (
            Path(__file__).resolve().parents[2] / "scripts" / "razorpay_test_lifecycle.py"
        ).read_text(encoding="utf-8")
        assert "redact(payload)" in source
