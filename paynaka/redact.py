"""Strip personal data out of provider payloads before they are written down.

`var/evidence/` is committed, pushed and quoted -- that is the point of it. A reviewer who
can read the raw Razorpay response beside the gate's decision does not have to take either
on trust. But a raw response is written by somebody else's API, and what it contains is
their decision rather than ours.

`03-payment-captured.json` was committed to a public repository carrying a real mobile
number in `raw.contact`, entered at a test-mode checkout by the person running the demo.
Test mode does not make a phone number synthetic. It was pushed, and public git history
keeps it after the file is fixed.

So evidence is written through an allow-list rather than a block-list. The difference is
the whole design: a block-list is a list of the fields somebody thought of, and the next
provider release adds a field nobody thought of. An allow-list can only ever leak a field
that was deliberately named, and the failure mode of getting it wrong is a missing number
in a report rather than somebody's phone number on GitHub.

Redaction is visible, not silent. A dropped field becomes ``"[redacted]"`` so a reader can
see that something was removed and ask what; a file that had simply lost a key would read
as a provider that never sent one.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PERSONAL_FIELDS", "REDACTED", "redact"]

REDACTED = "[redacted]"

#: Fields a payment provider may populate with a real person's details. Named rather than
#: pattern-matched: `contact` holds a phone number and `notes` holds our own audit anchors,
#: and no regular expression tells those apart reliably enough to bet somebody's privacy on.
PERSONAL_FIELDS = frozenset(
    {
        "contact",
        "email",
        "phone",
        "customer_email",
        "customer_contact",
        "customer_id",
        "name",
        "customer_name",
        "billing_address",
        "shipping_address",
        "address",
        "vpa",
        "card",
        "card_id",
        "bank_account",
        "upi",
        "ip",
        "user_agent",
    }
)


def redact(payload: Any, *, fields: frozenset[str] = PERSONAL_FIELDS) -> Any:
    """Return `payload` with every personal field replaced by a visible marker.

    Recurses through dicts and lists, because provider responses nest -- a contact can
    arrive under `raw`, under `customer`, or inside a list of payments, and a shallow pass
    catches whichever one the test happened to produce.

    A `None` is left alone: a field the provider did not populate is not personal data, and
    turning it into `"[redacted]"` would invent a person who was never there.
    """
    if isinstance(payload, dict):
        return {
            key: (REDACTED if key in fields and value is not None else redact(value, fields=fields))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item, fields=fields) for item in payload]
    return payload
