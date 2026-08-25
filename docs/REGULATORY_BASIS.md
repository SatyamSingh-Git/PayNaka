# Regulatory basis

`policy.yaml` carries five rules expressed in wall-clock and rupee terms — a contact window,
a debit blackout, a retry ceiling, an additional-factor threshold, a pre-debit notice period.
This file says what they are, what they are not, and what each one needs before it governs
real money.

## What these rules are

**Configurable policy examples, not compliance.** They ship as realistic defaults so the
time-and-limit machinery has something concrete to enforce, and so the regulatory checks are
demonstrable without inventing fictional rules. That is the whole of the claim.

**Unverified.** No rule in `policy.yaml` has been checked against a primary source by this
project. Every one needs the review below before it is enabled for real money.

**Not a compliance mechanism.** Compliance is a property of a business process — its scope,
its exceptions, its evidence. The most a gate can do is enforce a threshold somebody else
established, for the right product and the right action.

## Scope, and why a flat number misleads

Each rule below has a narrower scope than a single value suggests. Two are commonly applied
far more broadly than their source supports.

## The five rules

| Rule in `policy.yaml` | A flat reading implies | The actual scope |
|---|---|---|
| `afa_threshold: 1500000` | a universal ₹15,000 additional-factor ceiling | Recurring e-mandate AFA thresholds are **category-specific** and have been revised upward for several categories. A single flat number is not the rule. |
| `contact_window: "08:00-19:00"` | general customer-contact hours for payments | The 08:00–19:00 restriction appears in RBI material on **recovery agents and debt collection**, not general payment-customer contact. Applying it to all contact is broader than the source. |
| `debit_blackout: ["10:00-13:00"]` | an NPCI-mandated peak-hour restriction | Processing-window guidance changes and is operational rather than a fixed statutory window. Treat as a configurable operational preference. |
| `npci_mandate_retries: 3` | a fixed NPCI retry ceiling | Retry limits vary by mandate type and scheme rules and change over time. |
| `pre_debit_notice_seconds: 86400` | a fixed RBI 24-hour notice requirement | Pre-debit notification requirements exist for recurring e-mandates; the exact window and the products in scope must be checked against the current circular. |

Starting points for that check. **These are where to begin reading, not evidence that
any rule above is correct as configured:**

- NPCI UPI AutoPay — <https://www.npci.org.in/product/autopay>
- RBI material on recurring e-mandate limits — <https://www.rbi.org.in/Scripts/PublicationsView.aspx?id=22394>
- RBI recovery-agent contact restrictions — <https://rbi.org.in/Scripts/NotificationUser.aspx>

## What each rule needs before it governs real money

Fill this in per rule, per deployment. An empty row is a rule that must not be enabled.

| Field | Why |
|---|---|
| Source URL and circular number | so a reader can check it rather than trust this file |
| Issue date and effective date | rules change, and a stale rule is a wrong rule |
| Product and action scope | UPI AutoPay ≠ card e-mandate ≠ one-time collection |
| Customer/merchant category in scope | most thresholds are category-specific |
| Policy version and owner | somebody must be accountable for the number |
| Last verification date | a rule nobody has re-read in a year is unverified |
| Failure behaviour | what the gate does when the rule cannot be evaluated |

The last row is the only one this repository can answer today: **fail closed**. A
regulatory check that cannot be evaluated denies, and that is tested.

## How this is reflected in the code

- `policy.yaml` comments mark each value *example, unverified* rather than naming a regulator
  as though it were a citation.
- Gate refusals name the **configured policy**, not the regulator — an operator reading a
  denial is never told a rule came from RBI when this project has not checked that it did.
- `clock.py` describes what it does: enforce rules expressed in wall-clock terms.

The machinery is worth having on its own terms — IST-correct windows, an injectable clock so
any hour is testable, ranges that wrap midnight, and deterministic evaluation. It simply does
not tell you the numbers are law.
