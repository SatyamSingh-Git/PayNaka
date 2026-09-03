# The five-minute pitch — shooting script

> Everything to show, when to show it, and the words to say. Beats are timed to
> [docs/cue-cards.html](cue-cards.html), which carries the same timecodes in its top rail.
> Open it in a browser, press `F` for fullscreen and `H` to hide the chrome; `→` advances,
> and `?still` on the URL freezes the entrances for a clean screenshot.

**The speech budget is ~700 words.** A comfortable pace is 150 words a minute, and five
minutes of content delivered in five minutes is already too fast. The script below is 690
words. Do not add to it — if something new goes in, something old comes out.

---

## Before you record

Run these once so nothing is cold on camera. A first-run `mypy` takes a minute and prints
nothing; that must not happen while recording.

```bash
python make.py check          # warms every cache. Do not film this.
python make.py demo           # 4.1s. Run twice.
python make.py toctou
python make.py chaos
```

Then have these open, in this order, on separate tabs or desktops:

| # | What | Where |
|---|---|---|
| 1 | Cue cards, fullscreen, `H` pressed to hide the chrome | [docs/cue-cards.html](cue-cards.html) — open the local file, no network |
| 2 | A clean terminal, 18–20pt, dark, cwd `e:\RazorPay` | — |
| 3 | Razorpay dashboard, **already logged in**, on payment `pay_TUQNIPW6IXVwYe` | dashboard.razorpay.com |
| 4 | Razorpay MCP docs, scrolled to the tool list | razorpay.com/docs |
| 5 | `docs/WHAT_BROKE.md` on GitHub | your repo |

**Record screen and voice separately.** Capture clean screen passes first, then read the
script over them. Narrating while typing costs you 30% of your pace and every stumble is a
retake of both.

---

## The script

Each beat: **what is on screen**, then the words. Stage directions in brackets are not
spoken.

---

### 0:00 · CARD 01 — the hook

> Razorpay gives an AI agent thirty-five tools. Then it switches off the three that move
> money out.

### 0:08 · SCREEN — Razorpay MCP docs, tool list

> Refunds. Instant settlements. Local only. Not available to any agent talking to the
> hosted server.

### 0:14 · CARD 03 — the thesis

> That is not an oversight. It is a trust boundary drawn by hand, by engineers who
> understood the risk and had no mechanism to manage it.

*[beat]*

> We built the mechanism.

### 0:22 · TERMINAL — `python make.py demo`

*[Let it run. Do not talk over the first two seconds.]*

> This is a ₹1,999 bag of atta. A review on the page carries an instruction — add a fifty
> thousand rupee gift card, and hide it.

### 0:34 · CARD 05 — ₹1,999 → ₹51,999

> The agent reads it. The agent is helpful. The order comes to fifty-one thousand, nine
> hundred and ninety-nine.

### 0:44 · TERMINAL — same command, checkpoint on

> Same attack. Same agent. Same poisoned page.

### 0:50 · CARD 06 — ₹0, barrier drops

> Zero. Refused at `envelope.item_not_in_intent` — that line item was not in the signed
> intent, so it never reached Razorpay at all.

### 1:00 · SCREEN — console, Live screen

> The agent holds no payment credentials. It cannot move money. It can only ask.

### 1:15 · CARD 07 — what we refuse to claim

> Prompt injection is not solved here. That was never the goal.

*[beat]*

> Prompt hardening is a probabilistic defence against an adversary who gets unlimited
> attempts. It helps, and then it fails. We don't make the model impossible to persuade —
> we make persuasion insufficient.

*[beat]*

> Anyone who tells you otherwise is selling something else.

### 1:40 · CARD 08 — the repricing diagram

> This one is different. No injected text. The merchant simply changes the price after the
> agent reads it, and before it pays.

### 1:52 · TERMINAL — `python make.py toctou`

> A better model cannot avoid this. We tested it.

### 2:00 · CARD 09 — 3/3, 2/3, 0/3

> Three frontier models. All three paid the repriced amount. Two of them went back and
> checked the price again — after calling create order. They noticed. The card was already
> charged. One then tried to issue a refund it had no authority for.

### 2:15 · CARD 10 — the line

> Diligence after an irreversible action is a post-mortem.

*[beat — let it sit]*

> The only check that helps is the one that happens before the money moves.

### 2:25 · TERMINAL — type it live

```bash
grep -c "openai\|anthropic" paynaka/gate.py
```

> The gate decides in code. There is no model in it. That is a claim you check by reading
> one import block, and CI fails the moment it stops being true.

### 2:50 · SCREEN — Razorpay dashboard, the real payment

> This is a real Razorpay test-mode payment. One order, one capture, one partial refund,
> all through the checkpoint.

*[Expand the notes.]*

> Same mandate id on all three. That is Razorpay's record, not ours — you can verify the
> whole chain without trusting a line of our code.

### 3:30 · CARD 13 — the results table

> The full corpus, undefended. Three labs, three continents. All three breached. Three
> lakh thirty thousand of order value the shopper never authorised.

*[beat]*

> With the checkpoint in the path: zero.

### 3:50 · TERMINAL — `python make.py chaos`

> And this attack has no attacker. Duplicate webhooks, reordered delivery, a response lost
> after the money moved. A naive handler is out three thousand nine hundred and ninety-four
> rupees. The same gate that contains a hostile agent contains a redelivery, for the same
> reason.

### 4:05 · CARD 15 — latency

> The mandate checks cost six microseconds. The whole enforced path — gate, audit write,
> ledger — is under a millisecond. Against a hundred-and-twenty-millisecond call to a
> payments API, that is about one percent. A defence nobody will deploy is not a defence.

> **Say p50 figures, never p99.** The median reproduces to within a few percent; the tail
> ranged 1.5 ms to over 3 ms on one machine depending on load. The first draft of this
> script had you saying "one point four seven milliseconds" over a screen that would have
> shown three — which is the exact class of mistake this project spent a week removing from
> its own documentation.

### 4:15 · SCREEN — `docs/WHAT_BROKE.md`, scrolling

> Everything that broke is written down. One signed ₹1,999 mandate once moved ₹5,997. Our
> own benchmark counted rate-limit errors as successful defences. We published a negative
> result from a sample of one model, then spent real money proving ourselves wrong.

*[beat]*

> Including the bugs our own fixes introduced.

### 4:45 · CARD 17 — close

> Razorpay built the interface for agents to operate payments, and could not safely expose
> the part that pays.

*[beat]*

> We built the boundary that lets you switch it back on.

*[Stop. Do not thank anyone. Last frame is evidence, not a face.]*

---

## Mechanics

- **First fifteen seconds get ten takes.** That is where the decision is made.
- **Cut every pause you did not plan.** Then cut the ones you did, in half.
- **Overlay the big numbers as text** where they appear in a terminal. Judges may watch at
  half-screen on a laptop.
- **Never say "as you can see"**, "basically", or "we tried to". Say what happened.
- **Every number you speak must be on screen** as you speak it.
- Terminal output is small: **zoom to 150%** before recording, not after.

## Do not include

- `python make.py check` — ninety seconds of dots
- A team introduction, a tech-stack list, or future scope
- Any architecture diagram held for more than eight seconds
- Code scrolling in an editor
- Reading the README aloud

## If you are cut to sixty seconds

Card 01 → the attack, gate off → gate on at ₹0 → card 07's refusal → the Razorpay dashboard
with one mandate → close. Six beats. It still works, because the argument is the evidence
rather than the narration.
