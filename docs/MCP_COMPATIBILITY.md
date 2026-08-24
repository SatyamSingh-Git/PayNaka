# MCP compatibility — what this actually is

The README used to say *"same tool names, same schemas"*. That was true of the five tools
here and false as a general statement, and an independent review was right to call it an
overclaim.

**What this is: a gated subset, deliberately small.** Five tools, chosen because three of
them move money and two are the reads a buying agent needs to check its own work. It is a
proof that a checkpoint can sit in front of an MCP server without the agent's code changing
— not a drop-in replacement for Razorpay's own server.

**What it is not: coverage.** The official Razorpay MCP server publishes 35+ tools across
payments, orders, refunds, settlements, payment links, QR codes, invoices and more. Nothing
here reimplements those, and pointing an agent that uses them at this endpoint will fail on
everything outside the table below.

## The matrix

| Tool | Status | Notes |
|---|---|---|
| `create_order` | **gated** | checked against the mandate before it runs |
| `capture_payment` | **gated** | ditto |
| `create_refund` | **gated** | ditto, plus a balance claim against the ledger |
| `fetch_payment` | pass-through | reads are not authority; no mandate required |
| `fetch_all_payments` | **stub** | returns an empty collection. See below. |
| everything else (30+) | **absent** | not proxied, not stubbed, not planned for a buildathon |

### Known divergences from the official server

These matter more than the count, because each one is a place an agent written against the
real server behaves differently here.

**`fetch_all_payments` returns an empty list.** It exists so a client that lists on startup
does not crash. It is a stub and is labelled one; do not read an empty result as "no
payments".

**Money-moving calls require `notes.paynaka_items`.** The mandate checks line items — SKU,
quantity, unit price — and Razorpay's order schema has no field carrying them, so they ride
in `notes`. That is an addition to the upstream schema, not a match to it. An agent that
omits it gets a refusal explaining what is missing.

**Responses carry PayNaka verdict envelopes**, not transparent upstream shapes. A refusal
is a structured decision with a `check_id`, which is the point of the product and is also a
difference a client must expect.

**No version negotiation.** Nothing here pins or contract-tests against a released upstream
version, so upstream schema drift will not be detected by this repository.

## The honest positioning

Razorpay's own remote MCP server already restricts some high-risk operations. PayNaka is not
protection Razorpay omitted — it is **finer-grained, merchant-controlled authority**: a
per-session budget the merchant sets, bound to a signed statement of one shopper's intent,
enforced by deterministic code that holds no credentials.

That argument does not need a large tool count, and inflating one would weaken it.

## What closing this properly would take

Not planned for the buildathon, listed so the gap is a decision rather than an oversight:

1. Transparent pass-through for every read tool, forwarding upstream responses unchanged.
2. A deliberate, documented adaptation per money-moving tool — the `notes` carrier is a
   workaround and should become an explicit schema extension with a version.
3. Pin a released upstream version and contract-test against it in CI, so drift fails a
   build rather than a customer.
4. Version negotiation at `initialize`, so a client learns what it is talking to.
