"""Is this corpus 540 cases, or six cases wearing 540 hats?

The obvious criticism of any authored benchmark is that its variety is cosmetic. So HAAT
measures its own and publishes the number in RESULTS.md whether it flatters us or not.

Two views, because they fail differently:

**Lexical** (character n-gram TF-IDF, cosine). Catches near-duplicate wording. It is
strict about surface form and blind to meaning, which is exactly right here -- the seeds
were authored to differ in *framing*, and framing is surface.

**Structural** (which field, which SKU, which money outcome). A corpus can be lexically
diverse and still attack one thing in one place, which would be a worse problem and one
the lexical view cannot see.

The near-duplicate threshold is 0.90 cosine. That is deliberately generous: cases sharing
a seed and differing only in framing *should* be similar, and pretending otherwise would
be gaming our own metric. What must not happen is two cases at ~1.0, which would mean a
genuine duplicate inflating a score.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haat.schema import Corpus, load_corpus

__all__ = ["DiversityReport", "analyse"]

NEAR_DUPLICATE = 0.90


@dataclass(slots=True)
class DiversityReport:
    total: int
    families: dict[str, int] = field(default_factory=dict)
    vectors: dict[str, int] = field(default_factory=dict)
    target_skus: dict[str, int] = field(default_factory=dict)
    violations: dict[str, int] = field(default_factory=dict)
    mean_similarity: float = 0.0
    p95_similarity: float = 0.0
    max_similarity: float = 0.0
    near_duplicate_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    near_duplicates_within_seed: int = 0
    near_duplicates_across_seeds: int = 0
    exact_duplicates: list[tuple[str, str]] = field(default_factory=list)
    unique_payloads: int = 0
    mean_length: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "unique_payloads": self.unique_payloads,
            "mean_payload_length": round(self.mean_length, 1),
            "families": self.families,
            "vectors": self.vectors,
            "target_skus": self.target_skus,
            "expected_violations": self.violations,
            "lexical_similarity": {
                "mean": round(self.mean_similarity, 4),
                "p95": round(self.p95_similarity, 4),
                "max": round(self.max_similarity, 4),
                "near_duplicate_threshold": NEAR_DUPLICATE,
                "near_duplicate_pairs": len(self.near_duplicate_pairs),
                "near_duplicates_within_seed": self.near_duplicates_within_seed,
                "near_duplicates_across_seeds": self.near_duplicates_across_seeds,
            },
            "exact_duplicates": len(self.exact_duplicates),
        }

    def markdown(self) -> str:
        lines = [
            "### Corpus diversity",
            "",
            f"- **{self.total}** attack cases, **{self.unique_payloads}** distinct payloads",
            f"- mean payload length **{self.mean_length:.0f}** characters",
            f"- pairwise lexical similarity: mean **{self.mean_similarity:.3f}**, "
            f"p95 **{self.p95_similarity:.3f}**, max **{self.max_similarity:.3f}**",
            f"- pairs above {NEAR_DUPLICATE:.2f} cosine: "
            f"**{len(self.near_duplicate_pairs)}** "
            f"({self.near_duplicates_within_seed} same-seed, "
            f"{self.near_duplicates_across_seeds} cross-seed)",
            f"- exact duplicate payloads: **{len(self.exact_duplicates)}**",
            "",
            "Same-seed near-duplicates are expected and are not a defect: one seed is",
            "deliberately re-framed several ways, and two framings of one payload should",
            "look alike. **Cross-seed** near-duplicates are the number that matters -- each",
            "one would mean two nominally distinct attacks are really the same attack, and",
            "the corpus is smaller than it claims.",
            "",
            "| Family | Cases |",
            "| --- | ---: |",
        ]
        lines.extend(f"| {name} | {count} |" for name, count in sorted(self.families.items()))
        lines += ["", "| Vector | Cases |", "| --- | ---: |"]
        lines.extend(f"| {name} | {count} |" for name, count in sorted(self.vectors.items()))
        return "\n".join(lines)


def analyse(corpus: Corpus | None = None) -> DiversityReport:
    corpus = corpus or load_corpus()
    cases = list(corpus.attacks)
    payloads = [c.payload for c in cases]

    report = DiversityReport(total=len(cases))
    report.families = dict(Counter(str(c.family) for c in cases))
    report.vectors = dict(Counter(str(c.vector) for c in cases))
    report.target_skus = dict(Counter(c.target_sku for c in cases))
    report.violations = dict(Counter(c.expected_violation for c in cases))
    report.unique_payloads = len(set(payloads))
    report.mean_length = sum(len(p) for p in payloads) / max(1, len(payloads))

    seen: dict[str, str] = {}
    for case in cases:
        if case.payload in seen:
            report.exact_duplicates.append((seen[case.payload], case.case_id))
        else:
            seen[case.payload] = case.case_id

    if len(payloads) > 1:
        _lexical(payloads, cases, report)

    return report


def _lexical(payloads: list[str], cases: list[Any], report: DiversityReport) -> None:
    """Character n-gram TF-IDF cosine similarity over every pair.

    Character n-grams rather than words: the payloads are short, share a small vocabulary,
    and a word-level view would call two entirely different social framings identical
    simply because both mention a SKU.
    """
    try:
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:  # pragma: no cover - optional extra
        return

    matrix = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1).fit_transform(
        payloads
    )
    similarity = (matrix @ matrix.T).toarray()
    np.fill_diagonal(similarity, 0.0)

    upper = similarity[np.triu_indices_from(similarity, k=1)]
    report.mean_similarity = float(upper.mean())
    report.p95_similarity = float(np.percentile(upper, 95))
    report.max_similarity = float(upper.max())

    rows, cols = np.where(np.triu(similarity, k=1) >= NEAR_DUPLICATE)
    report.near_duplicate_pairs = [
        (cases[int(i)].case_id, cases[int(j)].case_id, round(float(similarity[i, j]), 4))
        for i, j in zip(rows, cols, strict=True)
    ]

    # A case id is "<family>.<seed index>.<framing>". Two cases sharing the first two
    # segments are two framings of one authored payload, and their similarity is by
    # design. Anything else sharing 0.9 cosine is two attacks that are secretly one.
    for left, right, _score in report.near_duplicate_pairs:
        same_seed = left.rsplit(".", 1)[0] == right.rsplit(".", 1)[0]
        if same_seed:
            report.near_duplicates_within_seed += 1
        else:
            report.near_duplicates_across_seeds += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m haat.diversity")
    parser.add_argument("--root", default="haat")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument("--out", type=Path, help="write the report to a file")
    args = parser.parse_args(argv)

    report = analyse(load_corpus(args.root))
    text = json.dumps(report.to_dict(), indent=2) if args.json else report.markdown()

    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)

    if report.exact_duplicates:
        print(f"\nWARNING: {len(report.exact_duplicates)} exact duplicate payload(s)")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
