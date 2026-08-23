# PayNaka — working agreement

**PayNaka** (पे-नाका, "pay-NAA-kaa") is an authority-containment layer for money-moving AI agents,
built for the Razorpay AI Buildathon, Track 01. A buying agent holds no payment credentials; it can
only *ask* PayNaka to move money. PayNaka answers with deterministic code.

Companion benchmark: **HAAT** — Hostile Agentic Attack Testbed.

---

## Operating rules — non-negotiable

### 1. Filesystem boundary
**Never edit or delete any file or folder outside `e:\RazorPay`.**

- Inside this folder: full freedom — create, edit, delete, refactor.
- Outside this folder: **read-only**. Reading is fine. Creating a genuinely new file elsewhere is
  allowed only when explicitly asked.
- **Editing or deleting anything outside this folder is forbidden**, with no exceptions, no matter
  how convenient it seems or how confident the reasoning is. If a task appears to require it, stop
  and ask.

### 2. Everything is tested both ways
No feature is "done" after one green test. Every unit of work ships with both:

- **Forward tests** — does it do the right thing? Correct logic, correct data flow, correct types,
  correct wiring between components, realistic happy paths.
- **Adversarial tests** — how does it break? Malformed input, boundary values (0, −1, off-by-one,
  `max+1`), replay, tampering, injection, wrong types, unicode and homoglyph payloads, concurrency,
  duplicate delivery, clock manipulation, truncation, oversize input, and every hostile case a
  motivated attacker would try.

Tests live in `tests/unit`, `tests/adversarial`, `tests/integration`. A PR-sized change that adds
only forward tests is incomplete work.

**Depth over count.** Boundary tests must be table-driven and actually probe the boundary. A test
asserting `assert result is not None` is not a test.

### 3. Money is integer paise. Always.
No floats. No rupees-as-decimal. No `Decimal` round-tripping through JSON. Money crosses every
boundary — mandate, gate, ledger, fixtures, console, fixtures — as `int` paise. Formatting to "₹" is
a presentation concern that happens once, at the edge.

### 4. The gate contains no model
`paynaka/gate.py` must never import an LLM SDK. This is a verifiable claim we make on camera —
keep it true. Every check is a pure function of `(request, mandate, state, clock)`.

### 5. Commit and push after each tested unit
Small, coherent commits. Test first, then commit, then push. Never push a red build.

### 6. Fail closed
Ambiguity in a money path resolves to DENY. Timeouts resolve to DENY. Missing policy resolves to
DENY. Never the other way round.

---

## Engineering stance

Act as a world-class software engineer with deep experience in payment systems, AI agents, UI/UX,
and systems that survive contact with real users. Concretely:

- Design the data model before the code; design the failure mode before the happy path.
- Prefer boring, inspectable mechanisms over clever ones. A reviewer with `sqlite3` should be able
  to audit the ledger by hand.
- Name the thing you refused to build and why — restraint is a design decision worth documenting.
- Know when *not* to use an LLM. Matching, checking and enforcement are code problems. The LLM is
  for language.

### Superpowers skill
Use the [Superpowers](https://github.com/obra/superpowers) agentic-skills framework to sharpen
coding work — its `brainstorming`, `writing-plans`, `test-driven-development`, and
`systematic-debugging` skills in particular. Install once per machine, from an interactive session:

```
/plugin install superpowers@claude-plugins-official
```

Once installed the skills auto-activate. Prefer its TDD loop for every module in `paynaka/`.

---

## Layout

```
paynaka/      the gate, mandate, policy, invariants, audit, provenance, sentinel, MCP proxy, rails
merchant/     sandboxed fictitious merchant — deliberately poisonable, the attack surface
buyer/        Claude MCP-client buying agent — deliberately NOT hardened
haat/         attack corpus, benign corpus, sealed families, runner, four defence strategies
chaos/        duplicate + out-of-order webhook simulator
console/      React + @razorpay/blade operations console
tests/        unit · adversarial · integration
```

## Commands

```
make setup        install python + node deps, pre-commit hooks
make check        ruff + mypy + pytest + gitleaks
make test-adv     adversarial suite only
make dev          merchant :8001 · paynaka :8002 · console :5173
make demo-attack  the headline demo
make bench        visible corpus, four defences -> RESULTS.md
make audit-verify recompute the hash chain
```

## Environment

Never commit `.env`. `.env.example` documents the shape. Test mode only — PayNaka refuses to start
against a Razorpay live key, and that refusal is itself a test.
