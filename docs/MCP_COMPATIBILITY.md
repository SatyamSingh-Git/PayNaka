# MCP compatibility

PayNaka exposes an MCP server that speaks the same protocol as Razorpay's, so an agent can
be pointed at it by changing one URL. It is **a gated subset, not a replacement**: five
tools, three of which move money and are checked against a signed mandate before they run.

Read this before pointing an agent here, because an agent written against Razorpay's own
server will behave differently in the four ways listed under *Divergences*.

## Tool matrix

| Tool | Status | Behaviour |
| --- | --- | --- |
| `create_order` | **gated** | checked against the mandate, then forwarded |
| `capture_payment` | **gated** | checked against the mandate, then forwarded |
| `create_refund` | **gated** | checked, plus a balance claim against the ledger |
| `fetch_payment` | pass-through | reads are not authority, so no mandate is required |
| `fetch_all_payments` | **stub** | returns an empty collection — see below |
| everything else | **absent** | not proxied, not stubbed |

Razorpay's own server publishes 35+ tools. Anything outside this table will fail here.

## Divergences

Five places where an agent written against Razorpay's server behaves differently.

**`fetch_all_payments` returns an empty list.** It exists so a client that lists on startup
does not crash. An empty result means *not implemented*, not *no payments*.

**Money-moving calls require `notes.paynaka_items`.** The mandate checks line items — SKU,
quantity, unit price — and Razorpay's order schema has nowhere to carry them, so they ride
in `notes` as `SKU:qty:unit_paise`, plus `notes.paynaka_destination`. This is an addition to
the upstream schema. Omit it and the call is refused with an explanation of what is missing.

**Responses carry a verdict envelope.** A refusal is a structured decision with a
`check_id` and a reason, not an upstream error shape. That is the product working, and it is
a difference a client must expect.

**A retry answers `already_done`, not an error.** Repeat a money call under the same
idempotency key and the response carries `status: "already_done"`, `replayed: true`, and the
original outcome including the order id — which is the entire reason a client retried. It is
neither a fresh execution nor a refusal, and saying either would be worse than saying
nothing: an agent told `blocked_by_paynaka` retries again, or reports a failed purchase to a
shopper whose money has moved. Supply the key yourself in `notes.idempotency_key` or
`arguments.idempotency_key`. Omit it and one is generated, which means a retry is a *new*
purchase and will be bounded as one.

**No version negotiation.** Nothing here pins or contract-tests against a released upstream
version, so upstream schema drift will not be detected by this repository.

## Binding a mandate

Money-moving tools need a mandate bound to the session, or they refuse. The flow:

```
POST /api/intent            (SHOPPER credential)  ->  signed mandate + mandate_grant
POST /mcp   initialize      {"mandateGrant": "..."}  ->  {"boundSession": "sess_..."}
POST /mcp   tools/call      create_order  ->  checked against that mandate
```

The grant is short-lived and single-use. Session identity is composed from the
*authenticated caller*, not from the `mcp-session-id` header, so one caller cannot bind or
borrow another's session. See [ARCHITECTURE.md](ARCHITECTURE.md) for the trust boundary.

## Positioning

Razorpay's remote server already restricts some high-risk operations. PayNaka is not
protection Razorpay omitted — it is **finer-grained, merchant-controlled authority**: a
per-session budget the merchant sets, bound to a signed statement of one shopper's intent,
enforced by code that holds no credentials.

That argument does not need a large tool count, which is why this one is small.

## What full compatibility would require

Out of scope for this build, listed so the gap is a decision rather than an oversight:

1. Transparent pass-through for every read tool, forwarding upstream responses unchanged.
2. A documented schema extension per money-moving tool, versioned, replacing the `notes`
   carrier.
3. A pinned upstream version with contract tests in CI, so drift fails a build.
4. Version negotiation at `initialize`, so a client learns what it is talking to.
