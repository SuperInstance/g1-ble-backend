"""
Does the interest signal survive contact with the wearer?

``sim/ledger.py`` already recorded a verdict on keyword weighting -- "spread
signal too thin to ever decide anything" -- and ``SOAK.md`` found 0 axes ever
flipping. The prompt-injection path in ``pipeline.py`` is built on the opposite
assumption, so the substrate underneath it needs to be measured rather than
asserted.

Four properties, each of which the previous scorer failed:

  1. SATURATION  four taps on one card must not pin a topic at MAX_WEIGHT.
                 Measured before: 1 tap -> 1.50, 4 taps -> 3.00 (pinned).
  2. EVIDENCE    one card is not an interest. ``interests()`` must refuse a
                 topic that has only ever appeared on a single card.
  3. RANKING     ordering must be by measured interest, not alphabetical.
                 Measured before: six terms tied at 3.0, so the sort fell
                 through to ``(term)`` and produced alphabetical noise.
  4. SILENCE     a term nobody reacted to sits at neutral with zero evidence,
                 and must never outrank one that was.

Run: ``.venv/bin/python experiments/learning_substrate.py``
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import (  # noqa: E402
    MAX_WEIGHT,
    MIN_WEIGHT,
    MIN_CARDS_FOR_INTEREST,
    NEUTRAL,
    FeedbackStore,
)


def card(store: FeedbackStore, text: str, keywords: list[str]) -> int:
    return store.add_card(
        query=text, stage1="answer " * 40, summary=text, keywords=keywords,
    ).id


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        store = FeedbackStore(Path(tmp) / "learn.db")

        # -- 1. saturation --------------------------------------------------
        c1 = card(store, "quantum tunnelling recap", ["quantum", "tunnelling"])
        for _ in range(4):
            store.add_event(c1, "expand")
        w = store.topic_evidence()
        q = w["quantum"]
        print(f"[1] 4 expands, 1 card      -> weight {q['weight']:.3f} "
              f"cards={q['cards']} events={q['events']}")
        if q["weight"] >= MAX_WEIGHT:
            failures.append(f"saturated: {q['weight']:.3f} == MAX_WEIGHT after 4 taps")

        # -- 2. evidence gate ----------------------------------------------
        lone = [c["topic"] for c in store.interests()]
        print(f"[2] interests from 1 card  -> {lone or 'none'} (want none)")
        if "quantum" in lone:
            failures.append("a single card was promoted to an interest")

        # -- 3. ranking + compounding across distinct cards ----------------
        # photon is starred on 3 cards, stripe on 2, mango confirmed on 2.
        # Different evidence on purpose: two topics with identical counts are
        # genuinely indistinguishable, and alphabetical order is then the
        # correct answer rather than a ranking defect.
        for n in range(3):
            cid = card(store, f"photon optics part {n}", ["photon", "stripe"])
            store.add_event(cid, "star")          # the strongest reaction
        for n in range(2):
            cid = card(store, f"stripe polariser {n}", ["stripe"])
            store.add_event(cid, "star")
        for n in range(2):
            cid = card(store, f"mango cultivar {n}", ["mango"])
            store.add_event(cid, "confirm")

        w = store.topic_evidence()
        print("[3] evidence after repeat")
        for t in ("quantum", "photon", "stripe", "mango"):
            if t in w:
                print(f"      {t:10s} w={w[t]['weight']:.3f} "
                      f"cards={w[t]['cards']} events={w[t]['events']}")

        top_w = max(v["weight"] for v in w.values())
        print(f"[3] ceiling reached?      -> {top_w:.3f} vs MAX {MAX_WEIGHT}")
        if top_w >= MAX_WEIGHT:
            failures.append(f"still saturating: {top_w:.4f} >= MAX_WEIGHT")

        ranked = [c["topic"] for c in store.interests(limit=5)]
        print(f"[3] interests             -> {ranked}")
        if "stripe" not in ranked:
            failures.append("starred-on-3-cards topic missing from interests")
        if ranked[:1] != ["stripe"]:
            failures.append(f"ranking led with {ranked[:1]}, not the best-evidenced")
        if "quantum" in ranked:
            failures.append("1-card topic outranked multi-card topics")

        # -- 4. silence is neutral, and ranks last --------------------------
        card(store, "unrelated trivia", ["kumquat", "obelisk"])
        w2 = store.topic_evidence()
        unseen = {t: w2[t] for t in ("kumquat", "obelisk") if t in w2}
        print(f"[4] never reacted         -> {unseen}")
        for t, info in unseen.items():
            if info["weight"] != NEUTRAL or info["cards"] != 0:
                failures.append(f"silent term {t} reads as evidence: {info}")
        if [c["topic"] for c in store.interests(limit=5)] != ranked:
            failures.append("adding silent terms changed the interest ranking")

        # -- 5. negative evidence must be able to cancel --------------------
        #
        # Both of these were real ranking inversions, found by an independent
        # review. The first came from folding each card down to its single
        # strongest reaction, which silently deleted every dismiss on a card
        # that also drew one star.
        noisy = store.add_card(query="noisy", stage1="n" * 400,
                               summary="Noisy topic", keywords=["noisy"]).id
        store.add_event(noisy, "star")
        for _ in range(20):
            store.add_event(noisy, "dismiss")
        for n in range(2):
            c = card(store, f"noisy rest {n}", ["noisy"])
            store.add_event(c, "star")

        faint = store.add_card(query="faint", stage1="f" * 400,
                               summary="Faint topic", keywords=["faint"]).id
        store.add_event(faint, "confirm")
        store.add_event(faint, "dismiss")

        w3 = store.topic_evidence()
        print(f"[5] star+dismiss x20      -> w={w3['noisy']['weight']:.3f} "
              f"cards={w3['noisy']['cards']}")
        print(f"[5] confirm+dismiss      -> w={w3['faint']['weight']:.3f} "
              f"cards={w3['faint']['cards']}")

        for t in ("noisy", "faint"):
            if w3[t]["weight"] < MIN_WEIGHT or w3[t]["weight"] > MAX_WEIGHT:
                failures.append(f"{t} weight {w3[t]['weight']} is out of bounds")
            if t in [c["topic"] for c in store.interests(limit=10)]:
                failures.append(f"net-negative topic {t} is still an interest")

        # ordering is by weight, with card count only a tie-break
        order = [c["weight"] for c in store.interests(limit=10)]
        if order != sorted(order, reverse=True):
            failures.append(f"interests not ordered by weight: {order}")
        print(f"[5] interest order        -> {[c['topic'] for c in store.interests(limit=4)]}")

        # -- preferences() is empty until there is real evidence ------------
        fresh = FeedbackStore(Path(tmp) / "fresh.db")
        if fresh.preferences():
            failures.append("preferences() non-empty for a store with no history")
        print(f"[5] preferences, no history -> {fresh.preferences()!r} (want empty)")
        print(f"[5] preferences, w/ evidence-> {store.preferences()[:70]!r}...")
        if not store.preferences():
            failures.append("preferences() empty despite 4 cards of evidence")

        print(f"\n    (gate is {MIN_CARDS_FOR_INTEREST} distinct cards per topic)")

    print()
    for f in failures:
        print(f"FAIL  {f}")
    if not failures:
        print("OK    no saturation, evidence gate holds, ranking is measured, "
              "silence stays neutral")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())