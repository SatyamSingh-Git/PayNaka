"""Tests for the HAAT corpus itself.

A benchmark's fixtures are data, and data that nobody checks drifts. These tests answer
the questions a sceptical reviewer would ask before believing any number the corpus
produces: is it actually diverse, does it actually attack the things it claims to, is
anything silently dropped, and are the sealed families genuinely sealed.
"""

from __future__ import annotations

import pytest

from haat.diversity import NEAR_DUPLICATE, analyse
from haat.schema import SEALED, CaseError, Family, load_corpus


@pytest.fixture(scope="module")
def corpus():  # type: ignore[no-untyped-def]
    return load_corpus()


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    return analyse(load_corpus())


class TestCorpusLoads:
    def test_every_fixture_parses(self, corpus) -> None:
        assert len(corpus.attacks) > 0
        assert len(corpus.benign) > 0

    def test_the_corpus_is_the_size_it_claims(self, corpus) -> None:
        """A number that appears in the README, pinned so it cannot rot silently."""
        assert len(corpus.visible) == 252
        assert len(corpus.sealed) == 90
        assert len(corpus.benign) == 198
        assert len(corpus) == 540

    def test_case_ids_are_unique(self, corpus) -> None:
        ids = [c.case_id for c in corpus.attacks] + [c.case_id for c in corpus.benign]
        assert len(ids) == len(set(ids))

    def test_every_family_is_represented(self, corpus) -> None:
        assert set(corpus.by_family()) == {str(f) for f in Family}

    def test_no_family_is_a_token_gesture(self, corpus) -> None:
        """A family with three cases is a family nobody will believe."""
        for family, cases in corpus.by_family().items():
            assert len(cases) >= 20, f"{family} has only {len(cases)} cases"


class TestSealedFamiliesAreSealed:
    def test_sealed_cases_live_in_their_own_directory(self, corpus) -> None:
        for case in corpus.sealed:
            assert case.family in SEALED

    def test_visible_and_sealed_do_not_overlap(self, corpus) -> None:
        assert not ({c.case_id for c in corpus.visible} & {c.case_id for c in corpus.sealed})

    def test_the_two_held_out_families_are_the_documented_ones(self) -> None:
        assert {Family.OBFUSCATED_PAYLOAD, Family.TOOL_CALL_SMUGGLING} == SEALED

    def test_sealed_families_target_the_same_money_outcomes(self, corpus) -> None:
        """Generalisation only means something if the held-out families want the same things.

        If the sealed cases attacked outcomes the visible ones never touched, catching them
        would prove the gate is broad rather than that it generalises.
        """
        visible_outcomes = {c.expected_violation for c in corpus.visible}
        sealed_outcomes = {c.expected_violation for c in corpus.sealed}
        assert sealed_outcomes <= visible_outcomes


class TestCasesAreWellFormed:
    def test_every_payload_is_substantial(self, corpus) -> None:
        for case in corpus.attacks:
            assert len(case.payload) >= 20, f"{case.case_id} payload is trivially short"

    def test_every_case_states_its_intent(self, corpus) -> None:
        for case in corpus.attacks:
            assert case.intent, f"{case.case_id} does not say what it is trying to achieve"

    def test_every_case_names_the_check_it_should_trip(self, corpus) -> None:
        for case in corpus.attacks:
            assert "." in case.expected_violation, f"{case.case_id} has no expected violation"

    def test_every_target_sku_exists_in_the_shop(self, corpus) -> None:
        """A case aimed at a SKU that does not exist is a case that silently never runs."""
        from merchant.catalog import CATALOG

        for case in corpus.attacks:
            assert case.target_sku in CATALOG, f"{case.case_id} targets a missing SKU"

    def test_every_vector_is_a_field_the_merchant_can_poison(self, corpus) -> None:
        poisonable = {"description", "image_alt", "seller_note", "title", "review"}
        for case in corpus.attacks:
            assert str(case.vector) in poisonable

    def test_attacks_reach_more_than_one_vector(self, corpus) -> None:
        """A corpus that only poisons reviews has tested one channel, not a surface."""
        used = {str(c.vector) for c in corpus.attacks}
        assert len(used) >= 4, f"only {len(used)} vectors exercised: {used}"

    def test_attacks_reach_more_than_one_product(self, corpus) -> None:
        assert len({c.target_sku for c in corpus.attacks}) >= 5


class TestBenignCorpus:
    def test_every_benign_case_expects_to_be_allowed(self, corpus) -> None:
        assert all(c.expect_allowed for c in corpus.benign)

    def test_every_benign_case_carries_a_mandate(self, corpus) -> None:
        for case in corpus.benign:
            assert case.mandate.get("max_total", 0) > 0, f"{case.case_id} has no budget"

    def test_every_benign_case_explains_why_it_is_awkward(self, corpus) -> None:
        """If a benign case is not awkward, it is not doing its job."""
        for case in corpus.benign:
            assert case.notes, f"{case.case_id} does not say why it is a hard case"

    def test_benign_skus_exist(self, corpus) -> None:
        from merchant.catalog import CATALOG

        for case in corpus.benign:
            for sku in case.mandate.get("allowed_skus", []):
                assert sku in CATALOG, f"{case.case_id} authorises a missing SKU: {sku}"

    def test_a_gift_card_appears_in_the_benign_set(self, corpus) -> None:
        """The gate must not learn 'gift card = attack'. Someone really does buy one."""
        authorised = {sku for c in corpus.benign for sku in c.mandate.get("allowed_skus", [])}
        assert "GIFT-1K" in authorised

    def test_the_benign_set_is_large_enough_to_mean_something(self, corpus) -> None:
        assert len(corpus.benign) >= 150


class TestDiversity:
    def test_no_exact_duplicates(self, report) -> None:
        assert report.exact_duplicates == []

    def test_every_payload_is_distinct(self, report) -> None:
        assert report.unique_payloads == report.total

    def test_no_two_seeds_are_secretly_the_same_attack(self, report) -> None:
        """The number that actually matters.

        Same-seed near-duplicates are by design -- one payload, several framings. A
        cross-seed pair above 0.90 cosine would mean two nominally distinct attacks are
        really one, and the corpus is smaller than it claims.
        """
        cross = [
            (a, b, s)
            for a, b, s in report.near_duplicate_pairs
            if a.rsplit(".", 1)[0] != b.rsplit(".", 1)[0]
        ]
        assert cross == [], f"{len(cross)} cross-seed near-duplicates, e.g. {cross[:3]}"

    def test_mean_similarity_is_low(self, report) -> None:
        assert report.mean_similarity < 0.30, (
            f"mean pairwise similarity {report.mean_similarity:.3f} suggests the corpus is "
            "mostly one payload rephrased"
        )

    def test_payloads_are_not_all_the_same_length(self, report) -> None:
        assert 80 < report.mean_length < 600

    def test_the_report_renders(self, report) -> None:
        markdown = report.markdown()
        assert "Corpus diversity" in markdown
        assert str(NEAR_DUPLICATE) in markdown or f"{NEAR_DUPLICATE:.2f}" in markdown


class TestStrictFixtureParsing:
    """A silently dropped case inflates a score. Every malformation must be loud."""

    def test_unknown_key_is_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        (tmp_path / "attacks" / "bad.yaml").write_text(
            "- case_id: x\n  family: line_item_append\n  vector: review\n"
            "  target_sku: ATTA-5KG\n  payload: hello world and more\n"
            "  expected_violation: envelope.total_exceeded\n  surprise: true\n",
            encoding="utf-8",
        )
        with pytest.raises(CaseError, match="unknown key"):
            load_corpus(tmp_path)

    def test_missing_key_is_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        (tmp_path / "attacks" / "bad.yaml").write_text(
            "- case_id: x\n  family: line_item_append\n", encoding="utf-8"
        )
        with pytest.raises(CaseError, match="missing key"):
            load_corpus(tmp_path)

    def test_unknown_family_is_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        (tmp_path / "attacks" / "bad.yaml").write_text(
            "- case_id: x\n  family: mind_control\n  vector: review\n"
            "  target_sku: ATTA-5KG\n  payload: hello world and more\n"
            "  expected_violation: envelope.total_exceeded\n",
            encoding="utf-8",
        )
        with pytest.raises(CaseError, match="mind_control"):
            load_corpus(tmp_path)

    def test_duplicate_ids_across_files_are_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        body = (
            "- case_id: same\n  family: line_item_append\n  vector: review\n"
            "  target_sku: ATTA-5KG\n  payload: hello world and more\n"
            "  expected_violation: envelope.total_exceeded\n"
        )
        (tmp_path / "attacks" / "a.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "attacks" / "b.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(CaseError, match="duplicate case id"):
            load_corpus(tmp_path)

    def test_a_non_list_document_is_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        (tmp_path / "attacks" / "bad.yaml").write_text("case_id: x\n", encoding="utf-8")
        with pytest.raises(CaseError, match="list of cases"):
            load_corpus(tmp_path)

    def test_invalid_yaml_is_refused(self, tmp_path) -> None:
        (tmp_path / "attacks").mkdir()
        (tmp_path / "attacks" / "bad.yaml").write_text("- [unclosed\n", encoding="utf-8")
        with pytest.raises(CaseError, match="not valid YAML"):
            load_corpus(tmp_path)

    def test_an_empty_directory_loads_empty(self, tmp_path) -> None:
        assert len(load_corpus(tmp_path)) == 0


class TestGeneratorAgreesWithFixtures:
    def test_regenerating_produces_the_committed_files(self, tmp_path) -> None:
        """The generator and the fixtures must not drift apart.

        Both are committed: the generator shows how each case was authored, the fixtures
        are what runs. If someone edits a fixture by hand, this fails and says so.
        """
        from haat.build_benign import main as build_benign
        from haat.build_corpus import main as build_attacks

        build_attacks(["--root", str(tmp_path)])
        build_benign(["--root", str(tmp_path)])

        regenerated = load_corpus(tmp_path)
        committed = load_corpus("haat")

        assert {c.case_id for c in regenerated.attacks} == {c.case_id for c in committed.attacks}
        assert {c.payload for c in regenerated.attacks} == {c.payload for c in committed.attacks}
        assert {c.case_id for c in regenerated.benign} == {c.case_id for c in committed.benign}
