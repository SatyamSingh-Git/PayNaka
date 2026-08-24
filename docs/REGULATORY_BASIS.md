# Regulatory basis — and the limits of that phrase

This file exists because the project claimed more than it had checked.

`policy.yaml` carried five rules with comments attributing them to RBI and NPCI, and
`clock.py` opened by saying PayNaka *"encodes real Indian payments regulation."* An
independent review pointed out that these rules have **different scopes** than the flat
statements implied, and that at least two of them were being applied far more broadly than
their source supports.

That is a serious kind of wrong for a payments project. A merchant who reads "RBI: Rs 15,000
additional-factor ceiling" in a config file and ships it has been told something by this
repository, and this repository had not verified it.

## What these rules actually are

**They are configurable policy examples, not compliance.** They are shipped as realistic
defaults so the time-and-limit machinery has something concrete to enforce, and so the
regulatory checks are demonstrable without inventing fictional rules. That is the whole of
the claim.

**No rule in `policy.yaml` has been verified against a primary source by this project.**
Every one of them needs the review below before it governs real money.

**A code rule cannot produce compliance in any case.** Compliance is a property of a
business process, its scope, its exceptions and its evidence. The most a gate can do is
enforce a threshold somebody else established for the right product and the right action.

## The specific overclaims

| Rule in `policy.yaml` | What was implied | What is actually the case |
|---|---|---|
| `afa_threshold: 1500000` | a universal ₹15,000 additional-factor ceiling | Recurring e-mandate AFA thresholds are **category-specific** and have been revised upward for several categories. A single flat number is not the rule. |
| `contact_window: "08:00-19:00"` | general customer-contact hours for payments | The 08:00–19:00 restriction appears in RBI material on **recovery agents and debt collection**, not general payment-customer contact. Applying it to all contact is broader than the source. |
| `debit_blackout: ["10:00-13:00"]` | an NPCI-mandated peak-hour restriction | Processing-window guidance changes and is operational rather than a fixed statutory window. Treat as a configurable operational preference. |
| `npci_mandate_retries: 3` | a fixed NPCI retry ceiling | Retry limits vary by mandate type and scheme rules and change over time. |
| `pre_debit_notice_seconds: 86400` | a fixed RBI 24-hour notice requirement | Pre-debit notification requirements exist for recurring e-mandates; the exact window and the products in scope must be checked against the current circular. |

Sources the review cited as starting points, **not as verification**:

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

## What was changed

- `policy.yaml` comments now say *example, unverified* rather than naming a regulator as
  though it were a citation.
- `clock.py` no longer opens by claiming to encode real regulation. It encodes
  wall-clock-shaped rules, which is what it does.
- Gate refusals name the **configured policy**, not the regulator, so an operator reading a
  denial is not told a rule came from RBI when this project has not checked that it did.

The machinery is unchanged and still worth having: IST-correct windows, an injectable clock,
midnight-wrapping ranges, and deterministic evaluation. What changed is that it no longer
tells you the numbers are law.
