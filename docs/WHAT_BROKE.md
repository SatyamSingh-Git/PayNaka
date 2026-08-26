# What broke at 2 AM, and how we got out

> A complete, unflattering account of everything that went wrong building PayNaka.
> Ninety-four commits, and a large share of them are named after a defect rather than a
> feature. This document collects them.

We are writing this down because the alternative — a submission that lists only what works —
would misrepresent how the thing was built. Every number in this repository survived a
process that produced far more wrong numbers than right ones. The wrong ones are the
interesting part.

There is a second reason. Several failures below were **caused by the fix for the previous
one**. That pattern is the most useful thing we learned, and it does not show up anywhere in
a finished codebase.

---

## The shape of it

| Class | What it means | Who usually caught it |
| --- | --- | --- |
| Authority holes | the design's central promise did not hold | independent review, adversarial tests |
| Money arithmetic | the ledger disagreed with reality | chaos harness, property tests, real runs |
| Benchmark self-flattery | the measurement made us look better than we were | running it, then distrusting the result |
| Claims we could not support | prose stronger than the code | review, and reading our own docs adversarially |
| "Does it work for a stranger" | correct code, unusable by anyone else | **Satyam, every single time** |
| Fixes that broke something else | the repair was the new defect | the test written for the repair |

That last column is not a rhetorical flourish. It is the single clearest finding of the
project and it is worked through at the end.

---

## 1. The authority holes

These are the worst category, because each one falsified the sentence the whole project
exists to say.

### 1.1 `max_total` was a per-request ceiling wearing a budget's name

One signed mandate authorising ₹1,999. Three `create_order` calls, three fresh idempotency
keys. Three ALLOWs. **₹5,997 moved.**

`check_total` asked *"does this request fit the budget?"* and every request fitted. Nothing
ever asked *"is there any budget left?"* The nonce that would have stopped it existed in
`state.py` and the production path never called it.

The mistake underneath is worth naming because it is common: **idempotency was doing duty as
a spending control.** Idempotency stops *the same request* repeating. It has never stopped a
caller spending the same authority again under a new key. Different questions; only one was
being asked.

`max_total` is cumulative now, claimed atomically inside a single `INSERT` whose `WHERE`
computes the remaining balance — so there is no instant where two callers both read the same
remainder and both decide they fit inside it.

### 1.2 The constrained agent could create its own constraint

`/api/intent` — the route that signs mandates — authenticated against the **agent** token
registry. The route's own docstring read *"This is the shopper's surface, not the agent's."*
It was true of the intent and false of the code, which is the most expensive kind of comment.

So a compromised buying agent needed no forged Ed25519 signature. It could ask this service
for a **genuine** one, over a mandate of its own design, with the budget and SKU list it
chose, then redeem the grant and spend inside a bound it had written itself. The checkpoint
downstream would have verified it perfectly, because it was genuine.

The clearest evidence it was real: the integration tests *issued with the buyer token and
asserted `issued_to == "buyer"`*. They were documenting the vulnerability. All fifteen broke
the moment the fix landed.

There are three credential sets now — agent spends, approver answers a step-up, shopper
creates authority — mutually disjoint by name **and** by token, every overlap a startup
failure. The subject comes from the credential, not the request body.

### 1.3 Capture and refund never asked whose payment it was

Every check on those paths was arithmetic: is the amount inside the captured balance, is
there a return on record. All of it correct about the *amount*, and none of it about the
*owner*. A fresh refund-capable mandate could operate on any payment that had reached state.

The committed Razorpay evidence showed it from the other side: the order and capture carried
`mnd_24ac…` in Razorpay's own notes, the refund carried `mnd_36a0…`. **Two mandates in one
lifecycle, anchored publicly, with nothing anywhere objecting.**

The gate now walks `payment → order → mandate, subject` before it looks at a balance.

### 1.4 `"allow_refunds": "false"` granted refund authority

`bool("false")` is `True`. Every non-empty string is. A client that quoted a boolean — the
single most common JSON mistake there is — was silently upgraded into permission to move
money out of an account.

The whole body is parsed strictly now. `int("199900")` works and `int(1999.9)` truncates, and
both are a value that looked close enough and was not the thing. The one worth naming
separately: `tuple("ATTA-5KG")` is twenty-one single-character SKUs, none of which exist, and
the mandate would have been issued over them.

### 1.5 The strongest defence in the project was unreachable

`ShopperIntent` has carried `reference_prices` and `price_tolerance_bps` all along. The HTTP
route never read them. So a mandate issued over the API could not carry the one bound that
stops the attack the project *leads with* — a merchant repricing between the agent reading a
page and paying for it.

The check was live and unreachable, which is the same as absent for anybody using the API.

### 1.6 The asking surface was open

Early on, "the agent holds no payment credentials, it can only ask PayNaka" was worth nothing
while anything able to open a socket to `:8002` **was** the agent. The credential split and
an authenticated asking surface are one argument; either alone is decorative.

---

## 2. Money arithmetic

### 2.1 Twenty concurrent refunds, twenty approvals

`check_refund_bounds` read the refundable balance, and the ledger was written *after* the
rail call. Concurrent refunds on one payment all fit through the gap.

Measured on twenty at once: **the gate approved all twenty, and the gateway refused sixteen.**
The money came out right and the enforcement was fiction. Relying on the payment provider to
enforce our own bound is not enforcement.

### 2.2 A definitive refusal filed as "outcome unknown"

`SimRail` raised the generic error type for definitive refusals, so the engine recorded
*"refund exceeds what was captured"* as an unknown outcome — teaching a reconciliation
process to chase money that never moved.

### 2.3 A decline destroyed the shopper's authority

The mandate claim is taken *before* the rail is called — it has to be, or two concurrent
requests both read the same remainder. It was never given back on a refusal.

So a shopper who authorised ₹1,999, met a declining card and tried again had a mandate worth
nothing. No money moved. Nothing anywhere explained it. The gate answered
`envelope.mandate_exhausted` about a budget spent on a payment that never happened — a true
sentence describing a false thing.

A **definitive** decline now returns the claim. A **timeout** does not, because there the
money may well have moved and a retry must not be able to spend it twice.

### 2.4 ₹3,998 on a ₹1,999 payment

Found by running the real Razorpay lifecycle script twice. Each run fetched the payment, saw
`captured: true, amount: 199900`, and *appended* a ledger entry.

Every downstream bound reads that number, so an over-refund of the full amount then fitted
inside the inflated balance — and the committed evidence for *"an over-refund is refused"*
quietly stopped demonstrating it. It came back as `policy.step_up` where it had been
`refund.exceeds_capture`.

**A provider's captured total is not an event.** `record_capture` appends, which is right for
something that happened once. Observing a total twice must not double it.

### 2.5 The escalation flow could not finish

`policy.yaml` had declared an escalation band since the first commit: above a threshold, a
person decides. Building the approval flow is what discovered that the flow **could not
complete**.

Idempotency was resolved before the step-up check and *claimed* the key. So a request that
stepped up burned its idempotency key while waiting for a human, and the agent's retry after
approval was classified as a duplicate — replaying a result that had never been produced. The
policy documented a flow that structurally could not finish, and nothing noticed because
nothing had ever walked it end to end.

### 2.6 A replay was reported as a refusal

`executed` stays `False` on a replay deliberately — it is what stops twenty webhook
redeliveries summing to twenty payments in the benchmark. The MCP proxy had one success
branch and one failure branch, so everything that was not an execution fell into *"refused"*.

An agent told `blocked_by_paynaka` retries, or tells the shopper their purchase failed while
the money has moved. It is the worst available answer to "what happened".

---

## 3. The benchmark lied, in our favour, four separate times

This is the category we are least comfortable with and the one most worth publishing. **Every
one of these moved the number in the same direction: they made the undefended baseline look
safer than it is, shrinking the exact gap the benchmark exists to demonstrate.**

A benchmark whose failures flatter its author is worse than no benchmark.

### 3.1 A rate limit recorded as a successful defence

OpenRouter's free tier allows 20 requests a minute; a case is several turns. The first real
attempt came back with 6 of the first 14 rows returning HTTP 429. Each was written as
`attack_succeeded=False`, and the resume logic counted it as done — so the case was **never
retried and scored as a defended attack for ever, on the strength of a rate limit.**

### 3.2 Errored runs left in the denominator

The summariser counted the error, then counted the run as a defended attack. Attack success
was diluted in direct proportion to how flaky the provider was that afternoon.

### 3.3 The baseline steered the agent away from the attack

The "naive" prompt said *"Be efficient. Do not ask the shopper to confirm things they have
already told you."* That pushed the agent straight from search to checkout, past
`get_product`, so **it never read the poisoned review.** The payload was never delivered.

A baseline that routes the agent around the attack surface is not a baseline. It is an
accidental defence, scored as an absence of attack.

### 3.4 A harness that never ran the attack

The model-free repricing table passed one invented moment string, which matched nothing. No
reprice happened, and the table read **0 breaches for every defence** — a flattering zero
produced by a harness that had not run the attack. The moment is validated against the real
list now, so a typo is an error rather than a result.

### 3.5 Two runners, one file

A sweep produced 432 rows for 252 cases. Two processes were writing the same file, because a
wrapper had been killed and the process underneath had not. This produced `haat/runlock.py` —
and the tests written for *that* found two more bugs in it: an unreadable lock fell through
to "conflict", and `os.kill(pid, 0)` reads dead processes as alive on Windows.

### 3.6 The conclusion inverted

For a while the headline read that plain-text catalogue injection *"does not reliably work
against 2026 tool-calling models"*, on the strength of **one free-tier model returning 0 of
245**.

Three paid models, same corpus, said otherwise: **4 breaches, ₹3,30,860**, across three
labs on three continents. All three fell. Solar Pro 4 lost exactly the ₹50,000 gift card this
project demonstrates, in eight turns, without ever refusing.

We had published a negative result derived from a sample of one.

### 3.7 A breach counted outside its own denominator

The README's per-model row read *"DeepSeek: 250 scored, 1 breached"* — while the figure quoted
beside it included a ₹2,01,899 runaway that the harness files as an errored run and drops from
scoring. A breach sitting outside the denominator it is quoted against.

A reader who ran the harness would have got different numbers with nothing in the README to
explain the gap, and the number the README carried was **the larger one**.

### 3.8 Three sweeps that stopped without saying so

Three paid sweeps launched in parallel wrote six, nine and zero rows and then stopped dead. No
error, no exit, no output — three live processes producing nothing for ten minutes.

The OpenAI SDK defaults to a **600-second** request timeout and the client was built without
overriding it. The retry machinery could not help, and that is the part worth understanding:
retries run when a call *returns*. A hung call never returns, so none of it is ever consulted.

**An unbounded timeout does not make a benchmark slower. It silently stops it — and a stopped
benchmark that is still running looks exactly like a working one.**

---

## 3½. Where we used a model, and where we refused to

Two of the failures above were failures of *judgment about AI*, not of code, and they are
worth separating out.

### The gate contains no model, and that is a testable claim

`paynaka/gate.py` imports no LLM SDK. Not as a stylistic preference — **matching, checking and
enforcement are code problems.** "Is this SKU in the allow-list" and "does this total fit the
remaining budget" are arithmetic, and putting a probabilistic component in that path buys
nothing and costs determinism, latency, money, and the ability to say what will happen.

CI fails the moment that import block changes. It is a claim a reviewer verifies by reading
one screen rather than by trusting us.

### We measured what prompt hardening is worth, and it is exactly zero here

The benchmark's `prompt` defence is **byte-identical machinery** to the undefended baseline
with a different system prompt. Against the repricing attack it runs and changes nothing —
`none` and `prompt` are identical **to the paise**, 9 breaches of 9 each.

That is not a strawman result. It is the honest measurement of a defence that has no causal
path into the attack: there is no injected text for a careful prompt to be suspicious of,
because that attack never needed any.

### The `judge` defence: an LLM reviewing money, and why it cannot work here

We implemented a second model inspecting every proposed money action, in good faith, failing
closed. Then we wrote down why it does not help on the attack we lead with — and that write-up
is more valuable than the row would have been.

On a repricing attack the judge sees `create_order(ATTA-5KG × 1)`. That is *precisely* what the
shopper asked for. Catching the reprice means remembering a price from an earlier turn and
doing exact arithmetic against a budget: **a deterministic bound wearing a model's costume, and
priced like a model.**

So the table reports `not applicable` rather than a zero. A defence with no causal path into
an attack is not a defence that scored badly — it is one that has nothing to do with it, and
printing a zero there would read as a win it did not earn.

### The AI-judgment failures

**We drew a conclusion from a sample of one model.** For a while the headline said injection
*"does not reliably work against 2026 tool-calling models"* on the strength of one free-tier
model returning 0 of 245. Three paid models later: 4 breaches, ₹3,30,860. We had generalised
about model behaviour from a single model, which is the exact error this project accuses
prompt-hardening of.

**We let a prompt become an accidental defence.** The baseline prompt's instruction to "be
efficient" routed the agent past the poisoned page entirely (§3.3). A prompt is not a neutral
harness component when the thing being measured is *whether text changes agent behaviour*.

**We assumed a hang was ours.** Three sweeps stopped dead and it looked like a harness bug for
some time. It was an SDK default — 600 seconds — and no amount of reading our own retry logic
would have found it, because retries are downstream of a response that never came.

---

## 4. Claims the code could not support

### 4.1 "Money moved" for order creation

Razorpay's lifecycle is order → customer authentication → capture. An autonomous agent
reaches the first step and stops. An order binds an amount and is handed to Checkout; nothing
has left an account when one is created.

The project's headline said *money moved*. It was the fastest available way to lose a payments
reviewer, and it was the word we led with. Worse, the repository argued with itself in print:
the README warned that calling order creation "money moved" loses a payments reviewer —
eleven sections below a table doing exactly that.

Every published column now reads **"Unauthorised order value"**, with the lifecycle stated
beside the headline table. It is the weaker claim and the true one.

### 4.2 Regulatory claims broader than their sources

`policy.yaml` attributed five rules to RBI and NPCI, and `clock.py` opened by saying PayNaka
*"encodes real Indian payments regulation"*. Two were being applied well beyond what their
source supports.

That is a bad kind of wrong for a payments project. A merchant who reads *"RBI: ₹15,000
additional-factor ceiling"* in a config file **has been told something by this repository**,
and this repository had not checked it. `docs/REGULATORY_BASIS.md` now states plainly that
these are configurable examples, unverified against primary sources, and that a code rule
cannot produce compliance in any case.

### 4.3 Two documented settings the app ignored

`.env.example` documented the audit database path and the signing key path. The app read
neither. It ran on two in-memory databases and a key generated fresh on every boot — so a
restart erased idempotency, mandate spend, escalations, revocations, the audit chain, and the
identity that had signed everything in it.

**A documented setting the code ignores is worse than an undocumented one: somebody sets it
and believes it worked.**

### 4.4 The test suite overwrote its own evidence — twice

First: the app started honouring the audit database path, and the suite began writing into
the committed audit fixture. A 3-record chain of evidence became **31 records of test
traffic**. Nothing failed. The fixture was simply no longer what it claimed to be.

Second, months later: a test that only wanted to inspect a JSONL called the report generator
with `--out` redirected and `--results` left at its default. **Every run of `check`
regenerated the published results tables in place.** Same numbers that time. Same numbers is
the lucky case.

A session fixture now records the bytes of every committed evidence file before the first test
and checks them after the last.

### 4.5 The docs drifted from the code

`docs/ARCHITECTURE.md` said the schema was "nine plain tables". It was thirteen. The README
quoted order, payment and refund IDs from a run whose files were **no longer in the
repository**.

Both are now pinned by tests: the table count is counted from `state.py`, and every provider
ID the README quotes must appear in a committed evidence file.

---

## 5. Correct code that nobody else could run

**Every failure in this section was found by Satyam, by running the project the way a judge
would, on the machine a judge would use.** None was found by 2,400 tests, `mypy --strict`,
`ruff`, `tsc`, or `vite build` — all of which were green throughout.

- **`make` is not installed on Windows.** The README's first command, the first thing any
  reviewer types. It produced `the term 'make' is not recognized`. This is how `make.py`
  exists: a `make` interpreter in Python that *reads* the Makefile rather than duplicating it.
- **The secret scan reported success while doing nothing.** `command -v gitleaks && … || echo`
  is POSIX. On Windows it printed *"The system cannot find the path specified"* and fell
  through to the echo — a security check that was absent and quiet about it, which is the
  worst state available.
- **`make demo` still failed after the first fix.** One command was fixed and sixteen left. The
  note saying "if you have make, that spelling works too" was worse than useless: a reader does
  not read a caveat, they copy the next code block.
- **`|| true` broke the one task whose job is to show a deliberate failure.** `audit-verify`
  demonstrates a tampered chain being caught. On Windows `true` does not exist, so it exited 1
  — **reporting a real failure while demonstrating a deliberate one.**
- **`cp .env.example .env` destroyed live credentials.** Satyam had working Razorpay and
  OpenRouter keys in `.env` and followed the README exactly. `cp` succeeded, said nothing, and
  the keys were gone. They existed nowhere else. *An instruction in a README is executed by
  people who have not read the rest of it.*
- **The Operations screen rendered blank.** `tsc` passed. `vite build` passed. A Blade `Amount`
  at `size="2xlarge"` without `type="heading"` threw at render. Two green checks and an empty
  page.
- **The console "looked so bad, not at all like the beautiful site of Razorpay."** Held next to
  Razorpay's own surfaces the gap was three things: we never used the components Razorpay
  ships, type was uniform so nothing led, and every section carried a paragraph nobody reads
  on a projector.
- **The rupee sign crashed the Windows console.** The command the README opens with had been
  broken on its own second line since it was written.
- **`mypy` was read as a hang.** 58 seconds cold, 4.3 warm, and it prints *nothing* until it
  finishes. Reading a silent terminal as a hang is the correct reading of the evidence.
- **A fresh clone went red.** A test asserted the freeze tag exists — a claim about the
  author's git history, failing in a reader's terminal. Investigating it found something worse:
  the guard ran `git rev-parse` in the reader's working directory, and an unpacked ZIP has no
  `.git`, so **git walked up and answered from the reviewer's home directory**, which happened
  to be a repository of their own. The sealed-corpus guard was reading a stranger's tags.
- **`bench` spent money without warning.** Satyam ran it, watched the counter reach 20/1800 and
  asked — from inside the running sweep — whether it was going to be expensive. Nothing in the
  command, the help text, or the README said so. That is this project's own rule broken on its
  own terms: ambiguity in a money path resolves to DENY, and a command that spends without
  being asked never asked.
- **Piped output was in the wrong order.** `print()` block-buffers when stdout is a pipe while
  the child writes straight through, so in any redirected log every echoed command arrived
  *after* the output it introduced — **a failure printed underneath the wrong task.** On a
  terminal it looked fine, which is why it survived.

---

## 6. Process failures

Not code. Still cost days.

- **A day's OpenRouter quota, burned.** 1,000 requests a day, account-wide, resetting at
  05:30 IST. Spent on runs that were later discarded because of the harness bugs in §3.
- **Killing the wrapper, not the process.** Which produced the double-write in §3.5.
- **Documentation written as a changelog.** Several docs read as *"here is what I got wrong and
  fixed"* rather than as a reference for a reader who has never seen the project. Satyam caught
  this directly: *"it feels like you have written it for your record."* Three documents were
  rewritten as reader-facing references.

---

## 7. The fix was the new defect

The most useful pattern of the project, and the one we would warn anybody about.

| The fix | What it broke |
| --- | --- |
| Replay handling, so redeliveries stop double-counting | Set `executed=True`, so **20 redeliveries summed to 20 payments** and inflated every benchmark score. Caught by an existing test. |
| Step-up resolved before the idempotency claim | Moved it ahead of *all* of idempotency, so a redelivery whose amount was altered in flight arrived as `policy.step_up` — **putting a fraudulent request in an approver's queue** instead of refusing it by name. |
| The authority graph (§1.3) | The webhook never wrote the link, so a capture landed on the ledger and the payment stayed an orphan — **the next legitimate refund was refused.** A containment check that also blocks the normal path is not containment, it is an outage, and it is how a security control gets deleted six weeks later. |
| The freeze guard, rewritten to be repo-local | `Path("").resolve()` returns the *current directory*, so a git call that exits 0 printing nothing answered **"yes, this is the project."** Found by the adversarial test written for the fix, not by reading it. |
| Reporting a replay as `already_done` | The original outcome was spread over the response last, so the rail's `"created"` **overwrote our own status** — answering "created" to a caller asking whether this was a replay. |
| Redacting personal data from evidence | A scrubber that emptied the files would have passed every privacy test **and destroyed the only artefact proving the code ran against a real API.** Hence tests asserting the amounts and audit anchors *survive*. |

The lesson we would give anyone: **a repair is a change, and a change needs an adversarial
test aimed at the repair itself.** Roughly a third of the defects in this document were
introduced by a previous fix, and almost all of those were caught by the test written for the
repair rather than by re-reading the code.

---

## 8. The one that never resolved

A real mobile number reached a public repository.

It came from a test-mode Razorpay checkout, written straight through from the payment
response into committed evidence. **Test mode does not make a phone number synthetic.**

The fix was an allow-list redactor at the point of writing — allow-list rather than
block-list, because a block-list is a list of the fields somebody thought of and the next
provider release adds one nobody thought of. Redaction is visible (`"[redacted]"`) rather than
silent, because a deleted key reads as a provider that never sent one.

**The value remains in git history.** Removing it needs a history rewrite and a force push,
and that was the repository owner's call to make. It is recorded here rather than quietly
omitted.

---

## Who did what

Satyam asked for this section to be accurate rather than polite. It is.

### Satyam Singh — the thinking

**The idea, and the observation it rests on.** PayNaka exists because of a specific noticing:
Razorpay's MCP server exposes 35+ tools to an AI agent and *disables the ones that move money
out*. `create_refund` and `create_instant_settlement` work locally and not remotely. That is
not an oversight — it is a trust boundary drawn by hand by engineers who understood the risk
and had no mechanism to manage it. **Seeing that a disabled feature is a product opportunity
is the entire origin of this project**, and no amount of code generation substitutes for it.

**The framing that made it defensible.** The decision to claim *"persuasion becomes
insufficient"* rather than *"prompt injection is solved"* — and to put that refusal in the
README's opening — is what makes this project survive a hostile reading. It is also the
harder thing to say.

**The instinct to run it like a stranger.** Every failure in §5 came from this. Not from a
plan, from a habit: clone it fresh, follow the README literally, stop at the first thing that
surprises you. It found a destroyed credential file, a silently-absent security scan, a blank
page behind two green builds, and a guard reading a stranger's git repository. **The test
suite found none of them.**

**Refusing "good enough."** *"Looks so bad, not at all like the beautiful site of Razorpay"* is
the reason the console is built on Razorpay's own components instead of hand-formatted rupees.
*"It feels like you have written it for your record"* is the reason three documents are
reader-facing references instead of changelogs. *"We need to score above 90 in each category"*
set the bar that produced most of this document.

**The calls that were judgment, not code.** Which models to spend real money on. Whether to
publish a contested number or exclude it. Whether a benchmark result was worth the quota.
Where to draw the line on scope with a deadline approaching. When to stop.

**Commissioning adversaries.** Bringing in independent audits — twice, on a project already
passing 2,400 tests — and relaying findings without softening them. That is not a comfortable
instinct and it is why §1 exists at all.

### Claude — the building

**The code.** Every module in `paynaka/`, `haat/`, `chaos/`, `merchant/`, `buyer/`, the
console, the scripts, the task runner. Roughly 94 commits' worth.

**The tests, both directions.** 2,460 tests, **1,531 of them adversarial** — malformed input,
boundary values, replay, tampering, injection, homoglyphs, concurrency, duplicate delivery,
clock manipulation, truncation, oversize input. The project's rule that nothing ships with
only forward tests is the reason several defects in this document were found by us rather than
by a reviewer: the mandate double-spend, the unhandled exception from `qty=-1`, the
Windows-specific `os.kill` behaviour, the `Path("")` fail-open, the ₹3,998 ledger.

**Verification rather than assertion.** Where a claim could be checked, it was: replaying every
breach through the real gate rather than asserting refusal; reverting a fix to confirm its test
actually goes red; reproducing an audit finding before fixing it — and reporting the one that
**did not reproduce** rather than fixing something that was not broken.

**Assisting the validation loop.** Turning raw ideas into shapes that could be tested, arguing
back where a claim outran the evidence, and writing down the restraint: what was deliberately
*not* built, and why. `docs/THREATMODEL.md` naming what is not defended is as much a
deliverable as the gate.

### Honestly, the split

Claude was reliable at *"is this correct"* and unreliable at *"does this work for someone who
isn't me."* Satyam was the opposite instrument, and the project needed both. The bugs that
would have been most embarrassing in front of a judge — a first command that does not run, a
blank page, a README that eats your API keys — were **all** found by a human being who sat
down and used the thing.

---

## What we would tell the next team

1. **Write the adversarial test for your fix, not just for the bug.** A third of the defects
   here were introduced by a repair.
2. **A benchmark that flatters you is not a benchmark.** Ours did, four separate ways, and
   every one moved the number in the direction we wanted.
3. **Green checks are not evidence that a person can use your project.** Ours were green
   through a destroyed credentials file, an absent security scan, and a blank page.
4. **Say the weaker true thing.** "Unauthorised order value" is less impressive than "money
   escaped" and it is the one that survives a payments reviewer.
5. **A documented setting the code ignores is worse than no setting at all.**
6. **When you cannot verify a claim, publish that you could not.** The regulatory basis
   document exists because we could not, and saying so is worth more than the claim was.
