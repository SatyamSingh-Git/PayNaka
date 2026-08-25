"""Create `.env` from the template, and never over an existing one.

The README used to say `cp .env.example .env`. A reader who already had keys in `.env`
followed that instruction and destroyed them — silently, because `cp` succeeds. There is no
warning, no prompt, and no way back unless the keys are written down somewhere else.

An instruction in a README is executed by people who have not read the rest of it. This one
refuses rather than overwrites, and says where the file already is.
"""

from __future__ import annotations

import pathlib
import shutil

TEMPLATE = pathlib.Path(".env.example")
TARGET = pathlib.Path(".env")


def main() -> int:
    if not TEMPLATE.exists():
        print(f"{TEMPLATE} is missing — nothing to copy from.")
        return 1

    if TARGET.exists():
        print(f"{TARGET} already exists. Leaving it exactly as it is.")
        print("To start over, delete it yourself and run this again — but read it first:")
        print("a populated .env holds credentials that exist nowhere else.")
        return 0

    shutil.copyfile(TEMPLATE, TARGET)
    print(f"wrote {TARGET} from {TEMPLATE}. Fill in the keys you need; every value is a")
    print("placeholder, and each one is documented in the file.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
