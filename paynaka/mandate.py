"""The intent mandate: a signed, scoped, expiring statement of what may be spent.

This is the object the whole design turns on. A mandate is created from the shopper's
stated intent **before** any attacker-controlled text reaches the agent, signed, and then
handed to the gate. Poisoned catalog content can change what the agent *wants*. It cannot
change what the mandate *allows*, because the mandate was sealed before the poison
existed and the agent cannot forge a new one.

Three properties make that true, and each is tested adversarially:

**Canonical serialisation.** The signature covers a byte string produced by exactly one
deterministic encoding. If two different JSON renderings of the same mandate could both
verify, an attacker could reorder or pad fields to smuggle meaning past the gate.

**Domain separation.** Signed bytes are prefixed with ``paynaka.mandate.v1``. A signature
minted for a mandate can never be replayed as a signature over some other PayNaka
structure, even if an attacker can influence both.

**Strict parsing.** Unknown fields are rejected rather than ignored. A mandate carrying
``{"max_total": 199900, "max_total_override": 5200000}`` must fail to parse, not parse
into something that quietly drops half its content.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import secrets
import uuid
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from paynaka.clock import Clock
from paynaka.money import MoneyError, to_paise

__all__ = [
    "DOMAIN",
    "IntentMandate",
    "MandateError",
    "MandateExpired",
    "MandateMalformed",
    "MandateSigner",
    "MandateVerifier",
    "SignedMandate",
    "canonical_bytes",
    "generate_keypair",
    "load_or_create_signing_key",
]

#: Domain-separation tag. Bump the version suffix if the signed field set ever changes,
#: so old signatures cannot verify against a new interpretation of the same bytes.
DOMAIN: Final[bytes] = b"paynaka.mandate.v1"

MANDATE_VERSION: Final[int] = 1

#: Actions a mandate is permitted to name. An action outside this set is a malformed
#: mandate, not an unauthorised one -- the distinction matters, because the first is a
#: bug or a forgery attempt and the second is a normal denial.
KNOWN_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "create_order",
        "create_payment_link",
        "capture_payment",
        "create_refund",
        "create_payout",
    }
)

_MAX_LIST = 256  # ceiling on any allow-list, so a mandate cannot be used as a DoS vector
_MAX_STR = 256  # ceiling on any identifier field


class MandateError(Exception):
    """Base class for every mandate failure."""


class MandateMalformed(MandateError):
    """The mandate is not structurally valid. Never treat this as a mere denial."""


class MandateExpired(MandateError):
    """The mandate was valid once and is not valid now."""


class SignatureInvalid(MandateError):
    """The signature does not verify against the presented payload."""


@dataclass(frozen=True, slots=True)
class IntentMandate:
    """What the shopper authorised. Immutable, and the signature covers all of it.

    Amounts are integer paise. ``allowed_skus`` empty means "any SKU, subject to the
    other bounds"; ``allowed_actions`` empty means "nothing is permitted", because the
    fail-closed reading of an empty permission list is the empty one.
    """

    mandate_id: str
    session_id: str
    subject: str
    max_total: int
    currency: str
    allowed_skus: tuple[str, ...]
    max_qty_per_sku: int
    allowed_destinations: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    requires_return_for_refund: bool
    issued_at: int
    expires_at: int
    nonce: str
    version: int = MANDATE_VERSION

    def __post_init__(self) -> None:
        self._validate()

    # ---------------------------------------------------------------- construction
    @classmethod
    def create(
        cls,
        *,
        clock: Clock,
        subject: str,
        session_id: str,
        max_total: int,
        ttl_seconds: int = 900,
        currency: str = "INR",
        allowed_skus: tuple[str, ...] | list[str] = (),
        max_qty_per_sku: int = 10,
        allowed_destinations: tuple[str, ...] | list[str] = (),
        allowed_actions: tuple[str, ...] | list[str] = ("create_order", "capture_payment"),
        requires_return_for_refund: bool = True,
    ) -> IntentMandate:
        """Mint a fresh mandate. The nonce and id are generated here, never supplied."""
        if ttl_seconds <= 0:
            raise MandateMalformed("ttl_seconds must be positive")
        if ttl_seconds > 86_400:
            raise MandateMalformed("ttl_seconds may not exceed 24h")

        now = clock.epoch()
        return cls(
            mandate_id=f"mnd_{uuid.uuid4().hex[:24]}",
            session_id=session_id,
            subject=subject,
            max_total=max_total,
            currency=currency,
            allowed_skus=tuple(allowed_skus),
            max_qty_per_sku=max_qty_per_sku,
            allowed_destinations=tuple(allowed_destinations),
            allowed_actions=tuple(allowed_actions),
            requires_return_for_refund=requires_return_for_refund,
            issued_at=now,
            expires_at=now + ttl_seconds,
            nonce=secrets.token_urlsafe(24),
        )

    # ---------------------------------------------------------------- validation
    def _validate(self) -> None:
        if self.version != MANDATE_VERSION:
            raise MandateMalformed(f"unsupported mandate version: {self.version}")

        for name in ("mandate_id", "session_id", "subject", "nonce", "currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise MandateMalformed(f"{name} must be a non-empty string")
            if len(value) > _MAX_STR:
                raise MandateMalformed(f"{name} exceeds {_MAX_STR} characters")

        if self.currency != "INR":
            raise MandateMalformed(f"unsupported currency: {self.currency!r}")

        try:
            to_paise(self.max_total)
        except MoneyError as exc:
            raise MandateMalformed(f"max_total is not a valid amount: {exc}") from exc
        if self.max_total <= 0:
            raise MandateMalformed("max_total must be positive")

        if isinstance(self.max_qty_per_sku, bool) or not isinstance(self.max_qty_per_sku, int):
            raise MandateMalformed("max_qty_per_sku must be int")
        if not 1 <= self.max_qty_per_sku <= 10_000:
            raise MandateMalformed(f"max_qty_per_sku out of range: {self.max_qty_per_sku}")

        for name in ("allowed_skus", "allowed_destinations", "allowed_actions"):
            self._validate_str_tuple(name, getattr(self, name))

        unknown = set(self.allowed_actions) - KNOWN_ACTIONS
        if unknown:
            raise MandateMalformed(f"unknown action(s) in mandate: {sorted(unknown)}")

        if not isinstance(self.requires_return_for_refund, bool):
            raise MandateMalformed("requires_return_for_refund must be bool")

        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise MandateMalformed(f"{name} must be an int epoch")
            if value < 0:
                raise MandateMalformed(f"{name} must not be negative")

        if self.expires_at <= self.issued_at:
            raise MandateMalformed("expires_at must be after issued_at")
        if self.expires_at - self.issued_at > 86_400:
            raise MandateMalformed("mandate lifetime may not exceed 24h")

    @staticmethod
    def _validate_str_tuple(name: str, value: object) -> None:
        if not isinstance(value, tuple):
            raise MandateMalformed(f"{name} must be a tuple")
        if len(value) > _MAX_LIST:
            raise MandateMalformed(f"{name} exceeds {_MAX_LIST} entries")
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item:
                raise MandateMalformed(f"{name} entries must be non-empty strings")
            if len(item) > _MAX_STR:
                raise MandateMalformed(f"{name} entry exceeds {_MAX_STR} characters")
            if item in seen:
                raise MandateMalformed(f"{name} contains duplicate entry: {item!r}")
            seen.add(item)

    # ---------------------------------------------------------------- lifecycle
    def is_expired(self, clock: Clock) -> bool:
        return clock.epoch() >= self.expires_at

    def assert_live(self, clock: Clock) -> None:
        """Raise if the mandate is not currently usable.

        Note the ``issued_at`` check: a mandate stamped in the future is refused rather
        than treated as merely not-yet-valid, because the only ways to produce one are a
        clock skew we should surface or a forgery we should refuse.
        """
        now = clock.epoch()
        if now < self.issued_at:
            raise MandateExpired(f"mandate {self.mandate_id} is stamped in the future")
        if now >= self.expires_at:
            raise MandateExpired(
                f"mandate {self.mandate_id} expired at {self.expires_at} (now {now})"
            )

    # ---------------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("allowed_skus", "allowed_destinations", "allowed_actions"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: object) -> IntentMandate:
        """Parse strictly. Unknown or missing fields are a hard failure."""
        if not isinstance(data, dict):
            raise MandateMalformed(f"mandate must be an object, got {type(data).__name__}")

        known = {f.name for f in fields(cls)}
        supplied = set(data)

        unknown = supplied - known
        if unknown:
            raise MandateMalformed(f"unknown field(s) in mandate: {sorted(unknown)}")

        required = {
            f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING
        }
        missing = required - supplied
        if missing:
            raise MandateMalformed(f"missing field(s) in mandate: {sorted(missing)}")

        payload = dict(data)
        for key in ("allowed_skus", "allowed_destinations", "allowed_actions"):
            value = payload.get(key, ())
            if not isinstance(value, list | tuple):
                raise MandateMalformed(f"{key} must be a list")
            payload[key] = tuple(value)

        try:
            return cls(**payload)
        except TypeError as exc:  # pragma: no cover - guarded by the checks above
            raise MandateMalformed(f"malformed mandate: {exc}") from exc


def canonical_bytes(mandate: IntentMandate) -> bytes:
    """The exact bytes a signature covers.

    One encoding, always: sorted keys, no insignificant whitespace, ASCII-escaped so the
    output is byte-identical regardless of platform or locale, prefixed with the domain
    tag. Any change here invalidates every existing signature, which is why the domain
    tag carries a version.
    """
    body = json.dumps(
        mandate.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return DOMAIN + b"|" + body.encode("ascii")


@dataclass(frozen=True, slots=True)
class SignedMandate:
    """A mandate plus the signature over its canonical bytes.

    The signature is deliberately *not* a field of :class:`IntentMandate`: a value cannot
    contain a signature over itself without a circularity, and keeping them separate makes
    it impossible to accidentally sign a payload that already carries a signature.
    """

    mandate: IntentMandate
    signature: bytes

    def to_dict(self) -> dict[str, Any]:
        return {"mandate": self.mandate.to_dict(), "signature": self.signature.hex()}

    @classmethod
    def from_dict(cls, data: object) -> SignedMandate:
        if not isinstance(data, dict):
            raise MandateMalformed("signed mandate must be an object")
        if set(data) != {"mandate", "signature"}:
            raise MandateMalformed(
                f"signed mandate must have exactly 'mandate' and 'signature', got {sorted(data)}"
            )
        raw = data["signature"]
        if not isinstance(raw, str):
            raise MandateMalformed("signature must be a hex string")
        try:
            signature = bytes.fromhex(raw)
        except ValueError as exc:
            raise MandateMalformed("signature is not valid hex") from exc
        if len(signature) != 64:
            raise MandateMalformed(f"Ed25519 signature must be 64 bytes, got {len(signature)}")
        return cls(mandate=IntentMandate.from_dict(data["mandate"]), signature=signature)


class MandateSigner:
    """Holds the private key. Exactly one instance should exist per process."""

    __slots__ = ("_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def verifier(self) -> MandateVerifier:
        return MandateVerifier(self.public_key)

    def sign(self, mandate: IntentMandate) -> SignedMandate:
        return SignedMandate(mandate=mandate, signature=self._key.sign(canonical_bytes(mandate)))


class MandateVerifier:
    """Holds only the public key, so a compromised gate cannot mint mandates."""

    __slots__ = ("_key",)

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    def verify(self, signed: SignedMandate) -> IntentMandate:
        """Return the mandate if the signature is genuine, else raise.

        Returns the payload rather than a bool so a caller cannot forget to check --
        ``verifier.verify(signed)`` used as a statement still raises, and there is no
        truthy value to accidentally ignore.
        """
        if not isinstance(signed, SignedMandate):
            raise MandateMalformed(f"expected SignedMandate, got {type(signed).__name__}")
        try:
            self._key.verify(signed.signature, canonical_bytes(signed.mandate))
        except InvalidSignature as exc:
            raise SignatureInvalid(
                f"signature does not verify for mandate {signed.mandate.mandate_id}"
            ) from exc
        return signed.mandate


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def load_or_create_signing_key(path: str) -> Ed25519PrivateKey:
    """Load a dev signing key from ``path``, creating one if absent.

    Development convenience only. The key lands in ``var/``, which ``.gitignore``
    excludes, and the file is written with owner-only permissions where the platform
    supports it. A real deployment supplies the key from a secret manager.
    """
    key_path = pathlib.Path(path)
    if key_path.exists():
        loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise MandateError(f"{path} is not an Ed25519 private key")
        return loaded

    private = Ed25519PrivateKey.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Best effort: Windows ignores POSIX modes, and that is acceptable for a dev key
    # that never leaves var/. A real deployment supplies the key from a secret manager.
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(key_path, 0o600)
    return private
