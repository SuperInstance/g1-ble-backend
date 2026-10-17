"""
session.py
==========

Card lifecycle, drill-down, and the feedback loop.

The core idea this module implements: **the wearer should be able to ask for
more without retyping a whole new question.**

After a summary lands on the lenses, a single TouchBar tap expands *that same
topic* — no new prompt, no re-explaining the context. Each expansion becomes a
child card, and every interaction (expand, dismiss, confirm, star) is written
to a local store. Those reactions are what later lets the compressor
"reweight what matters" for this particular person.

The stack is intentionally shallow and explicit:

    card[0]  "why do onion skins have layers"     <- tap
    card[1]    child: the mechanism                <- tap
    card[2]      child: what else follows          <- dismiss, back to root

Nothing here knows about Bluetooth or about LLM providers; it is handed a
``send`` callback and a configured pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from memory import Card, FeedbackStore, extract_keywords
from policy import resolve
from pipeline import (
    LLMConfig,
    LLMStageError,
    LLMUnavailable,
    QueryResult,
    ensure_models,
    make_client,
    run_query,
    word_count,
)

log = logging.getLogger("g1.session")

# TouchBar sub-commands (0xF5 ...) mapped to session actions.
#
# Only gestures we can distinguish with confidence are bound. Per the community
# protocol notes, 0x04 and 0x05 are NOT two gestures: they are a single
# triple-tap that alternates between the two bytes on each press (a silent-mode
# toggle in the firmware). Binding them to two different actions would make the
# meaning flip-flop with every press, so they are deliberately left unmapped
# until real taps are captured from the hardware.
#
# Correcting an earlier version: 0x04/0x05 were briefly mapped to confirm/star.
# That was a guess dressed up as a design.
DEFAULT_TOUCH_ACTIONS: dict[int, str] = {
    0x01: "more",     # single tap -> drill down on what is on the lens
    0x00: "dismiss",  # double tap -> done, or back out one level
    0x17: "confirm",  # held       -> deliberate positive signal
}

# Gesture bytes intentionally NOT bound, kept visible so the omission is
# documented rather than forgotten.
UNMAPPED_TOUCH_EVENTS: dict[int, str] = {
    0x02: "head up",
    0x03: "head down",
    0x04: "triple tap (alternating silent-mode toggle, not a distinct gesture)",
    0x05: "triple tap (alternating silent-mode toggle, not a distinct gesture)",
    0x18: "touchbar released",
}


@dataclass
class Displayed:
    """A card currently on (or just removed from) the lenses."""

    card: Card
    depth: int = 0
    parent_id: Optional[int] = None
    result: Optional[QueryResult] = None


@dataclass
class SessionStats:
    turns: int = 0
    expansions: int = 0
    dismissals: int = 0
    confirms: int = 0
    stars: int = 0


class Session:
    """
    Drives the wearer's conversation with the glasses.

    ``send`` is an async callable that renders text to the lenses (normally a
    thin wrapper around ``controller.send_text_packet``). Keeping it injected
    means the whole session is testable with a stub.
    """

    def __init__(
        self,
        *,
        send,
        store: FeedbackStore,
        config: LLMConfig | None = None,
        client=None,
        touch_actions: Optional[dict[int, str]] = None,
        policy=None,
        personalise: bool = True,
    ) -> None:
        # Contested design questions are resolved at setup and read here as a
        # flat answer. The runtime does not re-argue them; it reads a decision
        # that a metric already made. See policy.py.
        self.policy = policy if policy is not None else resolve()
        self.send = send
        self.store = store
        self.personalise = personalise
        self.config = config or LLMConfig(
            api_key=None, base_url=None,
            primary_model=None, compressor_model=None,
            timeout=30.0, max_retries=2, max_display_words=25,
        )
        self.client = client
        self.touch_actions = dict(touch_actions or DEFAULT_TOUCH_ACTIONS)

        self.stack: list[Displayed] = []
        self.stats = SessionStats()
        self._pending: set[asyncio.Task] = set()

    # -- helpers -----------------------------------------------------------

    @property
    def current(self) -> Optional[Displayed]:
        """Top of the stack, or None when nothing is showing."""
        return self.stack[-1] if self.stack else None

    async def preferences(self) -> str:
        """
        What the store has learned, as a prompt fragment.

        Empty for a new user, so the stage-1 prompt is unchanged until there
        is real signal to act on.

        Async because this is a full scan of the event history: measured at
        10.7ms median / 13.4ms max at 2400 events, growing linearly with
        history, once per ask and once per drill-down. Called inline that is
        10ms the BLE heartbeat does not get.

        Note stage 2 no longer consumes this at all -- it uses the gated
        WearerContext instead. Only stage 1 still does, which is a known open
        item: injecting interests into stage 1 shapes what gets reasoned about
        rather than merely what gets compressed.
        """
        try:
            return await asyncio.to_thread(self.store.preferences)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not read preferences: %s", exc)
            return ""

    def _ensure_ready(self):
        if self.client is None:
            self.client = make_client(self.config)
        return self.client

    async def ready(self) -> LLMConfig:
        """Resolve models once per session."""
        client = self._ensure_ready()
        self.config = await ensure_models(self.config, client)
        return self.config

    def _shown_so_far(self) -> str:
        """
        Everything already put in front of this wearer on this thread.

        Fed to stage 1 in follow-up mode so an expansion adds information
        rather than restating what is already on the lens.
        """
        lines = []
        for item in self.stack:
            if item.card.summary:
                lines.append(item.card.summary)
        return "\n".join(lines[-8:])

    # -- actions -----------------------------------------------------------

    async def ask(self, query: str) -> Displayed:
        """Fresh question: run the full pipeline and show the result."""
        self.stats.turns += 1
        client = self._ensure_ready()
        self.config = await ensure_models(self.config, client)

        prefs = await self.preferences()
        result = await run_query(
            query, self.send, client=client, config=self.config,
            preferences=prefs, mode="primary", store=self.store,
            personalise=self.personalise,
        )

        keywords = extract_keywords(f"{query} {result.display_text}")
        card = await asyncio.to_thread(
            self.store.add_card,
            query=query,
            stage1=result.stage1 or "",
            summary=result.display_text,
            keywords=keywords,
        )

        displayed = Displayed(card=card, depth=0, parent_id=None, result=result)
        # A new question replaces the thread rather than stacking under it.
        self.stack = [displayed]
        return displayed

    async def _record(self, card_id: int, kind: str, detail: str = "") -> bool:
        """
        Write one wearer reaction to the ledger, off the event loop.

        Every reaction goes through here. ``handle_touch`` is entered from the
        bleak notification callback and ``run_action`` from a task on the same
        loop that carries the BLE heartbeat, so a SQLite commit made inline
        would block the heartbeat for exactly as long as the commit takes --
        and the G1 drops the link after ~32s without one. ``asyncio.to_thread``
        moves the commit to the default executor; ``FeedbackStore`` serialises
        the write behind its own lock, so the heartbeat never waits on it.

        The await is kept deliberately: fire-and-forget would let the stats
        counters drift away from what is actually persisted, and a failed write
        would be invisible. Returns whether the row landed.
        """
        try:
            await asyncio.to_thread(self.store.add_event, card_id, kind, detail)
        except Exception as exc:                        # noqa: BLE001
            log.warning("could not record %r on card %s: %s", kind, card_id, exc)
            return False

        log.info("recorded %-7s card=%s", kind, card_id)
        # Show the shift, not just the event: the whole point of the ledger is
        # watching interests move. Only for the two reactions that actually
        # move them -- on every tap this is a full history scan for no news.
        if kind in ("star", "dismiss"):
            try:
                ctx = await asyncio.to_thread(self.store.wearer_context)
                log.info("weights -> %s", ctx.describe())
            except Exception as exc:                    # noqa: BLE001
                log.warning("could not read weights: %s", exc)
        return True

    async def more(self) -> Optional[Displayed]:
        """
        Drill down on whatever is currently on the lenses.

        This is the "ask for more information from the summary" path: no new
        prompt is needed. Runs stage 1 in follow-up mode (so it adds rather
        than repeats), compresses, renders, and records the expansion — which
        simultaneously keeps the card and tells the store the summary was too
        thin.
        """
        top = self.current
        if top is None:
            return None

        # Deferred by policy: on-demand detail puts the slowest component on
        # the interaction most likely to be abandoned when it is slow. The log
        # decides, not an opinion; until drill_miss_rate is measured, tap does
        # not spin up a generation.
        #
        # But the gesture still has to CHANGE SOMETHING. Returning the same
        # card without a lens write made the most-promised interaction a silent
        # no-op: the wearer taps, sees precisely the same line, and has no way
        # to tell a deferred drill from an ignored tap. The REPL then printed
        # the unchanged text back as though something had happened. Say so in
        # the cheapest way available -- the text already on the lenses.
        if not self.policy.on("detail_generation"):
            await self._record(top.card.id, "drill_miss", "deferred-by-policy")
            log.info("drill-down deferred: policy detail_generation=off "
                     "(drill_miss recorded; the log decides)")
            try:
                await self.send("Detail not ready. Tap again later.")
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("could not show deferral notice: %s", exc)
            return top

        self.stats.expansions += 1
        client = self._ensure_ready()
        self.config = await ensure_models(self.config, client)

        context = self._shown_so_far()
        result = await run_query(
            top.card.query, self.send, client=client, config=self.config,
            preferences=await self.preferences(), mode="followup",
            context=context,
            store=self.store, personalise=self.personalise,
        )

        # Recording the reaction and creating the child card are one action.
        await self._record(top.card.id, "expand", "touchbar/tap")

        keywords = extract_keywords(f"{top.card.query} {result.display_text}")
        card = await asyncio.to_thread(
            self.store.add_card,
            query=top.card.query,
            stage1=result.stage1 or "",
            summary=result.display_text,
            keywords=keywords,
        )
        child = Displayed(card=card, depth=top.depth + 1, parent_id=top.card.id,
                          result=result)
        self.stack.append(child)
        return child

    async def dismiss(self) -> Optional[Displayed]:
        """
        Back out of the current card.

        Critically, popping the stack is NAVIGATION, not rejection, and must not
        be scored as one. An earlier version recorded a negative event on every
        pop, so drilling into a topic three levels and then backing out taught
        the system "do not explore" - it penalised exactly the behaviour we
        want to encourage.

        A negative signal is only recorded when the wearer dismisses the ROOT
        card, which is a real rejection of what was offered.
        """
        top = self.current
        if top is None:
            return None

        self.stats.dismissals += 1

        if len(self.stack) > 1:
            # Navigating back up: neutral. Keep exploring cheap.
            self.stack.pop()
            return self.stack[-1]

        # Root card dismissed: the wearer declined to engage with this at all.
        await self._record(top.card.id, "dismiss", "root-dismissed")
        self.stack.pop()
        with_content = top.card.summary
        if with_content:
            try:
                await self.send("")
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("could not clear display: %s", exc)
        return None

    async def confirm(self) -> bool:
        """Wearer accepted the summary as good. Recorded as a positive signal."""
        top = self.current
        if top is None:
            return False
        await self._record(top.card.id, "confirm", "touchbar/triple-tap")
        self.stats.confirms += 1
        return True

    async def star(self) -> bool:
        """Wearer flagged this as highly important."""
        top = self.current
        if top is None:
            return False
        await self._record(top.card.id, "star", "touchbar/triple-tap-alt")
        self.stats.stars += 1
        return True

    async def run_action(self, action: str) -> Optional[Displayed]:
        """Dispatch a named action. Unknown actions are ignored, not fatal."""
        try:
            if action == "more":
                return await self.more()
            if action == "dismiss":
                return await self.dismiss()
            if action == "confirm":
                await self.confirm()
                return self.current
            if action == "star":
                await self.star()
                return self.current
        except LLMUnavailable as exc:
            log.warning("action %s unavailable: %s", action, exc)
        except LLMStageError as exc:
            log.warning("action %s failed: %s", action, exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("action %s raised: %s", action, exc)
        return None

    # -- TouchBar ----------------------------------------------------------

    def handle_touch(self, side: str, data: bytes) -> None:
        """
        Map a TouchBar packet to an action and run it off the hot path.

        Called synchronously from the BLE notification callback, so the actual
        work is scheduled as a task: an LLM call must never block the event
        loop that is delivering notifications.
        """
        if len(data) < 2 or data[0] != 0xF5:
            return
        action = self.touch_actions.get(data[1])
        if action is None:
            return
        if self.current is None:
            return

        log.debug("touchbar %s -> action %s", side, action)
        task = asyncio.create_task(self.run_action(action))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        """One-line description of the session so far, for the terminal."""
        return (
            f"{self.stats.turns} turn(s), {self.stats.expansions} expansion(s), "
            f"{self.stats.dismissals} dismiss(ed), "
            f"{self.stats.confirms} confirm(ed), {self.stats.stars} star(red); "
            f"depth {self.current.depth if self.current else 0}"
        )