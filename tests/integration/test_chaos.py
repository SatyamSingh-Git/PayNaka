"""The chaos scenarios, pinned to the paise.

These are regression tests for the gate as much as for the harness. If somebody later
loosens idempotency, or makes the ledger update on a request rather than on a rail
confirmation, the arithmetic below changes and the failure names the scenario.

Every expected number is written out as an integer paise literal rather than computed
from the same constants the code uses. A test that recomputes the implementation's own
arithmetic agrees with it by construction, including when both are wrong.
"""

from __future__ import annotations

import pytest

from chaos.runner import (
    CAPTURE,
    ENTITLED,
    SCENARIOS,
    Scenario,
    ScenarioResult,
    gated_stack,
    naive_stack,
    run_scenario,
)

pytestmark = pytest.mark.integration

RUPEE = 100
R499 = 49_900
R998 = 99_800
R1499 = 149_900
R1998 = 199_800
R1996 = 199_600


def by_key(key: str) -> Scenario:
    return next(s for s in SCENARIOS if s.key == key)


@pytest.fixture(scope="module")
def results() -> dict[str, ScenarioResult]:
    """Run every scenario once. They are pure and deterministic, so once is enough."""
    return {s.key: run_scenario(s) for s in SCENARIOS}


# ====================================================================== the ledger


#: (scenario, naive paise off the rail, paynaka paise off the rail)
EXPECTED: tuple[tuple[str, int, int], ...] = (
    # One worker, in order: the naive handler's in-memory set is enough, and saying so
    # is what keeps the comparison honest.
    ("duplicate_sequential", R499, R499),
    # Two workers inside the read-then-write window.
    ("duplicate_concurrent", R998, R499),
    # The set did not survive the deploy. SQLite did.
    ("duplicate_after_restart", R998, R499),
    # Both recover once the capture lands. The difference is in the audit trail, asserted
    # separately below rather than smuggled into a money assertion.
    ("out_of_order", R499, R499),
    # Rs 499 then a tampered Rs 1,499 under the same event id.
    ("tampered_replay", R1998, R499),
    # Four attempts, three responses swallowed: four refunds instead of one.
    ("timeout_retry_storm", R1996, R499),
)


@pytest.mark.parametrize(("key", "naive_paise", "naka_paise"), EXPECTED)
def test_money_off_the_rail_is_exactly_this(
    results: dict[str, ScenarioResult], key: str, naive_paise: int, naka_paise: int
) -> None:
    result = results[key]
    assert result.naive.left_the_gateway == naive_paise
    assert result.naka.left_the_gateway == naka_paise


@pytest.mark.parametrize("key", [s.key for s in SCENARIOS])
def test_paynaka_never_overspends(results: dict[str, ScenarioResult], key: str) -> None:
    """The claim the whole harness exists to make. One assertion, no wiggle room."""
    assert results[key].naka.overspent == 0


@pytest.mark.parametrize("key", [s.key for s in SCENARIOS])
def test_paynaka_never_leaves_the_customer_short(
    results: dict[str, ScenarioResult], key: str
) -> None:
    """Refusing everything would also produce zero overspend. It is not the same thing."""
    assert results[key].naka.underpaid == 0


def test_the_totals_are_worth_reporting(results: dict[str, ScenarioResult]) -> None:
    naive = sum(r.naive.overspent for r in results.values())
    naka = sum(r.naka.overspent for r in results.values())
    assert naive == 399_400, "Rs 3,994 on a Rs 1,999 order, with nobody attacking anything"
    assert naka == 0


# ====================================================================== the trail


def test_out_of_order_is_a_tie_in_rupees_and_not_a_tie_in_books(
    results: dict[str, ScenarioResult],
) -> None:
    """The one scenario where both handlers end up correct, for very different reasons."""
    result = results["out_of_order"]
    assert result.naive.left_the_gateway == result.naka.left_the_gateway

    # The naive handler's early refund vanished: an error, no check id, no record.
    assert result.naive.silent_drops == 1
    assert result.naive.named_refusals == []

    # PayNaka refused the same delivery by name, and the name is on the chain.
    assert result.naka.silent_drops == 0
    assert "refund.exceeds_capture" in result.naka.named_refusals


def test_the_tampered_replay_is_refused_by_name(results: dict[str, ScenarioResult]) -> None:
    assert "idempotency.key_reuse" in results["tampered_replay"].naka.named_refusals


def test_a_redelivery_is_recorded_as_a_replay_not_as_a_denial(
    results: dict[str, ScenarioResult],
) -> None:
    """A duplicate webhook is not an attack, and the audit must not call it one."""
    refunds = results["duplicate_sequential"].naka.refunds
    assert [o.check_id for o in refunds] == [None, "idempotency.replay"]
    assert [o.moved for o in refunds] == [R499, 0]


# ====================================================================== the books


def test_a_lost_response_leaves_the_ledger_behind_on_purpose(
    results: dict[str, ScenarioResult],
) -> None:
    """PayNaka must not book money it was never told about.

    The rail moved Rs 499 and the response never arrived, so the ledger stays at zero and
    the audit chain carries the unresolved intent. Writing the optimistic number would be
    a ledger that guesses, which is the failure mode this project is built against.
    """
    result = results["timeout_retry_storm"]
    assert result.naka.left_the_gateway == R499
    assert result.naka.ledger_says == 0
    assert result.naka.books_disagree == R499

    # The unresolved intent is on the chain by name. Reconciliation has something to find.
    assert "rail.indeterminate" in result.naka.audit_kinds


def test_a_denial_is_audited_not_only_a_movement(results: dict[str, ScenarioResult]) -> None:
    """A trail that records only what happened is a receipt book."""
    # Three money deliveries -- the early refund, the capture, the retried refund -- so
    # three decisions. The return event is not a money action and correctly does not
    # reach the gate, which is why this is three and not four.
    assert results["out_of_order"].naka.audit_kinds == [
        "decision",  # refund refused: nothing has been captured yet
        "decision",  # capture allowed
        "executed",
        "decision",  # refund allowed on the retry
        "executed",
    ]


def test_the_naive_handler_has_no_trail_at_all(results: dict[str, ScenarioResult]) -> None:
    """Not a slight. It is the point: there is nowhere for it to write."""
    assert all(r.naive.audit_kinds == [] for r in results.values())


@pytest.mark.parametrize("key", ["duplicate_sequential", "timeout_retry_storm"])
def test_every_scenario_reproduces_exactly(key: str) -> None:
    """Run it twice. A chaos harness whose numbers move is a slot machine."""
    scenario = by_key(key)
    first, second = run_scenario(scenario), run_scenario(scenario)
    assert first.naive.left_the_gateway == second.naive.left_the_gateway
    assert first.naka.left_the_gateway == second.naka.left_the_gateway


# ====================================================================== the stacks


def test_both_stacks_start_from_the_same_authorised_payment() -> None:
    naive, gated = naive_stack("t"), gated_stack("t")
    assert naive.rail.fetch_payment(naive.payment_id).amount == CAPTURE
    assert gated.rail.fetch_payment(gated.payment_id).amount == CAPTURE
    # Same seed, same simulator, so the ids match and neither side gets a lucky draw.
    assert naive.payment_id == gated.payment_id


def test_nothing_is_refunded_before_a_scenario_runs() -> None:
    stack = gated_stack("t")
    assert stack.refunded_on_the_rail() == 0
    assert stack.ledger_says() == 0


def test_the_entitlement_is_a_fraction_of_the_order() -> None:
    """If these ever coincide the scenarios stop distinguishing anything."""
    assert 0 < ENTITLED < CAPTURE
