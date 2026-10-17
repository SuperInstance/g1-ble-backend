"""
Jev vs the existing gate, on the corpus that already exists.

This is the experiment `policy.py` has been waiting for. That module's
`interrupt_gate` axis is stuck on `rules` because:

    0.99**n <= 0.05 needs n >= 299 clean pushes, ~75 days at the 4/day cap.

That arithmetic is correct *for a model that only emits decisions*. Jev emits a
calibrated probability for a closed-set question, so the same target becomes a
threshold comparison rather than an accumulation. The claim to test is
therefore specific and falsifiable:

  H1  Jev separates worth-interrupting from noise at least as well as the
      regex baseline, which scored 0.688 on this exact corpus.
  H2  Its probabilities are CALIBRATED -- a 0.9 prediction is right ~90% of
      the time. This is the whole argument for using it, and it is the claim
      no outside party has independently tested.
  H3  It is fast enough to sit on the delivery path.

H2 is the one that matters. A well-separated but badly calibrated score still
cannot authorise an interrupt; it can only rank. Both are reported, and they
are different claims.

Degenerates to a skip (exit 0) when the key is absent or does not authenticate,
so this is safe to run anywhere.

Run: ``.venv/bin/python experiments/jev_gate.py``
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jev                                   # noqa: E402
from experiments.score_corpus import auc, precision_at_recall   # noqa: E402

CORPUS = ROOT / "experiments" / "corpus.json"


def calibration_error(scores: list[float], labels: list[int],
                      bins: int = 5) -> tuple[float, list[dict]]:
    """
    Expected calibration error, plus the bins so the shape is visible.

    ECE alone hides whether the model is over- or under-confident; a caller
    choosing an interrupt threshold needs to know which way to move it.
    """
    buckets: list[dict] = [
        {"n": 0, "conf_sum": 0.0, "acc_sum": 0.0, "lo": i / bins, "hi": (i + 1) / bins}
        for i in range(bins)
    ]
    for s, y in zip(scores, labels):
        b = min(bins - 1, max(0, int(s * bins)))
        buckets[b]["n"] += 1
        buckets[b]["conf_sum"] += s
        buckets[b]["acc_sum"] += y
    total = len(scores) or 1
    ece = 0.0
    for b in buckets:
        if b["n"]:
            b["conf"] = b["conf_sum"] / b["n"]
            b["acc"] = b["acc_sum"] / b["n"]
            b["gap"] = b["conf"] - b["acc"]
            ece += (b["n"] / total) * abs(b["gap"])
        else:
            b["conf"] = b["acc"] = b["gap"] = None
    return ece, buckets


def main() -> int:
    items = json.loads(CORPUS.read_text())
    texts = [it["text"] for it in items]

    # corpus.json carries an ORDINAL truth (0 ignorable / 1 glance / 2 read
    # now), not a binary label. score_corpus.py binarises it two ways and
    # reports both, and this harness must use the same definitions or the
    # numbers are not comparable to the 0.688 / 0.958 baselines:
    #   interrupt   : truth == 2
    #   queue_worthy: truth >= 1
    interrupt_labels = [1 if int(it["truth"]) == 2 else 0 for it in items]
    queue_labels = [1 if int(it["truth"]) >= 1 else 0 for it in items]
    labels = interrupt_labels
    print(f"corpus: {len(items)} items | truth ordinal "
          f"{sum(int(i['truth']) == 2 for i in items)}x2, "
          f"{sum(int(i['truth']) == 1 for i in items)}x1, "
          f"{sum(int(i['truth']) == 0 for i in items)}x0")
    print(f"  interrupt (truth==2): {sum(interrupt_labels)} pos / "
          f"{len(items) - sum(interrupt_labels)} neg")
    print(f"  queue-worth (>=1)   : {sum(queue_labels)} pos / "
          f"{len(items) - sum(queue_labels)} neg")

    if not jev.available():
        print("\nSKIP  no TYPESAFE_API_KEY or typesafe-sdk not installed")
        return 0

    scores: list[float] = []
    latencies: list[float] = []
    skipped = 0
    client = None
    try:
        client = jev._client()
    except Exception as exc:                  # noqa: BLE001
        print(f"\nSKIP  could not build client: {type(exc).__name__}: {exc}")
        return 0

    print("\nscoring ...")
    for i, text in enumerate(texts, 1):
        v = jev.worth_interrupting(text, client=client)
        if v is None:
            skipped += 1
            continue
        scores.append(v.worth_interrupting)
        latencies.append(v.latency_ms)
        if i <= 5:
            print(f"  {i:>2}  p={v.worth_interrupting:.3f}  "
                  f"conf={v.confidence:.3f}  {v.latency_ms:>5.0f}ms  {text[:38]!r}")

    if skipped:
        print(f"\n  {skipped} call(s) returned nothing (auth or network)")
    if not scores:
        print("\nSKIP  no usable responses -- check the key")
        return 0

    used_i = interrupt_labels[: len(scores)]
    used_q = queue_labels[: len(scores)]
    print(f"\n{'=' * 66}")
    print(f"H1  separation")
    print(f"    AUC queue-worthy {auc(scores, used_q):.3f}   (regex 0.688, "
          f"winnow 0.813)")
    print(f"    AUC interrupt    {auc(scores, used_i):.3f}   (winnow 0.958)")
    for recall in (0.90, 0.95):
        p, t = precision_at_recall(scores, used_q, min_recall=recall)
        print(f"    precision @ {recall:.2f} recall  {p:.3f}  (tau {t:.3f})")

    print(f"\nH2  calibration  <-- the claim that matters")
    ece, buckets = calibration_error(scores, used_q)
    print(f"    ECE          {ece:.3f}   (0 = perfectly calibrated)")
    print(f"    {'bin':<12}{'n':>4}{'confidence':>12}{'accuracy':>11}{'gap':>8}")
    for b in buckets:
        if not b["n"]:
            continue
        print(f"    {b['lo']:.1f}-{b['hi']:.1f}    {b['n']:>4}"
              f"{b['conf']:>12.3f}{b['acc']:>11.3f}{b['gap']:>+8.3f}")

    print(f"\nH3  latency  <-- does it fit on the delivery path")
    print(f"    median {statistics.median(latencies):.0f} ms   "
          f"mean {statistics.mean(latencies):.0f} ms   "
          f"max {max(latencies):.0f} ms   n={len(latencies)}")

    print("\nread")
    a = auc(scores, used_q)
    if a >= 0.688:
        print(f"    separation clears the regex baseline ({a:.3f} >= 0.688)")
    else:
        print(f"    separation BELOW the regex baseline ({a:.3f} < 0.688)")
    print(f"    calibration is the decisive number. ECE {ece:.3f} with "
          f"{len(scores)} points is")
    print(f"    far too small to establish a 0.99 interrupt target -- that needs")
    print(f"    ~300 clean pushes no matter which model answers. This measures")
    print(f"    ranking quality and rough calibration; it does NOT retire the")
    print(f"    299-push requirement, and claiming otherwise would repeat the")
    print(f"    error this project has already made three times.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())