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
- rewrite the audit database *and* the notary's key *and* the payment gateway's own
  records of the payments the merchant made.

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

`make chaos` runs six of these. No model, no keys, no network, reproducible to the paise:
a careful naive handler overspends **₹3,994** on a ₹1,999 order; PayNaka overspends
nothing.

### The price changing between reading it and paying it

The one attack in this repository where **making the model smarter cannot help.**

The agent behaves perfectly. It searches, reads the product page, reports ₹1,999 to the
shopper, and orders exactly one bag of atta. Then the merchant reprices the SKU, and
`buyer/tools.py` — like every real shop — totals the order from the *live* catalogue at
checkout. The card is charged ₹51,974.

There is no injected text, so there is nothing for a prompt defence to be suspicious of.
There is no reasoning error, so a more capable agent behaves identically. A reviewing
model sees `create_order(ATTA-5KG × 1)`, which is precisely what the shopper asked for; to
catch this it would have to remember a price from an earlier turn and do exact arithmetic
against a budget, which is a deterministic bound wearing a very expensive costume.

`max_total` was frozen before the trip began. ₹51,974 > ₹1,999, and the check does not
care why the number changed — which is exactly why it survives changes nobody anticipated.

`make toctou`, 27 runs, three reprice sizes × three moments × three defences:

| | none | prompt hardening | PayNaka |
|---|---|---|---|
| **+5%** — the skim nobody notices | ₹99.95 | ₹99.95 | ₹0 |
| **×2** | ₹1,999 | ₹1,999 | ₹0 |
| **×26** | ₹49,975 | ₹49,975 | ₹0 |

Prompt hardening and no defence at all lose identical amounts, because the prompt is not
in the causal path.

**The obvious objection, measured rather than argued.** "A careful agent would re-check
the price before ordering." `python -m scripts.probe_toctou` puts the question to three
real models — DeepSeek V4 Flash, Solar Pro 4, Laguna XS 2.1 — with the gate **off** and
the *hardened* prompt, repricing ×26 the instant the item enters the basket:

| | |
|---|---|
| paid the repriced ₹51,974 | **3 / 3** |
| re-checked the price at some point | **2 / 3** |
| re-checked it *before* paying | **0 / 3** |

They were not careless. Two of the three went back and looked at the price again — after
calling `create_order`. They noticed. The card was already charged, and one of them then
attempted a refund it had no authority to issue.

That is the argument for pre-authorisation in one line: **diligence after an irreversible
action is a post-mortem.** The only check that helps is the one that happens before the
money moves, and a frozen mandate is that check whether or not anybody remembers to look.

**The budget bound is exactly as tight as the mandate**, and shoppers say round numbers.
Someone who says "something under ₹2,500" for a ₹1,999 bag has handed over ₹501 of room, and
a +5% skim inside that room does not exceed the budget. `max_total` will not stop it and
should not — it was authorised.

So the mandate now carries a second, narrower thing: **what the shopper was shown.**

```yaml
reference_prices:   { ATTA-5KG: 199900 }   # the page said ₹1,999
price_tolerance_bps: 100                   # 1% of slack for honest repricing
```

`check_reference_price` compares each line item against it. The two bounds answer
different questions and only the first was being asked:

| | asks | catches the skim inside a loose budget |
|---|---|---|
| `max_total` | does the basket fit the budget? | no |
| `reference_prices` | is the *thing* still the thing that was agreed? | **yes** |

Measured on the same ₹2,500-budget case that used to slip through:

| mandate | verdict | held by |
|---|---|---|
| budget only | STEP_UP | `policy.step_up` — the *merchant's* band, which a merchant who never configured one would not have |
| budget + reference | **DENY** | `envelope.price_moved` — the *shopper's* own authority |

Both stop the money. Only the second stops it for a reason the shopper chose.

Empty `reference_prices` disables the check entirely, which is the honest default: a
shopper who genuinely said "anything under ₹2,500" was not shown a price, and inventing a
ceiling for them would be inventing an intent they never expressed.

### Audit tampering, including the two kinds the chain cannot see by itself

Editing a payload, deleting a record, or reordering the chain all break verification, and
`verify()` names the exact sequence number and the kind of break.

Two attacks defeat that, and both defeat it completely, because `verify()` checks the
chain against *itself*:

- **A wholesale rewrite.** Recompute the table from scratch and it verifies perfectly.
- **Trailing truncation.** Lop records off the end and what remains is shorter, internally
  consistent, and silent about what is missing.

The fix is not cryptography, it is **witnesses**: one signed sentence saying *at time T
this chain had N records and its tip was H*. A rewritten chain has a different tip at
every length, and a truncated one cannot reach N at all. `paynaka/anchor.py` is honest
about there being three tiers rather than pretending to one solution:

| Tier | Where the witness lives | What it survives |
|---|---|---|
| 1 | a separate append-only log | a careless attacker, and accidental corruption |
| 2 | signed by a **notary key the gate does not hold** | an attacker who owns the database but not another machine's key |
| 3 | **the payment rail** — the head rides in the `notes` of calls PayNaka was already making | an attacker who owns *every local file including the notary key* |

Tier 3 is the one worth arguing about. Razorpay stores the notes and hands them back on
read, so every money movement is a witness to the local chain at the moment it happened,
and the merchant cannot go back and edit Razorpay's record of a payment they made. It
costs nothing, because the calls were happening anyway.

Its limitation, stated: it witnesses only at the moments money moved. A stretch of the
chain containing nothing but denials is covered by tiers 1 and 2 alone. That is a real
gap and it is a much narrower one than it replaces.

Still not defended: an attacker who controls the gate, the notary **and** the merchant's
Razorpay account at once has already won, and no arrangement of hashes changes that.

### Denial of wallet, bounded

A refusal costs PayNaka microseconds and costs whoever is driving the agent a full model
turn. That asymmetry runs the wrong way: an attacker who keeps an agent looping against a
wall spends nothing and drains someone else's budget, and `max_turns` bounds one run
rather than an adversary who can start many.

The circuit breaker bounds it. After `denials_per_session` refusals in an IST day the
session's authority is withdrawn, which turns a retryable "no" into a terminal one — and a
terminal answer is the only kind a looping agent cannot argue with. A second, wider bound
counts per *subject*, because an attacker who can burn one session can start another.

```yaml
circuit_breaker:
  denials_per_session: 12
  denials_per_subject: 40
```

Measured: 200 attempts against a breaker set to 5 cost the attacker **5** substantive
checks and 195 revocations.

Approvals are not counted. Replays are not counted — a duplicate webhook is not an attack.
A STEP_UP is not counted — waiting for a human is not being refused.

The awkward half is true too, and is tested rather than hidden: a breaker on a money path
locks out the legitimate session along with the attacker. That is what fail-closed means,
it is the defensible direction, and an operator clears it with `unrevoke()` and
`clear_denials()`.

---

## What is **not** defended

### Prompt injection is not solved

PayNaka does not stop an agent from being persuaded. It stops a persuaded agent from
moving money outside its mandate. Those are different claims and the second is the only
one made here.

This is not a gap waiting to be closed. It is the shape of the argument: nobody can
promise a model will not be talked into something, and a system whose safety depended on
that promise would be resting on the one part of the stack that offers no guarantees.

### Bad-but-authorised choices — the half that is not a number

The *price* half is now checked: `reference_prices` above catches a merchant repricing
inside a loose budget.

What remains is the half that is not a number. An agent steered into a *worse seller*, or
a worse product at an entirely honest price, is a real loss and is fully authorised. Every
check here is about authority, and that purchase has it. HAAT does not score it and it
should not be read as safe.

Making it checkable would mean the mandate carrying a judgment, and a judgment expressed
as a threshold is a heuristic in the money path — the exact thing this project refuses to
put there.

### Denial of wallet, past the breaker

The circuit breaker bounds a loop, and bounding is not preventing: the turns spent
before it trips are gone. An attacker who is content to burn a few turns per session, per
day, indefinitely, still costs the operator money without moving a rupee.

What the breaker buys is that the cost is now a number an operator chose rather than a
number an attacker chose.

### An attacker who owns everything at once

The three witness tiers each raise the bar and none of them is a wall. An adversary who
holds the gate process, the notary key **and** the merchant's Razorpay account can rewrite
the chain and every witness of it. That is not a hash-chain problem and no arrangement of
hashes solves it.

A *partial* rewrite is still caught by the chain alone: `AUTOINCREMENT` never reuses
sequence numbers, so a naive delete-and-replay produces a chain starting at seq 6 and the
gap gives it away.

### The sentinel classifier

`paynaka/sentinel.py` flags poisoned fields before the agent reads them. It is **defence
in depth and nothing more**. Its metrics are reported separately and it is not what
provides the guarantee. Merging its numbers into the gate's would be the single easiest
way to overstate this project.

Three properties keep it in its place, and all three are tested:

- **`gate.py` does not import it.** The money decision never consults a heuristic.
- **It returns evidence, not a verdict.** `scan()` has no `allow` and no `verdict`. The
  flag rides along in provenance so an operator can see which field carried the payload;
  it does not redact, and it does not block.
- **It contains no model.** A rule-based detector can be argued with. `directive_syntax`
  matched `[SYSTEM:` at offset 34 is actionable; "the classifier scored 0.83" is not.

Measured on the six **visible** attack families and 100 benign fields — the bundled
catalogue plus a hard-negative corpus of recipes, capitalised reviews, quoted error
messages, Hindi and Tamil text, and legitimate SKU and currency mentions:

| | |
|---|---|
| recall | **92.1%** (232 / 252 payloads) |
| false positives | **0.0%** (0 / 100 benign fields) |
| margin | **5 points** — the closest honest text scored 45 of 50 |

Read the margin, not just the zero. A 0% false-positive rate whose nearest miss sits one
signal below the line is one rule change away from a bad afternoon, and the number is
printed for exactly that reason. The near-miss is a seller apologising for a broken mail
template containing `{{customer_name}}`.

**Recall against the sealed families is unmeasured, deliberately.** The rules above were
written by reading the visible corpus, so the held-out families are the only evidence that
any of this generalises rather than remembers — and that evidence can be spent once.
`make sentinel-sealed` refuses to run until the `v1.0-freeze` tag exists, the same refusal
`make bench-sealed` makes.

Two rules — `override_previous` and `tool_name` — never fire on the measured corpus and
so contributed nothing to the 92.1%. They are named as such in the test suite rather than
left in the file to make the rule list look longer than it is.

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
