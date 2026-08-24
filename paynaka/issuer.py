"""Where a mandate comes from. The half of the design that was assumed.

Everything else in this project is downstream of a signed `IntentMandate` already
existing. Nothing produced one. `IntentMandate.create` was called by the demo service and
by eight test harnesses, which meant the whole argument rested on an object that, in a real
deployment, nobody had written the code to make. This is that code.

Three properties, and each one is the reason this is a separate module rather than a
helper on the app:

**It holds the private key. The gate does not.** ``MandateVerifier`` has only the public
key, so a compromised checkpoint can refuse a mandate and cannot mint one. That separation
was always in the types and never demonstrated by two components actually being apart.
Here they are apart, and a test asserts the gate's verifier exposes no way to sign.

**It cannot widen what the shopper said.** Every field of the mandate is derived from the
:class:`ShopperIntent`, and every derivation is bounded by it: the budget is the stated
budget, the SKUs are the stated SKUs, the destination is the stated destination. There is
no path here that produces authority the shopper did not utter. That is checked field by
field, because "the issuer is trusted" is exactly the sentence that precedes an incident.

**It records when intent was frozen.** The design's whole claim is that intent is captured
*before* attacker-controlled text reaches the agent. `frozen_at` makes that ordering a
matter of record rather than of narration: an incident review can compare it against when
the agent first read the catalogue, and see for itself which came first.

**What this deliberately is not.** It does not parse natural language. Turning "a bag of
atta under two thousand" into a SKU and a paise ceiling is a language problem and the only
place in this system where a model would belong -- but it belongs *here*, on the shopper's
side of the boundary, reading text the shopper typed rather than text a merchant controls.
This module takes the structured result. Whatever produces that result is free to be a
model, a form, or a dropdown, and none of them can widen what comes out of here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paynaka.clock import Clock
from paynaka.mandate import IntentMandate, MandateSigner, SignedMandate

__all__ = ["IssuedMandate", "Issuer", "IssuerError", "ShopperIntent"]

#: Nobody shops for a year. A mandate is a window, and a wide window is authority left
#: lying around -- an agent that crashes and resumes tomorrow should need a new one.
MAX_TTL_SECONDS: int = 24 * 60 * 60

#: One rupee. Below this a "budget" is a typo, and a mandate authorising nothing is a
#: request that will be denied later for a reason nobody can read.
MIN_BUDGET_PAISE: int = 100


class IssuerError(Exception):
    """The stated intent could not be turned into a mandate."""


@dataclass(frozen=True, slots=True)
class ShopperIntent:
    """What a person said they wanted, in the shape the issuer can sign.

    Deliberately structured. If this were free text, the issuer would have to interpret it,
    and an interpretation is a judgment -- which is the thing this project keeps out of the
    money path. Somebody upstream turns language into these fields; from here on it is
    arithmetic.
    """

    subject: str
    session_id: str
    #: The most the shopper agreed to spend, in integer paise, across the whole basket.
    budget_paise: int
    #: What they agreed to buy. Empty means "anything", which the issuer refuses: a mandate
    #: with no SKU list is a blank cheque inside a budget.
    skus: tuple[str, ...] = ()
    #: Where it may be sent. Empty is refused for the same reason.
    destinations: tuple[str, ...] = ()
    max_qty_per_sku: int = 1
    ttl_seconds: int = 900
    currency: str = "INR"
    #: The price the shopper was *shown*, per SKU. The budget bounds the basket; this
    #: bounds the thing, and they come apart wherever the budget is the rounder number.
    reference_prices: tuple[tuple[str, int], ...] = ()
    price_tolerance_bps: int = 0
    #: Refunds are not something a shopping trip needs. Off unless asked for.
    allow_refunds: bool = False

    def __post_init__(self) -> None:
        _require(bool(self.subject and self.subject.strip()), "intent needs a subject")
        _require(bool(self.session_id and self.session_id.strip()), "intent needs a session")
        _require(
            isinstance(self.budget_paise, int) and not isinstance(self.budget_paise, bool),
            f"budget must be int paise, got {type(self.budget_paise).__name__}",
        )
        _require(
            self.budget_paise >= MIN_BUDGET_PAISE,
            f"budget {self.budget_paise} is below {MIN_BUDGET_PAISE} paise; a mandate that "
            f"authorises nothing produces a denial nobody can read",
        )
        _require(
            bool(self.skus),
            "intent must name at least one SKU; an empty allow-list is a blank cheque",
        )
        _require(
            bool(self.destinations),
            "intent must name at least one destination; an empty allow-list lets goods go anywhere",
        )
        _require(
            isinstance(self.max_qty_per_sku, int)
            and not isinstance(self.max_qty_per_sku, bool)
            and self.max_qty_per_sku >= 1,
            f"max_qty_per_sku must be a positive int, got {self.max_qty_per_sku!r}",
        )
        _require(
            isinstance(self.ttl_seconds, int)
            and not isinstance(self.ttl_seconds, bool)
            and 0 < self.ttl_seconds <= MAX_TTL_SECONDS,
            f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}, got {self.ttl_seconds!r}",
        )
        for sku, price in self.reference_prices:
            _require(
                sku in self.skus,
                f"reference price names {sku!r}, which the shopper did not ask for",
            )
            _require(
                isinstance(price, int) and not isinstance(price, bool) and price > 0,
                f"reference price for {sku!r} must be positive int paise, got {price!r}",
            )


@dataclass(frozen=True, slots=True)
class IssuedMandate:
    """A signed mandate and the intent it came from, kept together.

    The intent travels alongside so an incident review can read what the shopper said next
    to what the system was told they said, without trusting a summary of either.
    """

    signed: SignedMandate
    intent: ShopperIntent
    frozen_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "signed": self.signed.to_dict(),
            "frozen_at": self.frozen_at,
            "intent": {
                "subject": self.intent.subject,
                "session_id": self.intent.session_id,
                "budget_paise": self.intent.budget_paise,
                "skus": list(self.intent.skus),
                "destinations": list(self.intent.destinations),
                "max_qty_per_sku": self.intent.max_qty_per_sku,
                "currency": self.intent.currency,
                "allow_refunds": self.intent.allow_refunds,
            },
        }


class Issuer:
    """Turns stated intent into a signed mandate. Holds the private key; the gate does not.

    One instance per deployment, on the shopper's side of the trust boundary. In this
    repository the demo service constructs one in the same process as the gate, because
    there is one process -- and that is a property of the demo rather than of the design.
    The engine only ever receives a :class:`~paynaka.mandate.MandateVerifier`.
    """

    __slots__ = ("_signer",)

    def __init__(self, signer: MandateSigner) -> None:
        self._signer = signer

    @property
    def public_key_holder(self) -> Any:
        """The verifier to hand the gate. Deliberately the only thing exported downstream."""
        return self._signer.verifier()

    def issue(self, intent: ShopperIntent, *, clock: Clock) -> IssuedMandate:
        """Sign a mandate that says exactly what the shopper said, and no more.

        Every field is taken from ``intent``. There is no default here that grants
        something unstated -- ``allowed_actions`` in particular is narrowed to ordering and
        capture unless refunds were explicitly asked for, because a shopping trip that can
        issue refunds is a wider authority than anybody asked for by saying "buy me atta".
        """
        actions: tuple[str, ...] = ("create_order", "capture_payment")
        if intent.allow_refunds:
            actions = (*actions, "create_refund")

        frozen_at = clock.epoch()
        mandate = IntentMandate.create(
            clock=clock,
            subject=intent.subject,
            session_id=intent.session_id,
            max_total=intent.budget_paise,
            ttl_seconds=intent.ttl_seconds,
            currency=intent.currency,
            allowed_skus=intent.skus,
            max_qty_per_sku=intent.max_qty_per_sku,
            allowed_destinations=intent.destinations,
            allowed_actions=actions,
            requires_return_for_refund=True,
            reference_prices=intent.reference_prices,
            price_tolerance_bps=intent.price_tolerance_bps,
        )
        _assert_not_widened(mandate, intent)
        return IssuedMandate(signed=self._signer.sign(mandate), intent=intent, frozen_at=frozen_at)


def _assert_not_widened(mandate: IntentMandate, intent: ShopperIntent) -> None:
    """The issuer's own audit of itself, run on every issue.

    Belt and braces on purpose. The mapping above is short and obvious today, and the way
    this goes wrong later is somebody adding a convenience default that grants a little
    more than was asked. This turns that into a failure at issue time rather than an
    authority nobody notices until it is spent.
    """
    if mandate.max_total > intent.budget_paise:
        raise IssuerError(
            f"issued budget {mandate.max_total} exceeds the stated {intent.budget_paise}"
        )
    extra_skus = set(mandate.allowed_skus) - set(intent.skus)
    if extra_skus:
        raise IssuerError(f"issued mandate allows SKUs nobody asked for: {sorted(extra_skus)}")
    extra_destinations = set(mandate.allowed_destinations) - set(intent.destinations)
    if extra_destinations:
        raise IssuerError(
            f"issued mandate allows destinations nobody asked for: {sorted(extra_destinations)}"
        )
    if mandate.max_qty_per_sku > intent.max_qty_per_sku:
        raise IssuerError(
            f"issued quantity ceiling {mandate.max_qty_per_sku} exceeds the stated "
            f"{intent.max_qty_per_sku}"
        )
    if "create_refund" in mandate.allowed_actions and not intent.allow_refunds:
        raise IssuerError("issued mandate permits refunds and the shopper did not ask for them")
    if mandate.currency != intent.currency:
        raise IssuerError(f"issued currency {mandate.currency} is not the stated {intent.currency}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IssuerError(message)
