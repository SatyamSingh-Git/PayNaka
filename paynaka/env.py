"""Load ``.env`` once, from every entry point that needs it.

A ``.env`` file that nothing reads is worse than no ``.env`` file: the operator does the
right thing, sets a key, and is told the key is missing. So every command-line entry point
calls :func:`load_env` before it looks at the environment.

Two rules, both of which exist because the alternative bites someone:

**A real environment variable wins.** ``.env`` fills gaps; it never overrides. Otherwise a
stale file silently beats the value someone deliberately exported for one command, and
they debug the wrong thing for twenty minutes.

**Loading is idempotent and quiet.** Import order across modules is not something a caller
should have to reason about, and a missing ``.env`` is normal in CI rather than an error.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env", "redact", "require_model_key"]

_LOADED = False


def load_env(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Read ``path`` into ``os.environ``. Returns the names it set, mapped to redactions.

    Hand-parsed rather than pulled from a library: the format is four lines of logic, and
    the value here is that a reviewer can see exactly what touches their credentials.
    """
    global _LOADED

    file = Path(path)
    if not file.exists():
        _LOADED = True
        return {}

    applied: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue

        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = redact(key, value)

    _LOADED = True
    return applied


def redact(key: str, value: str) -> str:
    """A value safe to print.

    Anything that looks like a credential becomes a length and a four-character tail --
    enough to tell two keys apart when debugging, not enough to be a leak if it lands in a
    log, a screenshot or a demo video.
    """
    sensitive = any(
        marker in key.upper() for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH")
    )
    if not sensitive:
        return value
    if len(value) <= 8:
        return "***"
    return f"***{value[-4:]} ({len(value)} chars)"


def require_model_key() -> str:
    """Return which model provider is configured, or raise with a useful message."""
    load_env()
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise RuntimeError(
        "No model key found. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY, either in the "
        "environment or in a .env file beside pyproject.toml. See .env.example."
    )
