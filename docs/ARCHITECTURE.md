# Architecture

## The one idea

**The agent holds no payment credentials.**

It cannot call Razorpay. It can only *ask* PayNaka to, and PayNaka answers with
deterministic code that never consults a model. Injection can change what the agent
**wants**. It cannot change what the mandate **allows**.

Everything else in this repository is in service of that sentence.

```
  shopper intent ──freeze + sign──▶ IntentMandate ─────────────────┐
   (before any untrusted text)      Ed25519, expiring, single-use  │
                                                                   ▼
  poisoned catalog ──reads──▶  buyer agent  ──asks──▶  ┏━━━━━━━━━━━━━━━┓ ──▶ Razorpay
  reviews · seller copy       (no credentials)          ┃   PayNaka    ┃     (test mode)
  alt-text · descriptions                               ┃ holds the    ┃
                                                        ┃ keys         ┃
                                                        ┗━━━━━━━━━━━━━━┛
                                                               │
                                                               ▼
                                                    hash-chained audit
```

Nothing to the right of the agent runs on model judgement.

---

## Why the credential split, and not a better prompt

A prompt-based defence is a probabilistic barrier against an adversary with unlimited
attempts and no cost per attempt. It helps, and then it fails, and you cannot tell in
advance which case you are in.

The mandate is sealed **before** the agent reads anything an attacker controls. So the
adversary is not attacking a filter that might catch them; they are attacking a statement
that was already signed when their payload did not exist. The only way to widen it is to
forge an Ed25519 signature.

That is the difference between "we made it harder" and "the action is not available".

---

## Components

| Module | Job | Notable property |
| ------ | --- | ---------------- |
| `paynaka/money.py` | integer paise, nothing else | floats are refused, not rounded |
| `paynaka/clock.py` | injectable time | RBI/NPCI windows are testable at any hour |
| `paynaka/mandate.py` | signed statement of intent | canonical bytes, domain separation, strict parsing |
| `paynaka/state.py` | nonces, idempotency, ledger, counters | atomic claims, never read-then-write |
| `paynaka/policy.py` | the merchant's envelope | unknown key = startup failure |
| `paynaka/gate.py` | the checks | **imports no LLM SDK**, enforced by a test |
| `paynaka/audit.py` | append-only hash chain | `verify()` names the exact broken record |
| `paynaka/engine.py` | the only path to a rail | audits the decision *before* calling out |
| `paynaka/identity.py` | who may ask | no unauthenticated path, not even in development |
| `paynaka/mode.py` | enforce or observe | the mode is on every audit record, never inferred |
| `paynaka/state.py` escalations | step-up approvals | single-use, bound to the request hash, expires to DENY |
| `paynaka/metrics.py` | what to alarm on | derived from the chain on scrape, never a second tally |
| `paynaka/proxy/mcp.py` | Razorpay-compatible MCP server | fails closed without a mandate |
| `paynaka/rails/` | sim + Razorpay test mode | refuses any key that is not `rzp_test_` |

---

## Running more than one of these

"It uses SQLite, so it is single-node" is the easy thing to say about this code and it is
not what the code does. Every claim in `state.py` is a **single atomic statement** -- an
`INSERT` guarded by a `UNIQUE` constraint, an `UPDATE` guarded on the current state, an
upsert. The `SELECT` that follows only reads back what the statement already decided.
SQLite in WAL mode serialises writers across connections, so none of those guarantees
depend on being in one process.

That is measured rather than asserted. `tests/adversarial/test_multinode.py` builds two
independent `SqliteState` objects over one file -- two connections, two different
`threading.RLock` instances, so the in-process lock cannot be what saves them -- and races
24 callers at each claim:

| Claim | Across two nodes |
| --- | --- |
| `consume_nonce` | exactly one node wins; the other sees it spent |
| `claim_idempotency` | exactly one claim; every loser reads the winner's record and can replay it |
| `reserve_refund` | 24 concurrent refunds against a ₹1,000 balance: exactly 4 of ₹250 claimed, and no more |
| `consume_approval` | a step-up approval is spent once, whichever node the agent retries against |
| `decide_escalation` | two approvers racing on two nodes produce exactly one answer |
| `bump_denial` | counts sum across nodes, so the breaker does not need N× the denials on a fleet of N |
| `revoke` / `is_revoked` | withdrawing authority on one node is honoured by the other |

One asymmetry worth stating: `bump_denial`'s read-back can return a *higher* total if
another node incremented between the upsert and the read. That is the safe direction -- a
breaker that overshoots trips early, and one that undercounts fails to trip when it matters
most.

**Where it actually ends.** Two connections are what two *processes* have. They are not two
*hosts*. Nodes that cannot see each other's storage share no state, and every row above
evaporates: the same nonce is spendable on both, because there are now two checkpoints
rather than one. That is a deployment fact, not something the code can fix, and it is
asserted in the test file so it is never mistaken for a bug. `:memory:` -- which the demo
service uses -- is per-connection and therefore always two checkpoints.

**What a shared database would change.** The statements translate directly: SQLite's
`INSERT OR IGNORE`, `ON CONFLICT DO UPDATE` and guarded `UPDATE` all have PostgreSQL
equivalents with the same atomicity, and the schema is nine plain tables. The number that
moves is latency, and `make latency` already says which one: the envelope checks cost
**10 µs** and the full gate costs **1.0 ms**, so roughly 99% of a decision is the state
store. A network round trip per claim is the cost to plan for -- not the checking, which is
free.

---

## The decision pipeline

Checks run cheapest-and-most-certain first, and the first `DENY` short-circuits.

```
0  revocation          kill switch, one lookup
1  mandate expiry      signed once, valid for at most 24h
2  structure           well-formed, internally consistent, under the ceiling
3  authority           action ∈ mandate.allowed_actions  AND  enabled by policy
4  envelope            currency · items ⊆ intent · quantity · total · destination
5  refund lifecycle    refund ≤ still-refundable · return event on record
6  daily cap           per-IST-day ceiling
7  regulation          NPCI retries · debit blackout · RBI contact window · AFA
8  idempotency         fresh key, replay, or key-reuse denial
9  policy step-up      the only check that may return something other than DENY
```

Three invariants:

**Deny wins.** Policy may escalate an `ALLOW` to a `STEP_UP`. Nothing can turn a `DENY`
into an `ALLOW`, so a misconfigured policy can only close a hole, never open one. Tested
with a deliberately wide-open policy that still cannot widen a mandate.

**Every check is a pure function** of `(request, mandate, state, policy, clock)`. Nothing
reads the wall clock, the environment, or a global.

**A check that raises denies.** A crashed check is an unenforced check, and continuing
past one is how gates get bypassed.

---

## Ordering decisions that are load-bearing

**The audit record is written before the rail is called.** If the process dies mid-call,
the *intent* to move money is already on record and reconciliation has something to find.
The reverse order loses exactly the events you would most want.

**Denials are audited too.** A trail that only records what happened is a receipt book.
After an incident the interesting question is what was attempted and refused.

**A timeout is not a failure.** Declines and timeouts are separate exception types,
recorded as `rail.declined` and `rail.indeterminate`. A decline is final; a timeout may
have succeeded, and calling it a failure is what invites an unsafe retry. The idempotency
key is what settles it.

**The ledger records what the rail confirmed**, never what was requested. Those differ on
a partial capture, and taking the request's word for it is how a ledger drifts.

**Idempotency replays rather than denies.** A duplicate webhook is not an attack, it is
Tuesday. An identical retry returns the original result; the same key carrying a different
body is a substitution attack and is denied.

---

## Money is integer paise. Everywhere.

No floats, no rupees-as-decimal, no `Decimal` round-tripping through JSON. Money crosses
every boundary — mandate, gate, ledger, fixtures, console — as `int` paise. Formatting to
`₹` happens once, at the edge.

`money.py` refuses a float outright rather than rounding it, because a float reaching a
money path means the bug is upstream and rounding would hide it.

---

## Trust labelling in the catalog

Track 01 asks for an agent-readable catalog. Serialising products to JSON is the easy
half. Every text field in this one carries where it came from:

```json
{
  "sku": "ATTA-5KG",
  "price_paise": 199900,
  "fields": {
    "title":       { "value": "…", "trust": "merchant" },
    "description": { "value": "…", "trust": "merchant" },
    "seller_note": { "value": "…", "trust": "seller" }
  },
  "reviews": [{ "body": { "value": "…", "trust": "user_generated" } }]
}
```

The label comes from the field the text landed in, never from the text, so a payload
spelling out `{"trust": "merchant"}` still arrives labelled `user_generated`.

Prices are integers and are deliberately *not* trust-wrapped. A price served as a string
an agent must parse is exactly where currency confusion gets its opening.

Search covers merchant-controlled fields only. Controlling which products an agent even
sees is cheaper than injection and would beat any downstream defence.

---

## The MCP proxy

```diff
  {
    "mcpServers": {
      "razorpay": {
-       "url": "https://mcp.razorpay.com/mcp"
+       "url": "http://127.0.0.1:8002/mcp"
      }
    }
  }
```

Same tool names, same argument shapes, same result shapes.

Read paths pass through and are audited. Write paths are gated. A session that never
presented a mandate can read and can do nothing else — the absence of an authorisation is
not permission to skip the check.

`create_refund` is offered here. Razorpay's own remote server disables it: the right
instinct, with no mechanism to manage it. Behind a return precondition, a per-payment
bound, a daily cap and a step-up band, it becomes safe to offer.

---

## Two rails, and no third

`SimRail` is a deterministic in-process gateway, not a mock. It reproduces idempotency,
bounded captures, bounded refunds, scheduled declines and timeouts, and at-least-once
webhook delivery — identically on every run given the same seed. Several thousand
benchmark runs must be reproducible and must not depend on a sandbox being up.

It is a *faithful* adversary: it refuses double captures and over-refunds independently
of the gate, so a gate bug cannot become a real over-refund even in a demo.

`RazorpayRail` refuses to construct against any key that is not `rzp_test_`. No override
flag, no environment variable. `build_rail("live")` raises. A project that can be pointed
at a live merchant account by editing one line is a liability.

---

## Running it

```
make dev            merchant :8001 · paynaka :8002 · console :5173
make demo-attack    the headline, in the terminal
make bench          HAAT, four defences → RESULTS.md   (needs an Anthropic key)
make audit-verify   recompute the hash chain
make check          ruff · mypy --strict · pytest · gitleaks
```

The console demo runs the scripted agent, so a reviewer with a clone and one command sees
the whole story with no keys, no network and no cost.
