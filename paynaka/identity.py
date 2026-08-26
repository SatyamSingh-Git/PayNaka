"""Who is asking. The other half of taking credentials away from the agent.

PayNaka's central move is that the buying agent holds no payment credentials: it cannot
move money, it can only ask. That argument has a hole in it if anything able to open a
socket to the service *is* the agent. Taking the keys away from one caller accomplishes
nothing while the asking surface is open to every caller.

So the MCP endpoint authenticates. Three decisions in that are worth stating, because each
one is the opposite of a convenience that would have been easier:

**There is no unauthenticated path, not even for development.** The tempting shape is a
check that switches off when nothing is configured, because then the demo works out of the
box. That is a bypass, and a bypass is what an attacker looks for first. Instead, when
nothing is configured the service *mints* a development credential and writes it where the
demo can read it -- the same pattern the project already uses for the dev signing key. The
check is always live; only the origin of the credential changes.

**The dev credential is refused the moment the rail is real.** ``PAYNAKA_RAIL=test`` reaches
Razorpay's API over the network. A generated-on-boot token is fine in front of an in-process
simulator and is not fine in front of anything that settles. Pointing at a real rail
requires configuring a real credential, and the service refuses to start otherwise.

**A weak token is a startup failure, not a warning.** A four-character shared secret in
front of a money API is not a smaller version of security, it is the absence of it with a
label on. Anything shorter than :data:`MIN_TOKEN_LENGTH` is refused at load time, when
somebody is watching, rather than at 3 a.m. when nobody is.

Comparison is constant-time, and every registered token is compared on every attempt --
returning early on the first match would leak, through timing, which caller a guess was
closest to.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import pathlib
import secrets
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MIN_TOKEN_LENGTH",
    "Caller",
    "TokenRegistry",
    "Unauthenticated",
    "load_approvers",
    "load_shoppers",
    "parse_bearer",
]

#: Shorter than this is refused at load time. 24 characters of the alphabet
#: :func:`secrets.token_urlsafe` draws from is ~143 bits, and the point of the floor is to
#: make a hand-typed secret impossible rather than to name the exact bit count.
MIN_TOKEN_LENGTH: Final[int] = 24

#: Where the development credential lands when nothing is configured. Under ``var/``,
#: which ``.gitignore`` excludes, beside the dev signing key.
# The suppression below is for a filesystem path, not a credential: the linter matches
# the name. Renaming it to dodge the linter would obscure what the file holds.
DEV_TOKEN_PATH: Final[str] = "var/dev-agent-token"  # noqa: S105

#: The development approver credential. A separate file because it is a separate
#: credential -- the whole point is that the agent cannot answer its own step-up.
DEV_APPROVER_PATH: Final[str] = "var/dev-approver-token"

#: The development shopper credential. Separate again, and for the sharper reason of
#: the three: this is the credential that *creates* authority, and the agent is the
#: thing that authority exists to constrain.
DEV_SHOPPER_PATH: Final[str] = "var/dev-shopper-token"

_ENV_VAR: Final[str] = "PAYNAKA_AGENT_TOKENS"
_APPROVER_ENV_VAR: Final[str] = "PAYNAKA_APPROVER_TOKENS"
_SHOPPER_ENV_VAR: Final[str] = "PAYNAKA_SHOPPER_TOKENS"


def _to_bytes(value: str) -> bytes:
    """Encode for a constant-time comparison that cannot raise.

    ``surrogatepass`` rather than the default because a lone surrogate reaching here --
    from a client that assembled the header carelessly -- must be a rejected credential,
    not a ``UnicodeEncodeError`` on the auth path.
    """
    return value.encode("utf-8", errors="surrogatepass")


class Unauthenticated(Exception):
    """The caller did not present a credential this service recognises.

    Deliberately one exception with one message shape for every failure mode -- absent
    header, wrong scheme, unknown token. Distinguishing them in the response would tell a
    prober which half of the guess was right.
    """


@dataclass(frozen=True, slots=True)
class Caller:
    """An authenticated caller. The name is safe to log; the token never is."""

    name: str


def parse_bearer(header: str | None) -> str | None:
    """Pull the token out of an ``Authorization`` header, or return ``None``.

    The scheme is matched case-insensitively because HTTP says auth schemes are
    case-insensitive. The token is not: it is compared as exact bytes, so a token with
    stray whitespace or different case is a different token.
    """
    if not header:
        return None
    scheme, separator, rest = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    # Exactly one token, and no leading or trailing space around it. `rest.strip()` would
    # quietly accept "Bearer  tok " and then the operator wonders why a copy-paste with a
    # trailing newline works in one client and not another.
    if rest != rest.strip() or not rest:
        return None
    return rest


class TokenRegistry:
    """The set of credentials this service accepts, and the names behind them."""

    __slots__ = ("_encoded", "_entries")

    def __init__(self, entries: dict[str, str]) -> None:
        """``entries`` maps caller name to token. Validated here, not at the call site."""
        for name, token in entries.items():
            if not name or name.strip() != name:
                raise ValueError(f"caller name {name!r} must be non-empty and unpadded")
            if len(token) < MIN_TOKEN_LENGTH:
                raise ValueError(
                    f"token for caller {name!r} is {len(token)} characters; "
                    f"{MIN_TOKEN_LENGTH} is the minimum. A guessable shared secret in "
                    f"front of a money API is not a weaker security posture, it is none."
                )
        # Two callers sharing a token means the audit record cannot say which one acted.
        # That is an accountability hole, so it is a startup failure rather than a
        # first-match-wins race.
        by_token: dict[str, list[str]] = {}
        for name, token in entries.items():
            by_token.setdefault(token, []).append(name)
        for names in by_token.values():
            if len(names) > 1:
                raise ValueError(
                    f"callers {sorted(names)} share one token; an audit record could not "
                    f"say which of them acted"
                )
        self._entries = dict(entries)
        # Compared as bytes, never as text. `hmac.compare_digest` raises TypeError on a
        # str containing any non-ASCII character, so comparing the header as text turns
        # one Cyrillic lookalike into an unhandled exception on the auth path -- a 500
        # where a 401 belongs, reachable by anyone, before any credential is known. The
        # encoding happens once here rather than per attempt.
        self._encoded = {name: _to_bytes(token) for name, token in entries.items()}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def names(self) -> tuple[str, ...]:
        """The configured caller names, sorted. Safe to log."""
        return tuple(sorted(self._entries))

    def shares_a_token_with(self, other: TokenRegistry) -> set[str]:
        """Caller names in ``other`` whose token is also live in this registry.

        Compared as tokens rather than names, because the dangerous configuration is not
        two entries with the same label -- it is one secret that opens two doors.
        """
        mine = set(self._encoded.values())
        return {name for name, token in other._encoded.items() if token in mine}

    def authenticate(self, header: str | None) -> Caller:
        """Return the :class:`Caller` behind an ``Authorization`` header, or raise.

        Every entry is compared even after a match is found. An early return would make a
        near-miss measurably faster than a wild guess, which is a slow way of handing an
        attacker the token one comparison at a time.
        """
        presented = _to_bytes(parse_bearer(header) or "")
        matched: str | None = None
        for name, token in self._encoded.items():
            if hmac.compare_digest(presented, token):
                matched = name
        if matched is None:
            raise Unauthenticated("no valid bearer credential presented")
        return Caller(name=matched)

    # -------------------------------------------------------------- construction
    @classmethod
    def from_env(
        cls, *, rail: str | None = None, dev_token_path: str | None = None
    ) -> TokenRegistry:
        """Build the registry from ``PAYNAKA_AGENT_TOKENS``, or mint a dev credential.

        The configured form is ``name:token`` pairs separated by commas. A token may
        contain a colon -- only the first one separates -- but not a comma.

        With nothing configured and the simulated rail, a development credential is
        generated and persisted. With nothing configured and a real rail, this raises:
        reaching Razorpay's API is not something to do behind a token that was invented
        two milliseconds ago and printed to a terminal.
        """
        raw = os.environ.get(_ENV_VAR, "").strip()
        if raw:
            return cls(_parse_entries(raw))

        choice = (rail or os.environ.get("PAYNAKA_RAIL", "sim")).strip().lower()
        if choice != "sim":
            raise ValueError(
                f"{_ENV_VAR} is not set and PAYNAKA_RAIL={choice!r} reaches a real "
                f"payment API. Configure a caller credential explicitly: "
                f"{_ENV_VAR}='buyer-agent:<token>'. The generated development credential "
                f"is only accepted in front of the in-process simulator."
            )
        return cls({"dev-agent": load_or_create_dev_token(dev_token_path or DEV_TOKEN_PATH)})


def _parse_entries(raw: str, *, var: str = _ENV_VAR) -> dict[str, str]:
    """Parse ``name:token,name:token``. Every malformed shape is a hard failure.

    Fail closed on the way in. A silently dropped entry is a caller who cannot
    authenticate for a reason nothing reported, and the operator debugs the client.
    """
    entries: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            raise ValueError(f"{var} has an empty entry; check for a stray comma")
        name, separator, token = item.partition(":")
        if not separator:
            raise ValueError(
                f"{var} entry {item!r} is not 'name:token'. A bare token has no "
                f"caller name, so an audit record could not say who acted."
            )
        name = name.strip()
        if name in entries:
            raise ValueError(f"{var} names caller {name!r} twice")
        entries[name] = token
    return entries


def load_or_create_dev_token(path: str = DEV_TOKEN_PATH) -> str:
    """Read the development credential at ``path``, generating one if absent.

    Development convenience only, and convenience of a specific kind: it removes the need
    to *invent* a credential, not the need to *present* one. A real deployment sets
    ``PAYNAKA_AGENT_TOKENS`` from a secret manager and this function is never called.
    """
    token_path = pathlib.Path(path)
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) >= MIN_TOKEN_LENGTH:
            return existing
        # A truncated or hand-edited file is replaced rather than accepted. Honouring a
        # six-character token because it happened to be on disk would reintroduce exactly
        # the weak-secret path the length floor exists to close.

    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(token_path, 0o600)
    return token


def load_approvers(agents: TokenRegistry) -> TokenRegistry:
    """The credentials permitted to answer a step-up. A separate set, deliberately.

    A step-up the buying agent can approve on its own behalf is theatre. The whole value
    of escalating is that a *different* party decides, so approving is a different
    credential rather than a flag on the same one -- and a token that appears in both sets
    is a startup failure rather than a subtle privilege overlap nobody notices.

    Configured the same way as agent tokens, in ``PAYNAKA_APPROVER_TOKENS``. Left unset,
    the returned registry is empty, and an empty registry authenticates nobody: with no
    approvers configured, every step-up runs out its window and resolves to DENY. That is
    the fail-closed direction, and it is what "unanswered" is supposed to mean.
    """
    raw = os.environ.get(_APPROVER_ENV_VAR, "").strip()
    if raw:
        approvers = TokenRegistry(_parse_entries(raw, var=_APPROVER_ENV_VAR))
    elif (os.environ.get("PAYNAKA_RAIL", "sim").strip().lower()) == "sim":
        # In front of the simulator, mint one rather than leaving the console unable to
        # approve anything. Same reasoning as the dev agent credential: the check stays
        # live and only the origin of the credential changes. A real rail still gets an
        # empty registry, so an unconfigured production deployment approves nothing.
        approvers = TokenRegistry({"dev-approver": load_or_create_dev_token(DEV_APPROVER_PATH)})
    else:
        approvers = TokenRegistry({})

    shared = set(agents.names) & set(approvers.names)
    if shared:
        raise ValueError(
            f"caller {sorted(shared)} is configured as both an agent and an approver. "
            f"A step-up the agent can approve for itself is not an escalation."
        )
    overlap = agents.shares_a_token_with(approvers)
    if overlap:
        raise ValueError(
            f"an agent credential and an approver credential are the same token "
            f"({sorted(overlap)}); the agent could approve its own step-up."
        )
    return approvers


def _assert_separate(first: TokenRegistry, second: TokenRegistry, *, why: str) -> None:
    """Two registries must share neither a name nor a token.

    Names are checked because one label in two roles is a configuration nobody can reason
    about afterwards. Tokens are checked because the *dangerous* configuration is not two
    entries with the same label -- it is one secret that opens two doors, which no amount
    of careful naming prevents.
    """
    shared = set(first.names) & set(second.names)
    if shared:
        raise ValueError(f"caller {sorted(shared)} is configured in both roles. {why}")
    overlap = first.shares_a_token_with(second)
    if overlap:
        raise ValueError(f"one token is configured in both roles ({sorted(overlap)}). {why}")


def load_shoppers(agents: TokenRegistry, approvers: TokenRegistry) -> TokenRegistry:
    """The credentials permitted to *create* authority. The sharpest separation here.

    A mandate exists to bound what the buying agent may do. If the buying agent's own
    credential can ask this service to sign one, the bound is whatever the agent asked
    for, and the design's central claim evaporates -- not by forging a signature, which is
    hard, but by requesting a genuine one, which is a POST.

    That is exactly what shipped. ``/api/intent`` authenticated against the *agent*
    registry while its own docstring said "this is the shopper's surface, not the
    agent's". An independent audit found it and asked the right question: **who is allowed
    to create the constraint?** The answer was "the constrained agent".

    So issuing is a third credential set, disjoint from both others by name and by token.
    An agent token presented here does not authenticate, and the entry's *name is the
    subject* the mandate is issued for -- a shopper credential can create authority over
    its own account and no other, server-side, with nothing in the request body able to
    change it.

    Configured in ``PAYNAKA_SHOPPER_TOKENS`` as ``subject:token`` pairs. Left unset in
    front of the simulator, a development credential is minted, the same way the agent and
    approver ones are: the check stays live and only the origin of the secret changes. In
    front of a real rail, unset means an empty registry, and an empty registry
    authenticates nobody -- so an unconfigured production deployment issues no mandates at
    all rather than issuing them to whoever asks.
    """
    raw = os.environ.get(_SHOPPER_ENV_VAR, "").strip()
    if raw:
        shoppers = TokenRegistry(_parse_entries(raw, var=_SHOPPER_ENV_VAR))
    elif (os.environ.get("PAYNAKA_RAIL", "sim").strip().lower()) == "sim":
        shoppers = TokenRegistry({"dev-shopper": load_or_create_dev_token(DEV_SHOPPER_PATH)})
    else:
        shoppers = TokenRegistry({})

    _assert_separate(
        agents,
        shoppers,
        why=(
            "A mandate the buying agent can issue for itself is not a constraint on the "
            "buying agent; it is a request form."
        ),
    )
    _assert_separate(
        approvers,
        shoppers,
        why=(
            "Approving a step-up and creating authority are different powers. One "
            "credential holding both can widen a mandate and then wave the widening "
            "through."
        ),
    )
    return shoppers
