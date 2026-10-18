"""
Does a newer Jev generation actually score better, or is that the dice?

Run this because the obvious version of the question -- run the corpus once on
each model and compare the two AUCs -- cannot answer it. Measured on this corpus
on this account, the SAME model over FIVE runs gives:

    AUC  0.913  0.917  0.921  0.921  0.940
    ECE  0.355  0.360  0.362  0.362  0.366

That is a spread of 0.027 AUC with nothing changed: same model, same corpus,
same code. So a single-run A/B that reports "1.14.0 beats 1.13.0 by 0.02" is
reporting a difference smaller than its own measurement error, and would be
indistinguishable from re-rolling.

This measures the noise floor first, then asks whether the between-model gap
clears it. The verdict is deliberately hard to earn: overlapping ranges are
reported as INDISTINGUISHABLE rather than resolved by picking the larger mean.
A model comparison that cannot clear the noise is the most expensive kind of
wrong in this repository -- it reads as evidence about the system and is
actually evidence about the sampler.

THE ALIAS TRAP. `jev.DEFAULT_MODEL` is the floating string "jev-latest". On this
account (measured 2026-10-17) it resolves to jev-1.13.0, while "jev-preview"
resolves to jev-1.14.0 -- the alias named "latest" is BEHIND the preview. Every
model below is therefore named explicitly, never by alias, and the resolved
build is printed alongside the request so a silent server-side swap is visible.

Degenerates to a skip (exit 0) when no key is configured.

Run: ``.venv/bin/python experiments/jev_model_ab.py [--reps N]``
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jev                                        # noqa: E402
from experiments.jev_gate import calibration_error  # noqa: E402
from experiments.score_corpus import auc            # noqa: E402

CORPUS = ROOT / "experiments" / "corpus.json"

MODELS = ["jev-1.13.0", "jev-1.14.0"]


def one_rep(texts: list[str], labels: list[int], model: str,
            client) -> tuple[float, float, str]:
    """Score the whole corpus once. Returns (auc, ece, resolved_model)."""
    scores: list[float] = []
    resolved: set[str] = set()
    for t in texts:
        v = jev.worth_interrupting(t, model=model, client=client)
        if v is None:
            continue
        scores.append(v.worth_interrupting)
        resolved.add(v.model)
    if len(scores) != len(texts):
        # A short corpus means dropped calls, and the AUC would be computed
        # over a silently different item set than every other rep.
        raise RuntimeError(
            f"{len(texts) - len(scores)} call(s) returned nothing; refusing "
            f"to compare runs over different item sets")
    if len(resolved) != 1:
        raise RuntimeError(f"mixed models in one rep: {sorted(resolved)}")
    ece, _ = calibration_error(scores, labels)
    return auc(scores, labels), ece, resolved.pop()


def spread(xs: list[float]) -> tuple[float, float]:
    return min(xs), max(xs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5,
                    help="repetitions per model (default 5)")
    args = ap.parse_args()

    items = json.loads(CORPUS.read_text())
    texts = [it["text"] for it in items]
    # Same binarisation as jev_gate.py: queue-worthy is truth >= 1. Using a
    # different one here would make the two harnesses incomparable.
    labels = [1 if int(it["truth"]) >= 1 else 0 for it in items]

    if not jev.available():
        print("\nSKIP  no TYPESAFE_API_KEY or typesafe-sdk not installed")
        return 0

    client = jev._client()
    results: dict[str, dict[str, list[float]]] = {}
    resolved_for: dict[str, str] = {}

    print(f"\ncorpus: {len(items)} items, {args.reps} reps per model\n")
    for model in MODELS:
        aucs: list[float] = []
        eces: list[float] = []
        print(f"  {model}")
        for r in range(1, args.reps + 1):
            try:
                a, e, resolved = one_rep(texts, labels, model, client)
            except Exception as exc:                      # noqa: BLE001
                print(f"    rep {r}: SKIP ({type(exc).__name__}: {exc})")
                continue
            aucs.append(a)
            eces.append(e)
            resolved_for[model] = resolved
            print(f"    rep {r}:  AUC {a:.3f}   ECE {e:.3f}")
        if not aucs:
            print(f"    no usable reps for {model}")
            return 1
        results[model] = {"auc": aucs, "ece": eces}

    print(f"\n{'=' * 70}")
    print(f"{'model':<14}{'resolved':<14}{'AUC mean':>10}{'AUC range':>16}"
          f"{'ECE mean':>10}")
    print(f"{'-' * 70}")
    for model in MODELS:
        r = results.get(model)
        if not r:
            continue
        lo, hi = spread(r["auc"])
        print(f"{model:<14}{resolved_for[model]:<14}"
              f"{statistics.mean(r['auc']):>10.3f}"
              f"{f'{lo:.3f}-{hi:.3f}':>16}"
              f"{statistics.mean(r['ece']):>10.3f}")

    if len(results) < 2:
        print("\n  only one model produced usable reps -- nothing to compare.")
        return 0

    a_name, b_name = MODELS
    a_auc, b_auc = results[a_name]["auc"], results[b_name]["auc"]
    a_lo, a_hi = spread(a_auc)
    b_lo, b_hi = spread(b_auc)
    gap = statistics.mean(b_auc) - statistics.mean(a_auc)

    # Both metrics are judged, not just AUC. jev_gate.py calls calibration
    # "the claim that matters" -- a well-separated but badly calibrated score
    # can rank but cannot authorise an interrupt -- so a verdict reading only
    # AUC would quietly discard the metric the project already named as
    # decisive, and would call a real calibration win indistinguishable.
    a_ece, b_ece = results[a_name]["ece"], results[b_name]["ece"]
    a_elo, a_ehi = spread(a_ece)
    b_elo, b_ehi = spread(b_ece)
    ece_gap = statistics.mean(b_ece) - statistics.mean(a_ece)
    auc_overlap = not (b_lo > a_hi or a_lo > b_hi)
    ece_overlap = not (b_elo > a_ehi or a_elo > b_ehi)

    def verdict(ov: bool) -> str:
        return "INDISTINGUISHABLE" if ov else "SEPARATED"

    print(f"\n{'=' * 70}")
    print(f"{'metric':<6}{a_name + ' range':>22}{b_name + ' range':>22}"
          f"{'gap':>9}  verdict")
    print(f"{'-' * 70}")
    print(f"{'AUC':<6}{f'{a_lo:.3f}-{a_hi:.3f}':>22}"
          f"{f'{b_lo:.3f}-{b_hi:.3f}':>22}{gap:>+9.3f}  {verdict(auc_overlap)}")
    print(f"{'ECE':<6}{f'{a_elo:.3f}-{a_ehi:.3f}':>22}"
          f"{f'{b_elo:.3f}-{b_ehi:.3f}':>22}{ece_gap:>+9.3f}  {verdict(ece_overlap)}")
    print(f"\n  gap is ({b_name} - {a_name}); negative ECE is better.")

    if auc_overlap and ece_overlap:
        print("\n  VERDICT  INDISTINGUISHABLE at this n.")
        print("  Both metrics' ranges overlap, so the between-model gap is")
        print("  smaller than the within-model spread of a SINGLE model. More")
        print("  reps before claiming a winner -- a difference this size is")
        print("  what re-rolling the sampler looks like.")
        return 0

    # The separated metric is the one whose ranges do NOT overlap.
    ece_won = not ece_overlap
    auc_won = not auc_overlap
    overlapping = [n for n, ov in (("AUC", auc_overlap), ("ECE", ece_overlap)) if ov]
    metric = "calibration" if ece_won else "separation"
    if not overlapping:
        metric = "both calibration and separation"
    if ece_won:
        winner = b_name if statistics.mean(b_ece) < statistics.mean(a_ece) else a_name
    else:
        winner = b_name if statistics.mean(b_auc) > statistics.mean(a_auc) else a_name
    tail = f"; {' and '.join(overlapping)} still overlapping." if overlapping else "."
    print(f"\n  VERDICT  {winner} SEPARATED on {metric}{tail}")
    print("  Ranges do not overlap, so this is not a re-roll. It is still")
    print(f"  n={len(b_auc)} reps over {len(texts)} items -- enough to reject")
    print("  'no difference', not enough to call the new model better overall.")
    if auc_overlap and ece_overlap is False:
        print("\n  Note the split. Separation is not yet distinguishable while")
        print("  calibration clearly is. For THIS project calibration is the")
        print("  question that decides whether a score may authorise an")
        print("  interrupt, and ranking is not what is being asked. But it is")
        print("  one number moving against another, so it is reported as two")
        print("  findings, not one verdict.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())