# Security

How to report a problem, what this project protects, how credentials are handled, and how to
reproduce every security claim it makes.

## Reporting a vulnerability

This is a buildathon project, not a deployed service. There is no bug bounty and nothing
here handles real money: `RazorpayRail` refuses to construct against any key that is not
`rzp_test_`, and that refusal is a test rather than a convention.

If you find something anyway, open a GitHub issue. If it would be irresponsible to describe
publicly, say so in the issue without the detail and it can move somewhere private.

## What this project protects, and what it does not

The claim is narrow on purpose, and stated the same way everywhere:

> PayNaka does not stop an agent being persuaded. It stops a persuaded agent moving money
> outside its mandate.

[THREATMODEL.md](THREATMODEL.md) carries the full list of what is defended and what is not,
including the entries that will not be closed and why. Two that matter most for anybody
reading this file:

- **Prompt injection is not solved.** Nobody can promise a model will not be talked into
  something, and a system resting on that promise would rest on the one part of the stack
  offering no guarantees.
- **The sentinel generalises poorly, measured.** 92.1% recall on the corpus its rules were
  read from, **64.4%** on held-out families. It is layer two, `gate.py` does not import it,
  and a flag never blocks anything — which is exactly why a weak layer two is not a weak
  checkpoint.

## Credentials

Nothing in this repository should ever contain a real credential.

| Guard | Where |
|---|---|
| `.env` is never committed; `.env.example` documents the shape | `.gitignore` |
| `make secrets` scans the working tree **and full history** with gitleaks | `Makefile` |
| a pre-commit hook runs the same scan before anything lands | `.pre-commit-config.yaml` |
| CI fails on a detected secret | `.github/workflows/ci.yml` |
| live Razorpay keys are refused at construction, no override, no env escape | `paynaka/rails/razorpay_rail.py` |
| errors are scrubbed, so `rzp_live_…` cannot reach a log, an audit record or a projector | `paynaka/rails/razorpay_rail.py` |

`gitleaks` is not a dependency of the test suite. `make secrets` says so and continues if it
is missing rather than passing silently — a scan that reports success because the scanner is
absent is worse than no scan.

### Development credentials

Four credentials are *minted* rather than configured when nothing is set and the rail is
the in-process simulator. They land in `var/`, which is gitignored:

- `var/mandate_ed25519.key` — the mandate signing key
- `var/dev-agent-token` — what the buying agent presents to `/mcp`
- `var/dev-approver-token` — what an operator presents to approve a step-up
- `var/dev-shopper-token` — what a shopper presents to `/api/intent` to create authority

This is deliberately *not* a bypass. The check is always live; only the origin of the
credential changes. The tempting alternative — verification that switches off when nothing
is configured — is a bypass, and a bypass is what gets found. In front of a real rail
nothing is minted and the service refuses to start without explicit configuration.

### Three roles, and why the third one exists

**agent** spends within a mandate · **approver** answers a step-up · **shopper** creates
authority. All three sets are mutually disjoint, and a name **or a token** appearing in any
two is a startup failure. The dangerous configuration is not two entries with the same
label; it is one secret that opens two doors, which careful naming does not prevent.

The shopper set is the sharpest of the three and it was added late, after an audit asked the
best question anyone has asked about this project: **who is allowed to create the
constraint?** The answer was *the constrained agent*. `/api/intent` authenticated against
the agent registry, so a compromised buying agent needed no forged signature — it could ask
this service for a genuine one over a mandate of its own design, with the budget and SKU list
it chose, and the checkpoint downstream would verify it perfectly, because it was genuine.

The subject is taken from the credential rather than the request body: a shopper creates
authority over their own account and no other, and a body naming a different subject is a
403 rather than a silent rewrite. An agent token presented here gets **401, not 403** — a
403 would confirm the token is real and merely wrong for this route, which hands a prober
half the answer. Unset in front of a real rail, the shopper registry is empty and an empty
registry authenticates nobody, so an unconfigured production deployment issues no mandates
at all rather than issuing them to whoever asks.

## The trust boundary

- **The buying agent holds no payment credentials.** It cannot move money; it can only ask.
- **The asking surface authenticates.** Taking keys away from one caller buys nothing while
  anything able to open a socket is the agent.
- **The gate holds only a public key.** `paynaka/issuer.py` holds the private one, so a
  compromised checkpoint can refuse a mandate and cannot mint one.
- **Webhooks are verified before they are believed.** HMAC-SHA256 over the raw body, never a
  re-serialised parse — that mistake fails *open*. No secret configured means nothing is
  accepted.
- **`paynaka/gate.py` imports no LLM SDK.** A test parses its AST and fails the build if
  that ever changes.

## Running it safely

- Test mode only. `PAYNAKA_RAIL=sim` is the default; reaching a network is an explicit choice.
- The attack corpus ships as static fixtures aimed at the bundled fictitious merchant. It is
  not a generator and cannot be pointed at a live target.
- `merchant/` exposes a `/_test/poison` endpoint that exists **only** when
  `PAYNAKA_ENV=sandbox`.
- The Docker image runs as a non-root user with `PAYNAKA_RAIL` pinned to `sim` and no
  credential baked in.

## Reproducing the security claims

Every one of these runs offline, with no keys and no model:

```bash
make test-adv        # the adversarial suite alone — the hostile half of every module
make audit-verify    # one intact chain and one with a denial rewritten as an approval
make secrets         # gitleaks over the tree and the full history
make modelfree       # the four defences over the attacks that actually land
```
