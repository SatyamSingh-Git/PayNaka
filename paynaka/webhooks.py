"""Receiving what the payment provider says happened, and proving it said it.

`chaos/` established what the engine does with duplicate, reordered and lost deliveries,
and did it entirely in process. That proves the semantics and leaves a gap: a real webhook
is an HTTP POST from Razorpay, and this system had nothing to receive one. "We handle
duplicate webhooks" was true of the engine and untrue of the deployment.

The gap is not just plumbing. A webhook endpoint says *money moved* -- it is an instruction
to write the ledger, arriving over the open internet, from a source anybody can imitate.
Unauthenticated, it is a forge-a-capture hole with a URL. So the first thing this module
does is refuse to believe anything it cannot verify.

**HMAC-SHA256 over the raw body, exactly as Razorpay specifies.** Over the *bytes* as they
arrived, never over a re-serialised parse: JSON round-tripping reorders keys and changes
whitespace, and a signature over the reconstruction is a signature over a different
document. That mistake fails open -- it accepts a body that was tampered with in a way the
parser normalises away.

**Constant-time comparison**, for the same reason as `identity.py`: a comparison that
returns early tells an attacker how much of a forged signature was right.

**Fail closed on configuration.** No secret configured means no webhook is accepted, not
that every webhook is accepted. The tempting shape -- "verification is optional in
development" -- is a bypass, and a bypass is what gets found.

**A verified webhook is still not trusted content.** It is trusted to have come from
Razorpay; what it *claims* still passes through the ledger's own invariants. A genuine
`payment.captured` for an amount that exceeds what was authorised is a real message about
a real problem, and the money-correctness checks are what catch it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "EVENT_ID_HEADER",
    "WEBHOOK_SECRET_ENV_VAR",
    "WebhookError",
    "WebhookEvent",
    "load_webhook_secret",
    "parse_event",
    "verify_signature",
]

# The suppression is for the *name* of an environment variable, not a credential. The
# linter matches on the identifier; renaming it to dodge that would obscure what it is.
WEBHOOK_SECRET_ENV_VAR: Final[str] = "RAZORPAY_WEBHOOK_SECRET"  # noqa: S105

#: Razorpay signs the raw request body with the endpoint secret using HMAC-SHA256 and
#: sends the hex digest in this header.
SIGNATURE_HEADER: Final[str] = "X-Razorpay-Signature"

#: Razorpay's own handle for a delivery, and what its documentation says to deduplicate on.
#: This module originally read an `id` field off the top of the body, which is a shape I
#: invented: a redelivery would have arrived with no dependable id and duplicate suppression
#: would have silently had nothing to work with.
EVENT_ID_HEADER: Final[str] = "X-Razorpay-Event-Id"

#: A hex SHA-256 digest is exactly this long. Checked before comparing so a wildly
#: malformed header is refused on shape rather than on content.
DIGEST_HEX_LENGTH: Final[int] = 64

#: Bodies above this are refused unread. Razorpay's events are a few kilobytes; anything
#: at this scale is either a bug or somebody making the server do work.
MAX_BODY_BYTES: Final[int] = 1_000_000

#: The secret is a shared key protecting a money path. The floor exists for the same
#: reason as the one in `identity.py`.
MIN_SECRET_LENGTH: Final[int] = 16


class WebhookError(Exception):
    """The webhook was not accepted.

    One exception for every reason -- bad signature, absent header, unparseable body --
    because distinguishing them tells a prober which half of a forgery was right.
    """


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """A verified event, in the shape the rest of the system uses.

    Construction happens only after the signature checks out, so holding one of these is
    evidence the payload arrived from whoever knows the endpoint secret.
    """

    event: str
    payment_id: str | None
    order_id: str | None
    amount: int | None
    #: The provider's own idempotency handle for this delivery. Razorpay repeats it on a
    #: redelivery, which is what makes duplicate suppression possible at all.
    event_id: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "amount": self.amount,
            "event_id": self.event_id,
        }


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Whether ``signature`` is a genuine HMAC-SHA256 of ``body`` under ``secret``.

    ``body`` must be the bytes as they arrived. Passing a re-serialised parse here is the
    subtle way to get this wrong, and it fails *open*: a body whose tampering the parser
    normalised away would verify against its own normalisation.
    """
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        # A weak or absent secret verifies nothing. Returning False rather than raising
        # keeps this a pure predicate; the endpoint refuses to start without a real one.
        return False
    if not signature or len(signature) != DIGEST_HEX_LENGTH:
        return False
    if len(body) > MAX_BODY_BYTES:
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Both sides are hex, so ASCII, so `compare_digest` on str is safe here -- but bytes
    # cost nothing and remove the question entirely.
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "replace"))


def parse_event(body: bytes, *, event_id: str | None = None) -> WebhookEvent:
    """Turn a verified body into an event, refusing anything that is not one.

    Every field is read defensively. A verified webhook proves who sent it and says nothing
    about the shape of what they sent, and a provider is free to add fields, rename nothing,
    and send a payload this code has never seen.

    ``event_id`` comes from the ``X-Razorpay-Event-Id`` header, which is where Razorpay
    documents it and the only place a redelivery reliably repeats it. The body's top-level
    ``id`` is used only as a fallback, and it is a fallback rather than the source because
    reading it there was this module's original mistake.
    """
    if len(body) > MAX_BODY_BYTES:
        raise WebhookError("webhook body is too large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WebhookError("webhook body is not JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookError("webhook body must be a JSON object")

    event = payload.get("event")
    if not isinstance(event, str) or not event:
        raise WebhookError("webhook names no event")

    entity = _entity(payload)
    amount = entity.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        # Money crosses every boundary in this system as int paise. A float or a string
        # here is not something to coerce -- coercion is how a ledger learns to believe
        # "1999.00" is 1999 paise.
        amount = None

    return WebhookEvent(
        event=event,
        payment_id=_text(entity.get("id"))
        if "payment" in event
        else _text(entity.get("payment_id")),
        order_id=_text(entity.get("order_id")),
        amount=amount,
        event_id=event_id or _text(payload.get("id")),
        raw=payload,
    )


def _entity(payload: dict[str, Any]) -> dict[str, Any]:
    """Dig the entity out of Razorpay's ``payload.<kind>.entity`` nesting, or give up.

    Gives up by returning an empty mapping rather than raising: an event whose body this
    code cannot navigate is still an event that arrived and should be recorded as one.
    """
    container = payload.get("payload")
    if not isinstance(container, dict):
        return {}
    for kind in ("payment", "refund", "order"):
        wrapper = container.get(kind)
        if isinstance(wrapper, dict):
            entity = wrapper.get("entity")
            if isinstance(entity, dict):
                return entity
    return {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_webhook_secret(*, rail: str | None = None) -> str:
    """The endpoint secret, or an explanation of why there is no usable one.

    Absent, this raises rather than returning something falsy that a caller might treat as
    "verification off". The only way to accept a webhook is to have a secret.
    """
    secret = os.environ.get(WEBHOOK_SECRET_ENV_VAR, "").strip()
    choice = (rail or os.environ.get("PAYNAKA_RAIL", "sim")).strip().lower()

    if not secret:
        raise WebhookError(
            f"{WEBHOOK_SECRET_ENV_VAR} is not set, so no webhook can be verified and none "
            f"will be accepted. Set it to the secret configured on the Razorpay dashboard "
            f"for this endpoint. There is no development mode that skips verification: an "
            f"unverified webhook is an instruction to write the ledger from anybody at all."
            + ("" if choice == "sim" else f" PAYNAKA_RAIL={choice!r} reaches a real payment API.")
        )
    if len(secret) < MIN_SECRET_LENGTH:
        raise WebhookError(
            f"{WEBHOOK_SECRET_ENV_VAR} is {len(secret)} characters; {MIN_SECRET_LENGTH} is "
            f"the minimum for a shared key in front of a money path."
        )
    return secret
