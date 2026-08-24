# PayNaka, in one command, on a machine with nothing installed.
#
# `make dev` needs Python 3.12, uv and Node. That is three things to get right before
# anybody sees a number, and the number is the point. This image needs Docker.
#
#     docker build -t paynaka .
#     docker run --rm paynaka                 # the whole argument, ~90 seconds
#     docker run --rm paynaka make check      # or the full suite
#     docker run --rm -p 8002:8002 paynaka make naka
#
# No keys, no network, no model at runtime: every demonstration here runs against the
# in-process simulator. The image is built without any credential and refuses to acquire
# one -- `PAYNAKA_RAIL` stays `sim` unless somebody deliberately overrides it, and pointing
# at a real rail additionally requires an explicitly configured agent credential.

FROM python:3.12-slim-bookworm

# git, because the sealed-corpus guard asks whether the v1.0-freeze tag exists and the
# honest answer from inside a container without git is "no", which would silently look
# like discipline rather than a missing tool. make, because the entry points are targets.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git make \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so editing source does not re-resolve the world on every build.
COPY pyproject.toml uv.lock ./
RUN uv sync --all-extras --frozen --no-install-project

COPY . .
RUN uv sync --all-extras --frozen

# The demo is deterministic and offline. Anything that would reach a network is opt-in.
ENV PAYNAKA_RAIL=sim \
    PAYNAKA_MODE=enforce \
    PYTHONUNBUFFERED=1

# A non-root user, because a container that runs a payment checkpoint as root is an odd
# thing to find in a project about bounded authority. `var/` is writable: the dev signing
# key and the dev credentials are minted there on first run.
RUN useradd --create-home --uid 10001 paynaka \
 && mkdir -p var haat/out \
 && chown -R paynaka:paynaka /app
USER paynaka

# One command, the whole story.
CMD ["make", "demo"]
