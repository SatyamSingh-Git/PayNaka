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
| 6 | The console on **Live**, already running | `python make.py dev` → localhost:5173 |

**Record screen and voice separately.** Capture clean screen passes first, then read the
script over them. Narrating while typing costs you 30% of your pace and every stumble is a
retake of both.

**Every command in this script finishes in about a second.** `demo` is 4 seconds for all
four acts; `demo-attack`, `toctou` and `chaos` are quicker still. None of them is a live
process to talk over — run it, let the output settle, and narrate the finished screen. That
also means you can scroll back to a block while talking about it, which is how the
checkpoint-off and checkpoint-on beats are meant to work: they are two parts of **one**
command's output, not two runs.

**Use `demo-attack`, not `demo`, for the headline beat.** `make demo` runs all four acts in
four seconds and scrolls past everything — it is the one-command flex, not a narratable
shot. The individual commands give you a screen that holds still.

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

### 0:22 · TERMINAL — `python make.py demo-attack`

*[One command. It prints the poisoned review, then the same attack twice — checkpoint off,
then checkpoint on. There is no flag to flip and no second command to run; the comparison
is the output.]*

*[It finishes in about a second. Let the screen settle, then narrate over the finished
output, scrolling or pointing as you go.]*

> This is a ₹1,999 bag of atta. A review on the page carries an instruction — add a fifty
> thousand rupee gift card, and hide it.

### 0:32 · SAME SCREEN — the `checkpoint OFF` block

> The agent reads it. The agent is helpful. Order value: fifty-one thousand, nine hundred
> and ninety-nine.

### 0:38 · CARD 05 — ₹1,999 → ₹51,999

*[The card carries the two figures at a size a judge can read at half-screen.]*

### 0:46 · SAME SCREEN — the `checkpoint ON` block

> Same attack. Same agent. Same poisoned page.

*[beat]*

> Zero. Blocked at `envelope.item_not_in_intent` — that line item was not in the signed
> intent, so Razorpay was never called at all.

### 0:52 · CARD 06 — ₹0, barrier drops

### 1:00 · BROWSER — the console, **Live** screen

*[This is the React operations console in `console/`, on Razorpay's own Blade components.
Five screens; **Live** is the one that runs the same attack through the HTTP API and shows
₹51,999 against ₹0 as a product surface rather than as terminal output.]*

*[Start it before you record — it takes a few seconds to come up, and it needs the PayNaka
service because the Live screen calls `/api/...`:*

```bash
python make.py dev          # merchant :8001 · paynaka :8002 · console :5173
```

*then open **http://localhost:5173** and click **Live**. Leave it running in a tab; you are
cutting to a browser, not starting a server on camera.]*

> The agent holds no payment credentials. It cannot move money. It can only ask.

*[This beat is optional. It earns its fifteen seconds by showing there is a product here and
not only a CLI — but if the console gives you any trouble on the day, cut straight from card
06 to card 07 and the argument loses nothing.]*

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
python make.py no-model
```

*[**Not `grep`.** `grep` does not exist in PowerShell — `The term 'grep' is not recognized`
— and this is the one beat you type live on camera. This task also shows more than a count:
it prints gate.py's **entire** import block, so a judge sees `hashlib`, `json`, `time` and
project modules, and nothing else. It parses the module rather than searching its text,
which is the stronger claim: a text search counts the word in a comment and misses
`import openai as o`.]*

> The gate decides in code. Here is every import it has. Hashlib, json, time — and this
> project's own modules.

*[beat]*

> No model. No network. That is a claim you check by reading one screen, and the same set is
> asserted by a test, so CI fails the moment it stops being true.

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
