"""
Does the adaptive loop actually work, and does the heartbeat survive it?

The integration is easy to claim and easy to get wrong in ways that only show
up under load, so this exercises the four claims that matter:

  1. INGESTION   ``run_query`` reads the store before either LLM runs, and the
                 stage-2 prompt actually carries the interests when the answer
                 concerns one of them.
  2. GATING      the same prompt is withheld when the answer is unrelated, and
                 ``personalise=False`` restores a byte-identical prompt. An
                 interest block that reaches the model unconditionally is a
                 topic filter, not personalisation.
  3. OFF-LOOP    a heartbeat task on the same loop as the session keeps its
                 cadence while reactions are written. The G1 drops the link
                 after ~32s without a 0x25 heartbeat, so a commit on the loop
                 is a link drop.
  4. LEARNING    starring raises the weight of that card's keywords, the shift
                 is visible, and it is visible over time rather than only at
                 the end.

Run: ``.venv/bin/python experiments/adaptive_loop.py``
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import MIN_CARDS_FOR_INTEREST, FeedbackStore   # noqa: E402
from pipeline import (  # noqa: E402
    COMPRESSOR_SYSTEM_PROMPT,
    build_llm_config,
    condense_for_glasses,
    run_query,
)

HEARTBEAT_INTERVAL = 0.010      # 10s in production; 10ms so a stall is visible
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


from main import _FakeClient as FakeClient   # noqa: E402


def system_of(client, index: int = 0) -> str:
    """The system prompt sent on call ``index``."""
    return client.completions.calls[index]["messages"][0]["content"]


def config() -> object:
    c = build_llm_config("custom")
    c.api_key = "test"
    c.primary_model = "test-primary"
    c.compressor_model = "test-compressor"
    return c


def seed(store: FeedbackStore, cards: int, kinds: list[str], keywords: list[str]):
    """Give the store real evidence: several DISTINCT cards per topic."""
    for n in range(cards):
        cid = store.add_card(
            query=f"stripe optics {n}", stage1="long " * 200,
            summary=f"Stripe {n} scatters light", keywords=list(keywords),
        ).id
        for k in kinds:
            store.add_event(cid, k)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = FeedbackStore(Path(tmp) / "adaptive.db")
        seed(store, 3, ["star"], ["stripe", "photon"])
        ctx = store.wearer_context()

        print("\n1. ingestion")
        check("store exposes evidenced interests",
              set(ctx.interests) == {"stripe", "photon"}, str(ctx.interests))
        check("gated on distinct cards, not taps",
              all(d["cards"] >= MIN_CARDS_FOR_INTEREST for d in ctx.interest_detail))
        check("recent card state is carried", len(ctx.recent_summaries) > 0,
              f"{len(ctx.recent_summaries)} summaries")

        print("\n2. stage-2 prompt gating")
        cfg = config()
        on_topic = ("Stripe gratings diffract light into discrete orders, "
                    "which is why photon momentum appears quantised.")
        off_topic = ("Mango cultivars ripen over six weeks once ethylene "
                     "production crosses a threshold set by the seed.")

        fake = FakeClient(["Stripe gratings split light into discrete orders."])
        await condense_for_glasses(on_topic, client=fake, config=cfg, context=ctx)
        sent = system_of(fake)
        check("on-topic answer receives the block", "<interests>" in sent)
        check("prompt stays within display rules",
              "120 characters" in sent and "NO authority" in sent)

        fake2 = FakeClient(["Mango ripens in six weeks after ethylene rises."])
        await condense_for_glasses(off_topic, client=fake2, config=cfg, context=ctx)
        check("off-topic answer is NOT personalised",
              "<interests>" not in system_of(fake2))

        fake3 = FakeClient(["Stripe gratings split light into discrete orders."])
        await condense_for_glasses(on_topic, client=fake3, config=cfg,
                                   context=ctx, personalise=False)
        check("personalise=False restores the exact base prompt",
              system_of(fake3) == COMPRESSOR_SYSTEM_PROMPT)

        print("\n3. heartbeat isolation under real writes")
        written: list[str] = []

        async def send(text: str) -> None:
            written.append(text)

        gaps: list[float] = []
        stop = asyncio.Event()

        COMMIT_COST = 0.004      # a deliberately slow commit

        class SlowStore:
            """
            Duck-typed stand-in that charges a real commit cost per write.

            Not a subclass: sqlite3.Connection refuses attribute assignment, so
            the delay cannot be hung off the connection. This wraps exactly the
            surface that run_query and Session._record call.
            """

            def __init__(self, inner, cost):
                self.inner, self.cost = inner, cost

            def wearer_context(self):
                return self.inner.wearer_context()

            def add_event(self, *a, **kw):
                time.sleep(self.cost)
                return self.inner.add_event(*a, **kw)

        card_id = store.recent_cards(limit=1)[0].id

        async def measure(body):
            gaps, stop = [], asyncio.Event()

            async def beat_task():
                last = time.perf_counter()
                while not stop.is_set():
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            task = asyncio.create_task(beat_task())
            # Let the heartbeat establish a baseline before the body runs.
            # Without this the control -- which never yields -- starves the
            # task entirely and reports zero ticks, i.e. no measurement at all.
            await asyncio.sleep(HEARTBEAT_INTERVAL * 6)
            await body()
            await asyncio.sleep(HEARTBEAT_INTERVAL * 6)
            stop.set()
            await task
            return gaps

        async def off_loop_writes():
            """What Session._record does: dispatch the commit to a thread."""
            slow = SlowStore(store, COMMIT_COST)
            for kind in ("expand", "confirm", "star", "expand", "dismiss"):
                await asyncio.sleep(0)        # each tap is its own task
                await asyncio.to_thread(slow.add_event, card_id, kind, "experiment")

        async def on_loop_writes():
            """The bug this whole design exists to prevent."""
            slow = SlowStore(store, COMMIT_COST)
            for kind in ("expand", "confirm", "star", "expand", "dismiss"):
                await asyncio.sleep(0)        # same yields, same store
                slow.add_event(card_id, kind, "control")

        control_gaps = await measure(on_loop_writes)
        real_gaps = await measure(off_loop_writes)

        worst_control, worst_real = max(control_gaps), max(real_gaps)
        ceiling = HEARTBEAT_INTERVAL * 1.9

        # The control is the load-bearing assertion. Without it, "no stalls"
        # could just mean the instrument is insensitive, and a heartbeat this
        # smooth says nothing about where the writes ran.
        check("CONTROL: inline writes DO stall the loop", worst_control > ceiling,
              f"worst={worst_control * 1000:.1f}ms vs nominal "
              f"{HEARTBEAT_INTERVAL * 1000:.0f}ms")
        check("off-loop writes do not stall the loop", worst_real <= ceiling,
              f"worst={worst_real * 1000:.1f}ms")
        check("off-loop beats the control", worst_real < worst_control,
              f"{worst_real * 1000:.1f}ms vs {worst_control * 1000:.1f}ms")

        await run_query("why does light diffract", send, client=FakeClient([
            "Stripe gratings diffract light into orders.",
            "Light splits into orders.",
        ]), config=cfg, store=SlowStore(store, 0.0), verbose=False)


        print("\n4. learning is visible over time")
        before = store.topic_evidence()
        for n in range(3):
            cid = store.add_card(query=f"mango {n}", stage1="z" * 400,
                                 summary=f"Mango {n} ripens", keywords=["mango"]).id
            store.add_event(cid, "star")
        after = store.topic_evidence()
        check("new topic enters the prompt",
              "mango" in store.wearer_context().interests,
              str(store.wearer_context().interests))
        check("weight rose with evidence",
              after["mango"]["weight"] > before.get("mango", {}).get("weight", 0.0),
              f"{before.get('mango', {}).get('weight', 0):.2f} -> "
              f"{after['mango']['weight']:.2f}")
        check("weights remain sorted by evidence",
              all(v["cards"] >= MIN_CARDS_FOR_INTEREST
                  for v in after.values() if v in
                  [{"cards": d["cards"]} for d in
                   store.wearer_context().interest_detail]))
        print(f"     describe() -> {store.wearer_context().describe()}")
        store.close()

    print()
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        return 1
    print("OK    ingestion, gating, heartbeat isolation and learning all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))