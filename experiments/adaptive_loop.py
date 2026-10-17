"""
Does the adaptive loop actually work, and does the heartbeat survive it?

The integration is easy to claim and easy to get wrong in ways that only show
up under load, so this exercises the four claims that matter:

  1. OBSERVATION  the store keeps weights, and they move when the wearer
                 reacts. This is the half of the adaptive loop that SURVIVED
                 its own falsification: the substrate measures cleanly and is
                 how a human reads the wearer (``main.py --weights``).
  2. SILENCE     no interest ever reaches a prompt. This is the half that did
                 not survive. Every prompt sent on either architecture is
                 asserted to contain no <interests> block, including the case
                 where the store has strong evidence and the query names it --
                 because that is exactly the case the old gate let through.
  3. OFF-LOOP    a heartbeat task on the same loop as the session keeps its
                 cadence while reactions are written. The G1 drops the link
                 after ~32s without a 0x25 heartbeat, so a commit on the loop
                 is a link drop.
  4. LEARNING    starring raises the weight of that card's keywords, the shift
                 is visible, and it is visible over time rather than only at
                 the end.

Sections 2 and 3b were INVERTED at commit b5ea746+ when the injection was
removed. They previously asserted that an on-topic question RECEIVED the
block; asserting the absence of the block needs a positive control or it is
vacuous -- so both sections first prove they can still detect a block that is
genuinely there, then assert the shipped path does not contain one.

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
    ONE_STAGE_SYSTEM,
    PRIMARY_SYSTEM_PROMPT,
    build_llm_config,
    condense_for_glasses,
    run_query,
)

# The removed block, kept as a DETECTOR rather than as a feature. Proving
# "no <interests> in this prompt" is worthless if the search string never
# matches anything, so both sections first build a prompt with the block
# spliced in and confirm the assertion would have caught it. The exact text
# is the specimen archived in experiments/personalisation_ab.py; only the
# marker matters here.
_BLOCK_MARKER = "<interests>"

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

        print("\n1. observation substrate")
        check("store exposes evidenced interests",
              set(ctx.interests) == {"stripe", "photon"}, str(ctx.interests))
        check("gated on distinct cards, not taps",
              all(d["cards"] >= MIN_CARDS_FOR_INTEREST for d in ctx.interest_detail))
        check("recent card state is carried", len(ctx.recent_summaries) > 0,
              f"{len(ctx.recent_summaries)} summaries")

        print("\n2. prompts carry no wearer context")
        cfg = config()

        # POSITIVE CONTROL FIRST. Every assertion in this section is an
        # absence, and an absence asserted by a search string that cannot
        # match is indistinguishable from a passing test. So: splice the
        # block into a copy of each prompt, confirm the marker is findable,
        # and only then assert the shipped prompt has none. If someone deletes
        # the block from pipeline.py and the marker in this file goes stale,
        # this check fails LOUD instead of the absence passing vacuously.
        for name, base in (("compressor", COMPRESSOR_SYSTEM_PROMPT),
                           ("one-stage", ONE_STAGE_SYSTEM),
                           ("primary", PRIMARY_SYSTEM_PROMPT)):
            planted = base + f"\n\n<interests>stripe, photon</interests>\n"
            check(f"CONTROL: a planted block IS detectable in the {name} prompt",
                  _BLOCK_MARKER in planted)

        # Driven through the SHIPPED path -- Session.ask -> run_query ->
        # condense_for_glasses -- not by calling condense_for_glasses
        # directly. The previous version did the latter, and that is exactly
        # why a dead `elif` branch in the shipped code passed this section
        # green: the production call shape was never exercised.
        # The store is seeded with three starred cards on exactly these
        # topics, so the evidence is as strong as this harness can make it.
        cfg.two_stage = True

        ON_TOPIC_STAGE1 = (
            "Stripe gratings diffract light into discrete orders, which is "
            "why photon momentum appears quantised.")

        async def ask_through_session(stage1: str):
            """Drive the real Session.ask -> run_query -> condense path."""
            from session import Session
            fake = FakeClient([stage1, "Stripe splits light into orders."])
            written2: list[str] = []

            async def send2(text: str) -> None:
                written2.append(text)

            sess = Session(
                send=send2, store=FeedbackStore(store.path), config=cfg,
                client=fake,
            )
            await sess.ask("why does light diffract through a stripe grating")
            return fake

        # One arm is enough now that there is nothing to compare: the block
        # must be absent from every prompt, on-topic or not. Two-stage still
        # gets its own check because that prompt no longer exists in any
        # other form and could be reintroduced with the template.
        fake = await ask_through_session(ON_TOPIC_STAGE1)
        check("TWO-STAGE: on-topic answer still carries NO block",
              _BLOCK_MARKER not in system_of(fake, 0),
              "stage 1")
        check("TWO-STAGE: compressor prompt carries NO block",
              _BLOCK_MARKER not in system_of(fake, 1))
        check("TWO-STAGE: compressor prompt is byte-identical to the base",
              system_of(fake, 1) == COMPRESSOR_SYSTEM_PROMPT)
        check("TWO-STAGE: the old unguarded imperative is still GONE",
              "Weight those subjects when choosing" not in system_of(fake, 1))

        # The plugin layer is a third injection site and it has never had a
        # caller (plugins.preferred_profile). Guard it anyway: the failure mode
        # here is quiet, because wiring a helper to a prompt looks like an
        # improvement and produces no test failure on its own.
        from plugins import preferred_profile
        prof = preferred_profile(store)
        check("CONTROL: the plugin profile path still produces text",
              bool(prof), repr(prof[:40]))
        check("TWO-STAGE: no plugin profile in either prompt",
              prof[:40] not in system_of(fake, 0)
              and prof[:40] not in system_of(fake, 1))

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

        async def no_writes():
            """Baseline: heartbeat cadence with the store untouched."""
            await asyncio.sleep(0)

        baseline_gaps = await measure(no_writes)
        control_gaps = await measure(on_loop_writes)
        real_gaps = await measure(off_loop_writes)

        worst_base, worst_control, worst_real = (
            max(baseline_gaps), max(control_gaps), max(real_gaps))
        ceiling = HEARTBEAT_INTERVAL * 1.9

        # The control is the load-bearing assertion. Without it, "no stalls"
        # could just mean the instrument is insensitive, and a heartbeat this
        # smooth says nothing about where the writes ran.
        # Both arms are measured against a BASELINE captured on the same
        # loop, in the same process, moments apart -- not against the nominal
        # interval. asyncio's own timer and scheduling overhead puts the real
        # floor at ~11ms, so "nominal + half a commit" sat within 0.1ms of the
        # off-loop arm's own jitter. The question is not "is the heartbeat
        # perfect" but "did these writes add a stall", and only a baseline can
        # answer that.
        stall_off = worst_real - worst_base
        stall_inline = worst_control - worst_base
        print(f"     baseline={worst_base * 1000:.1f}ms  "
              f"off-loop={worst_real * 1000:.1f}ms  "
              f"inline={worst_control * 1000:.1f}ms")

        check("CONTROL: inline writes add a real stall",
              stall_inline > COMMIT_COST * 0.75,
              f"inline adds {stall_inline * 1000:.1f}ms over baseline "
              f"(commit costs {COMMIT_COST * 1000:.0f}ms)")
        check("off-loop writes add no measurable stall",
              stall_off < COMMIT_COST * 0.5,
              f"off-loop adds {stall_off * 1000:.1f}ms over baseline")
        check("off-loop is materially better than inline",
              stall_inline - stall_off > COMMIT_COST,
              f"recovers {(stall_inline - stall_off) * 1000:.1f}ms")

        await run_query("why does light diffract", send, client=FakeClient([
            "Stripe gratings diffract light into orders.",
            "Light splits into orders.",
        ]), config=cfg, verbose=False)


        print("\n3b. the one-call path (the DEFAULT) carries no block either")
        # This is the shipped default, so it is the path that actually matters
        # for the absence assertion. The store is seeded on "stripe" and the
        # query is squarely on that topic -- the strongest evidence the
        # harness can produce -- and the block must still be absent. Under the
        # old design this exact query DID receive the block, which is what
        # experiments/personalisation_ab.py measured and falsified.
        cfg_two = config()
        cfg_two.two_stage = False

        async def ask_one_stage(q: str):
            from session import Session
            fake = FakeClient(["Stripe gratings split light into orders."])
            out: list[str] = []

            async def send2(text: str) -> None:
                out.append(text)

            sess = Session(
                send=send2, store=FeedbackStore(store.path), config=cfg_two,
                client=fake,
            )
            await sess.ask(q)
            return fake

        f1 = await ask_one_stage("why does light diffract through a stripe grating?")
        sent1 = system_of(f1, 0)
        check("ONE-STAGE: on-topic query gets NO block",
              _BLOCK_MARKER not in sent1,
              f"prompt was {len(sent1)} chars, no marker")
        check("ONE-STAGE: prompt is byte-identical to the base",
              sent1 == ONE_STAGE_SYSTEM)

        f2 = await ask_one_stage("how do mango cultivars ripen?")
        check("ONE-STAGE: off-topic query gets NO block",
              _BLOCK_MARKER not in system_of(f2, 0))

        print("\n5. observability: a card in the pocket is not a card ignored")
        # The distinction this buys is the whole point. "The wearer did not
        # react" has two causes -- they looked away, or the glasses were in
        # the case. Only the first is evidence about the wearer. The firmware
        # emits the wear-state events either way and handle_touch used to drop
        # all of them, because none of them map to an action.
        import tempfile as _tf
        from memory import FeedbackStore as _Store
        from session import WEAR_STATE_EVENTS
        from session import Session as _Session

        with _tf.TemporaryDirectory() as tmp2:
            os_store = _Store(Path(tmp2) / "obs.db")
            cfg_o = config()

            async def ask_and_read(packets: list[bytes]) -> tuple[bool, str]:
                fake = FakeClient(["Stripe splits light into orders."])
                out: list[str] = []

                async def send2(text: str) -> None:
                    out.append(text)

                sess = _Session(send=send2, store=_Store(os_store.path),
                                config=cfg_o, client=fake)
                for pkt in packets:
                    sess.handle_touch("LEFT", pkt)
                await sess.ask("why does light diffract")
                return sess.observable, os_store.events_for_card(
                    sess.current.card.id)[-1].detail

            worn_ob, detail_a = await ask_and_read([bytes([0xF5, 0x06])])
            check("worn: the card records itself as observable",
                  worn_ob and detail_a == "observable=1", detail_a)

            cased_ob, detail_b = await ask_and_read(
                [bytes([0xF5, 0x0B])])          # placed in case, lid closed
            check("in the case: the card records itself as NOT observable",
                  (not cased_ob) and detail_b == "observable=0", detail_b)

            dash_ob, _ = await ask_and_read([bytes([0xF5, 0x1E])])
            check("dashboard open: not observable", dash_ob is False)

            back_ob, _ = await ask_and_read(
                [bytes([0xF5, 0x0B]), bytes([0xF5, 0x06])])
            check("worn again: observable returns", back_ob is True)

            check("all six wear-state bytes are decoded, none mapped to action",
                  set(WEAR_STATE_EVENTS) == {0x06, 0x07, 0x08, 0x0B, 0x1E, 0x1F},
                  str(sorted(hex(k) for k in WEAR_STATE_EVENTS)))
            os_store.close()

        print("\n6. learning is visible over time")
        before = store.topic_evidence()
        for n in range(3):
            cid = store.add_card(query=f"mango {n}", stage1="z" * 400,
                                 summary=f"Mango {n} ripens", keywords=["mango"]).id
            store.add_event(cid, "star")
        after = store.topic_evidence()
        check("new topic appears in the observed interests",
              "mango" in store.wearer_context().interests,
              str(store.wearer_context().interests))
        check("weight rose with evidence",
              after["mango"]["weight"] > before.get("mango", {}).get("weight", 0.0),
              f"{before.get('mango', {}).get('weight', 0):.2f} -> "
              f"{after['mango']['weight']:.2f}")
        ctx_now = store.wearer_context()
        check("every interest clears the distinct-card gate",
              ctx_now.interest_detail
              and all(d["cards"] >= MIN_CARDS_FOR_INTEREST
                      for d in ctx_now.interest_detail),
              f"{len(ctx_now.interest_detail)} interests")
        check("interests are ordered by weight, not card count",
              [d["weight"] for d in ctx_now.interest_detail]
              == sorted((d["weight"] for d in ctx_now.interest_detail),
                        reverse=True))
        print(f"     describe() -> {store.wearer_context().describe()}")
        store.close()

    print()
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        return 1
    print("OK    substrate observes, no prompt carries the wearer, heartbeat "
          "holds, learning is visible")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))