"""Hand the console a credential it can approve step-ups with.

The console is an operator surface and approving is a privileged action, so it needs the
*approver* credential -- which is deliberately not the agent's. A step-up the buying agent
can answer on its own behalf is theatre, and the service refuses to start if one token
appears in both sets.

This writes the development credential into ``console/.env.local``, where Vite picks it up
as ``VITE_PAYNAKA_APPROVER_TOKEN``. Through the build rather than over HTTP: an endpoint
that hands a credential to whoever asks for it is not a credential.

Without this, the approve button gets an honest 401 -- which is the correct outcome, and
better than a button that silently appears to work.
"""

from __future__ import annotations

import pathlib

from paynaka.identity import DEV_APPROVER_PATH, load_or_create_dev_token

TARGET = pathlib.Path("console/.env.local")


def main() -> int:
    token = load_or_create_dev_token(DEV_APPROVER_PATH)
    TARGET.write_text(f"VITE_PAYNAKA_APPROVER_TOKEN={token}\n", encoding="utf-8")
    print(f"dev approver credential written to {TARGET}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
