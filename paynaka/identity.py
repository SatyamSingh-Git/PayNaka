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

_ENV_VAR: Final[str] = "PAYNAKA_AGENT_TOKENS"


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


def _parse_entries(raw: str) -> dict[str, str]:
    """Parse ``name:token,name:token``. Every malformed shape is a hard failure.

    Fail closed on the way in. A silently dropped entry is a caller who cannot
    authenticate for a reason nothing reported, and the operator debugs the client.
    """
    entries: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            raise ValueError(f"{_ENV_VAR} has an empty entry; check for a stray comma")
        name, separator, token = item.partition(":")
        if not separator:
            raise ValueError(
                f"{_ENV_VAR} entry {item!r} is not 'name:token'. A bare token has no "
                f"caller name, so an audit record could not say who acted."
            )
        name = name.strip()
        if name in entries:
            raise ValueError(f"{_ENV_VAR} names caller {name!r} twice")
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
