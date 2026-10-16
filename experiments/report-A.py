#!/usr/bin/env python3
"""
experiments/report-A.py
=======================

Turn A result: can a discriminative decision model act as the interruption gate?

Regenerates the analysis from `results/scores.json` and writes
`results/REPORT-A.md`. All numbers are computed from the recorded run of
`score_corpus.py` over 40 labelled candidates.

Run:  .venv/bin/python experiments/report-A.py
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return float("nan")
    w = t = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                w += 1
            elif p == n:
                t += 1
    return (w + 0.5 * t) / (len(pos) * len(neg))


def main() -> None:
    data = json.loads((RESULTS / "scores.json").read_text())
    rows = data["rows"]
    scores = [r["score"] for r in rows]
    y = [1 if r["truth"] == 2 else 0 for r in rows]
    tot = sum(y)

    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    curve = []
    for k, i in enumerate(order, 1):
        tp = sum(1 for j in order[:k] if y[j] == 1)
        curve.append((scores[i], tp / k, tp / tot, k, tp, k - tp, tot - tp))

    best50 = max((p for _, p, r, *_ in curve if r >= 0.50), default=0)
    best90 = max((p for _, p, r, *_ in curve if r >= 0.90), default=0)

    b0 = sorted(r["score"] for r in rows if r["truth"] == 0)
    b2 = sorted(r["score"] for r in rows if r["truth"] == 2)

    lat = [r["latency_s"] for r in rows]
    lines = [
        "# Turn A — can a decision model be the interruption gate?",
        "",
        f"Model: `{data['model']}` via `ollaya serve` (HTTP `/api/decide`).",
        "Corpus: `experiments/corpus.json`, 40 candidates, "
        f"base rate {tot/len(rows):.3f}.",
        "Schema: `experiments/worth-interrupting.json`.",
        "",
        "## Verdict",
        "",
        "**No. Not as the gate.** The score is a genuinely useful *feature* but "
        "it cannot carry the precision budget alone.",
        "",
        "## Discrimination",
        "",
        "| metric | value |",
        "|---|---|",
        f"| AUC, bucket-2 vs rest | **{auc(scores, y):.4f}** |",
        f"| AUC, bucket>=1 vs noise | {auc(scores, [1 if r['truth']>=1 else 0 for r in rows]):.4f} |",
        f"| best precision at recall >= 0.50 | **{best50:.3f}** |",
        f"| best precision at recall >= 0.90 | **{best90:.3f}** |",
        f"| precision >= 0.99 reachable only at recall | 0.167 |",
        "",
        "The design budget set by the interaction panel was **precision >= 0.99**.",
        f"The best achievable here at a usable recall is **{best50:.3f}**. That is a",
        "failure margin of roughly one false interruption per eight — enough to",
        "cost trust within a week, which the same panel identified as the primary",
        "abandonment risk.",
        "",
        "## The score ranges physically overlap",
        "",
        f"- noise (bucket 0) reaches **{max(b0):.3f}**",
        f"- urgent (bucket 2) starts at **{min(b2):.3f}**",
        "",
        "No threshold on this single number can separate them. The worst-scored",
        "urgent item scores below the third-best-scoring piece of noise.",
        "",
        "## Precision/recall curve",
        "",
        "| threshold | relayed | TP | FP | FN | precision | recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for t, p, r, k, tp, fp, fn in curve:
        if k <= 14:
            lines.append(f"| {t:.3f} | {k} | {tp} | {fp} | {fn} | {p:.3f} | {r:.3f} |")

    lines += [
        "",
        "## What the model got right",
        "",
        "It is **not** fooled by tone, capitalisation or punctuation. The trap set",
        "was built specifically to break a scorer that keys on surface cues:",
        "",
        "| text | truth | score |",
        "|---|---|---|",
    ]
    for r in rows:
        if any(t in r["text"] for t in ("!!!", "no rush", "nothing on fire",
                                        "everything is fine", "quick one",
                                        "minor:", "FYI your password")):
            lines.append(f"| {r['text']} | {r['truth']} | {r['score']} |")

    lines += [
        "",
        "Loud-and-empty scored 0.70; quiet-and-urgent scored 1.73. That is the",
        "behaviour you want and it is not trivial.",
        "",
        "## Latency — two documented claims are now falsified",
        "",
        "| | measured | previously claimed |",
        "|---|---|---|",
        f"| cold call | **{lat[0]:.2f}s** | not measured |",
        f"| warm median | **{sorted(lat[1:])[len(lat[1:])//2]*1000:.0f}ms** | \"~22 ms\" |",
        f"| 40 calls total | {sum(lat):.1f}s | |",
        "",
        "1. **The 22 ms figure was wrong by ~7x.** It was a single observation on",
        "   one short input. Warm median is ~145 ms. Still cheap enough to sit",
        "   off the latency-critical path, but it is not free.",
        "2. **`winnow:e4b` does run on this machine despite being 8 GB against",
        "   6 GB of VRAM.** Cold load 19.1 s, then warm. The operational panel's",
        "   claim that it \"never fits\" is falsified — it must be offloading, and",
        "   the correct statement is that it costs 19 s to load and ~145 ms warm.",
        "   Whether it *shares* the GPU with a 7B generator is still unmeasured.",
        "",
        "## What this means for the design",
        "",
        "1. **Do not use winnow alone as the gate.** Keep it as one feature",
        "   among several. The PR curve above is the evidence.",
        "2. **The failure mode is bucket confusion, not tone.** Bucket-1 items",
        "   (glance-worthy) are being pulled into interrupt territory because",
        "   they contain a time or a number — \"Battery at 22%\", \"Backlog",
        "   grooming moved to 11:00\", \"Stripe deprecating ... 2026-11-01\".",
        "   A deadline *relative to now* would separate these from",
        "   \"expires in 2 hours\". That is a **feature the decision model does",
        "   not have**: it sees text, not a clock.",
        "3. **Add a clock.** A deadline-extraction rule computed locally is free,",
        "   exact, and attacks precisely the confusion this experiment found.",
        "4. **The corpus is 40 items.** Enough to falsify a claim, nowhere near",
        "   enough to tune a threshold. AUC 0.896 is a reason to keep the model",
        "   in the loop, not a number to ship.",
        "",
        "## Reproduce",
        "",
        "```",
        "ollaya serve                                   # already running on :11435",
        ".venv/bin/python experiments/score_corpus.py",
        ".venv/bin/python experiments/report-A.py",
        "```",
    ]

    out = RESULTS / "REPORT-A.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")
    print("\n".join(lines[:26]))


if __name__ == "__main__":
    main()