#!/usr/bin/env python3
"""
experiments/score_corpus.py
===========================

Does a discriminative decision model actually do the routing job?

Scores every candidate in `corpus.json` with a local winnow model via ollaya,
then reports discrimination (AUC), per-bucket score spread, precision at high
recall, and wall-clock latency. Every number printed here is computed from a
real run; nothing is estimated.

Usage::

    python experiments/score_corpus.py                 # default model + schema
    python experiments/score_corpus.py --model laya:en # cheaper decision model
    python experiments/score_corpus.py --repeat 3      # latency distribution

Requires `ollaya serve` to be running (started automatically if not).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.json"
SCHEMA = HERE / "worth-interrupting.json"
RESULTS = HERE / "results"

OLLAMA_API = "http://127.0.0.1:11434"
OLLAYA_PORT = 11435


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def auc(scores: list[float], labels: list[int]) -> float:
    """
    ROC AUC via the rank-sum method, with proper handling of ties.

    AUC = P(score(positive) > score(negative)) + 0.5 * P(tie).
    """
    pairs = [(s, l) for s, l in zip(scores, labels) if l in (0, 1)]
    pos = [s for s, l in pairs if l == 1]
    neg = [s for s, l in pairs if l == 0]
    if not pos or not neg:
        return float("nan")
    wins = ties = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def precision_at_recall(scores: list[float], labels: list[int],
                        min_recall: float = 0.90) -> tuple[float, float]:
    """
    Precision achievable while still covering `min_recall` of the positives.

    Sweeps a threshold down over the ranked list and returns the highest
    precision seen at or above the required recall.
    """
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    total_pos = sum(labels)
    if total_pos == 0:
        return float("nan"), float("nan")
    seen_pos = seen_all = 0
    best_p, at_t = 0.0, scores[order[0]]
    for i in order:
        seen_all += 1
        if labels[i] == 1:
            seen_pos += 1
        recall = seen_pos / total_pos
        if recall >= min_recall:
            best_p = max(best_p, seen_pos / seen_all)
            at_t = scores[i]
    return best_p, at_t


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def ollaya_up() -> bool:
    for url in (f"http://127.0.0.1:{OLLAYA_PORT}/api/decide",
                f"{OLLAYA_API}/v1/models"):
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            continue
    return False


def decide(model: str, schema: dict, state: str, timeout: float = 120.0) -> dict:
    """One decision call, preferring the HTTP API and falling back to the CLI."""
    payload = json.dumps({"model": model, "questions": schema, "state": state})
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{OLLAYA_PORT}/api/decide",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError):
        pass

    # CLI fallback: schema must live on disk for --questions.
    schema_path = RESULTS / "schema-temp.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(schema, indent=2))
    proc = subprocess.run(
        ["ollaya", "run", model, "--questions", str(schema_path),
         "--format", "json", "--keepalive", "10m"],
        input=state, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ollaya failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="winnow:e4b")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--schema", default=str(SCHEMA))
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat the whole pass to measure warm latency")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    items = json.loads(Path(args.corpus).read_text())
    schema = json.loads(Path(args.schema).read_text())
    if args.limit:
        items = items[: args.limit]
    RESULTS.mkdir(parents=True, exist_ok=True)

    print(f"model  : {args.model}")
    print(f"corpus : {len(items)} candidates "
          f"({sum(1 for i in items if i['truth'] == 2)} bucket-2 positives, "
          f"base rate {sum(1 for i in items if i['truth'] == 2)/len(items):.3f})")
    print(f"schema : {Path(args.schema).name}\n")

    rows: list[dict] = []
    latencies: list[float] = []
    score_key = next(iter(schema))

    for n, item in enumerate(items, 1):
        state = f"request: {item['text']}"
        started = time.monotonic()
        try:
            resp = decide(args.model, schema, state)
        except Exception as exc:
            print(f"  [{n:2}/{len(items)}] ERROR {item['text'][:40]!r}: {exc}")
            continue
        elapsed = time.monotonic() - started
        latencies.append(elapsed)

        ans = (resp.get("answers") or {}).get(score_key) or {}
        score = ans.get("score")
        probs = ans.get("probabilities") or {}
        rows.append({
            "id": item["id"],
            "text": item["text"],
            "truth": item["truth"],
            "score": score,
            "confidence": ans.get("confidence"),
            "probs": probs,
            "latency_s": round(elapsed, 3),
        })
        print(f"  [{n:2}/{len(items)}] truth={item['truth']} "
              f"score={score} ({elapsed:.2f}s)  {item['text'][:52]}")

    if not rows:
        print("\nno successful decisions — nothing to report")
        return 1

    scores = [r["score"] or 0.0 for r in rows]

    print("\n" + "=" * 68)
    print("DISCRIMINATION")
    print("=" * 68)

    binary = [1 if r["truth"] == 2 else 0 for r in rows]
    a_binary = auc(scores, binary)
    notnoise = [1 if r["truth"] >= 1 else 0 for r in rows]
    a_notnoise = auc(scores, notnoise)

    print(f"  AUC  bucket2 vs rest      : {a_binary:.4f}")
    print(f"  AUC  (bucket>=1) vs noise : {a_notnoise:.4f}")
    print(f"  AUC  three-class          : "
          f"{auc(scores, [r['truth'] for r in rows]):.4f}  (informational only)")

    for bucket in (0, 1, 2):
        vals = [r["score"] for r in rows if r["truth"] == bucket and r["score"] is not None]
        if vals:
            print(f"  bucket {bucket}: n={len(vals):2d}  "
                  f"min={min(vals):.4f}  median={statistics.median(vals):.4f}  "
                  f"max={max(vals):.4f}")

    prec, thr = precision_at_recall(scores, binary, 0.90)
    print(f"\n  precision @ recall>=0.90 : {prec:.3f}  (threshold {thr:.4f})")

    print("\n" + "=" * 68)
    print("LATENCY")
    print("=" * 68)
    print(f"  first call (cold) : {latencies[0]:.3f}s")
    if len(latencies) > 1:
        warm = latencies[1:]
        print(f"  warm median       : {statistics.median(warm):.3f}s")
        print(f"  warm min/max      : {min(warm):.3f}s / {max(warm):.3f}s")
    print(f"  total for {len(latencies)} calls : {sum(latencies):.1f}s")

    print("\n" + "=" * 68)
    print("DECEPTIVE CASES")
    print("=" * 68)
    traps = [r for r in rows if any(t in r["text"] for t in
             ("!!!", "no rush", "nothing on fire", "everything is fine",
              "Heads up", "quick one", "minor:", "FYI your password"))]
    for r in traps:
        verdict = "OK" if (
            (r["score"] >= 1.4 and r["truth"] == 2) or
            (r["score"] < 1.4 and r["truth"] != 2)
        ) else "** FAILED **"
        print(f"  [{verdict}] truth={r['truth']} score={r['score']}  {r['text'][:50]}")

    out = RESULTS / "scores.json"
    out.write_text(json.dumps({"model": args.model, "rows": rows}, indent=2))
    print(f"\nraw scores -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())