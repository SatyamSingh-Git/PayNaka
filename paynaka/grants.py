"""Binding a mandate to an MCP session, over the wire, without trusting the client.

`McpProxy.bind()` existed and nothing outside a test fixture could reach it. An external
agent pointed at `/mcp` could initialize, list tools and call read tools; every money action
answered "no mandate for this session". The documented product -- a drop-in checkpoint in
front of an MCP server -- had no working path for the one thing it exists to do.

Worse than missing: the session identity came from a client-supplied `mcp-session-id`
header that nothing checked. Any authenticated caller could name any session, so a binding,
had one existed, would have been claimable by whoever asked for it.

So identity here comes from two places at once and neither is sufficient alone:

**Who you are** is the authenticated caller on the HTTP request -- a bearer token the
service already validates. Not a header the client chooses.

**What you may spend** is a *grant*: a short-lived, single-use ticket handed out beside a
freshly issued mandate, redeemed once at MCP `initialize`, and thereafter bound to the
authenticated caller's session key rather than to any string the client sent.

The two are combined, not alternatives. A grant redeemed by one caller does not bind
another's session, and a caller with no grant has no authority no matter what session id
they claim.

**Why a ticket rather than presenting the mandate itself.** The signed mandate is long-lived
authority and would then be travelling on every session-init, logged by every proxy in
between. A grant is worthless a few minutes after issue and worthless again the moment it is
used once, which is the property that makes it safe to hand across a network.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Final

from paynaka.clock import Clock
from paynaka.mandate import MandateVerifier, SignedMandate
from paynaka.state import SqliteState

__all__ = ["DEFAULT_GRANT_TTL", "Grant", "GrantError", "Grants"]

#: Long enough for a client to initialize a session, short enough that a leaked one is
#: usually already worthless. A grant is not a session; the mandate's own expiry still
#: governs how long the authority lasts.
DEFAULT_GRANT_TTL: Final[int] = 300

#: A grant must be unguessable. 32 bytes of urlsafe randomness is 256 bits.
_TOKEN_BYTES: Final[int] = 32


class GrantError(Exception):
    """A grant could not be issued or redeemed."""


@dataclass(frozen=True, slots=True)
class Grant:
    """A freshly issued ticket. The token is returned once and never stored in the clear."""

    token: str
    expires_at: int
    session_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "mandate_grant": self.token,
            "grant_expires_at": self.expires_at,
            "session_id": self.session_id,
        }


def token_hash(token: str) -> str:
    """What gets stored. SHA-256 of the token, so the database holds nothing spendable."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Grants:
    """Issue and redeem mandate-binding tickets against durable state."""

    __slots__ = ("_state", "_ttl", "_verifier")

    def __init__(
        self, state: SqliteState, verifier: MandateVerifier, *, ttl_seconds: int = DEFAULT_GRANT_TTL
    ) -> None:
        self._state = state
        self._verifier = verifier
        self._ttl = ttl_seconds

    def issue(self, signed: SignedMandate, *, clock: Clock) -> Grant:
        """Mint a ticket for ``signed``. Returned once; only its hash is kept."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = self._state.issue_grant(
            token_hash(token),
            json.dumps(signed.to_dict()),
            signed.mandate.subject,
            self._ttl,
            clock=clock,
        )
        return Grant(token=token, expires_at=expires_at, session_id=signed.mandate.session_id)

    def redeem(self, token: str, *, by: str, clock: Clock) -> SignedMandate:
        """Spend a ticket and return the mandate it carried.

        Raises for every failure with one message. Distinguishing "no such grant" from
        "already spent" from "expired" tells a prober whether a token they guessed ever
        existed, and none of the three is recoverable by the caller anyway.

        The mandate's signature is re-verified on the way out. The grant proves somebody was
        handed this mandate; it does not prove the stored bytes still say what they said, and
        the checkpoint should never trust its own database more than the signature it holds.
        """
        if not token or not isinstance(token, str):
            raise GrantError("no usable mandate grant was presented")

        stored = self._state.redeem_grant(token_hash(token), by, clock=clock)
        if stored is None:
            raise GrantError(
                "that mandate grant is not usable: unknown, already redeemed, or expired"
            )
        try:
            signed = SignedMandate.from_dict(json.loads(stored))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            raise GrantError("the stored mandate could not be read") from exc

        self._verifier.verify(signed)
        return signed
