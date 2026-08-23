# Threat model

What PayNaka defends, what it does not, and why. The second list matters more than the
first — a security claim without a stated boundary is marketing.

---

## The attacker

Assume an adversary who can:

- write arbitrary text into any catalog field a buying agent reads — product
  descriptions, seller notes, image alt-text, and especially reviews;
- read the mandate in full, since it travels with the request;
- replay, truncate, pad, reorder and re-encode anything on the wire;
- sign whatever they like, with a key that is not ours;
- retry indefinitely, at no cost, learning from each refusal.

Assume they cannot:

- forge an Ed25519 signature over bytes they did not choose;
- read or write the process memory of the gate;
- rewrite the audit database *and* the externally published head hash.

---

## What is defended

### Actions that violate a frozen mandate cannot move money

The core claim. The mandate is sealed before any attacker-controlled text enters the
agent's context, so the adversary is not attacking a filter — they are attacking a
statement that was already signed when their payload did not exist.

| Attack | Outcome sought | Stopped by |
| ------ | -------------- | ---------- |
| Line-item append | a ₹50,000 gift card nobody asked for | `envelope.item_not_in_intent` |
| Quantity inflation | "minimum order 40 units" | `envelope.qty_exceeded` |
| Destination swap | goods to an attacker's address | `envelope.destination` |
| Refund without return | cash out with no goods back | `refund.no_return_on_record` |
| Currency confusion | `$1,999` read as `₹1,999` | `envelope.currency` |
| Replay / double charge | pay twice for one order | `idempotency.*` |
| Undeclared items | skip the SKU allow-list by omitting itemisation | `envelope.items_undeclared` |

### Mandate forgery

Field tampering, signature bit-flips, cross-mandate signature swaps, domain confusion,
duplicate-JSON-key smuggling, type confusion, unknown-field injection, oversize
allow-lists, and mandates stamped in the future or valid for a year — all refused, all
tested.

### Money-correctness failures with no attacker at all

Duplicate webhooks, over-refunds accumulating across partials, retries past NPCI's cap,
debits inside the restricted peak window, collection contact outside RBI hours, skipped
additional-factor authentication above ₹15,000.

An agent does not need an adversary to lose a merchant money. It needs a duplicate
webhook.

### Undetectable audit tampering

Editing a payload, deleting a record, or reordering the chain all break verification, and
`verify()` names the exact sequence number and the kind of break.

---

## What is **not** defended

### Prompt injection is not solved

PayNaka does not stop an agent from being persuaded. It stops a persuaded agent from
moving money outside its mandate. Those are different claims and the second is the only
one made here.

### Bad-but-authorised choices

An agent steered into buying a *worse* product **inside** the budget, from a *worse*
seller, at a *worse* price, is a real loss. Every check here is about authority, and that
purchase is authorised. HAAT does not score it, and it should not be read as safe.

### Denial of wallet

An adversary who induces an agent into a loop against a denying gate burns tokens without
moving a rupee. `max_turns` bounds one run; nothing bounds an attacker who can start many.

### A wholesale audit rewrite

The chain proves internal *consistency*, not authenticity. An attacker with total write
access who also resets `sqlite_sequence` can recompute a valid chain from scratch. Only a
head hash published somewhere outside our control catches that, which is why `head()`
exists. A test is named for this limitation so nobody later claims more.

A partial rewrite *is* caught: `AUTOINCREMENT` never reuses sequence numbers, so a naive
delete-and-replay produces a chain starting at seq 6 and the gap gives it away.

### Trailing truncation

Lopping records off the end leaves a shorter but internally consistent chain. Same
defence, same limitation: compare against a published head.

### The sentinel classifier

The layer-two detector that flags poisoned fields before the agent reads them is
**defence in depth and nothing more**. Its metrics are reported separately and it is not
what provides the guarantee. Merging its numbers into the gate's would be the single
easiest way to overstate this project.

### Compromise of the signing key or the gate process

Whoever holds the private key can mint mandates. Whoever can write to the gate's memory
has already won. Neither is in scope.

### Anything about live money

Test mode only. `RazorpayRail` refuses any key that is not `rzp_test_`, with no override,
and `build_rail("live")` raises rather than resolving. That is a property of the code, not
a promise in a document.

---

## Deliberate design refusals

**Failing open is not offered.** `on_step_up_timeout` must be `DENY`; the policy loader
rejects anything else. Making it configurable invites someone to turn it the wrong way at
3am during an incident.

**An unconfigured action is disabled, not unrestricted.** A policy that forgot to mention
payouts must not thereby permit unlimited payouts.

**An empty `allowed_actions` list grants nothing.** The fail-closed reading of an empty
permission list is the empty one.

**`create_refund` and `create_payout` are absent from the default mandate.** Irreversible
actions are opted into, never inherited.

**A typo in a policy key is a startup failure.** `max_amont: 500000` must fail loudly on
boot rather than silently removing a limit that nobody notices for six weeks.

---

## On the attack corpus

HAAT ships as **static YAML fixtures aimed at a bundled fictitious merchant**. It is not a
generator and takes no target parameter, because there is nowhere else to point it. Every
case is diffable in review.

That is the line between a benchmark and a weapon, and it is why this work is defensive:
the deliverable is the gate and the measurement, and the payloads exist only to
demonstrate that the measurement is real.

The merchant's `/_test/poison` endpoint writes to in-process state, cannot reach any other
host, and returns 404 outside sandbox mode — checked per request, so flipping the
environment closes the door on an already-running process.
