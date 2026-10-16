"""
memory.py
=========

Local feedback store and salience model.

Why this exists
---------------
The waveguide can only show ~25 words, so the system is constantly making a
bet: *which 25 words are worth the wearer's attention right now?* That bet is
only good if it learns. This module records two things in a local SQLite file:

* every card that has been shown (the full answer plus its summary), and
* what the wearer did with it (ask for more, dismiss, confirm, star).

From that history it derives a per-topic weight and a small "preferences"
block that gets injected into the stage-2 prompt. Over a session or a day, the
compressor starts emphasising the things this particular wearer actually
engages with, rather than the generic "most important fact" every model
defaults to.

Design constraints
------------------
* **No dependencies.** ``sqlite3`` is stdlib. Nothing here phones home; the
  database is a local file and stays under the user's control.
* **Deterministic and testable.** Weighting is plain arithmetic over stored
  events, so behaviour can be asserted without a model or a radio.
* **Append-only.** Cards and events are written, never mutated. Aggregates are
  recomputed on read, which keeps the derived weights honest and makes it easy
  to change the formula later without a migration.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Signal weights. Positive means "relay more of this", negative means "less".
EVENT_WEIGHTS: dict[str, float] = {
    "expand": 0.5,    # asked for more detail -> topic matters, summary too thin
    "confirm": 0.4,   # accepted as sufficient -> the framing worked
    "star": 1.0,      # explicitly marked important
    "dismiss": -0.6,  # pushed away -> low interest
}

# How fast interest decays. A topic you cared about last month should not
# dominate today's summaries.
HALF_LIFE_DAYS = 7.0

# Weight bounds, so a runaway feedback loop cannot pin everything at max.
MIN_WEIGHT = 0.0
MAX_WEIGHT = 3.0

# Words carrying no signal about what a person cares about.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here so as of to
in on at by for with from into onto over under about across after before while
is are was were be been being am do does did done have has had having i me my
we our you your he she it its they them their what which who whom whose when
where why how not no nor only own same too very can will just should now
would could may might must shall don t s re ve ll d m o y ain aren couldn
didn doesn hadn hasn haven isn ma mightn mustn needn shan shouldn wasn weren
won wouldn use used using get got make made made say said says tell told
one two three first second next last also just like really thing things lot
much many more most other another some such only own same
""".split())


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]+")


def extract_keywords(text: str, *, limit: int = 12) -> list[str]:
    """
    Pull representative terms out of a block of text.

    Deliberately simple and dependency-free: lowercase, drop stopwords and
    very short tokens, then rank by frequency with earlier occurrences
    breaking ties so the lead of a passage is favoured.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    position = 0
    for token in _TOKEN_RE.findall((text or "").lower()):
        position += 1
        if token in STOPWORDS or len(token) < 4 or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
        first_seen.setdefault(token, position)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return [word for word, _ in ranked[:limit]]


def _decay(age_seconds: float) -> float:
    """Exponential decay with a seven-day half-life."""
    if age_seconds <= 0:
        return 1.0
    return math.pow(0.5, age_seconds / (HALF_LIFE_DAYS * 86400.0))


def _clamp(value: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, value))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Card:
    """One thing that was shown on the glasses."""

    id: int
    created_at: float
    query: str
    stage1: str
    summary: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Interaction:
    """Something the wearer did in response to a card."""

    created_at: float
    card_id: int
    kind: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class FeedbackStore:
    """
    Append-only local history of cards and reactions.

    ``path`` defaults to ``~/.local/share/g1-ble-backend/feedback.db`` so
    history survives across runs without living in the git checkout.
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        if path is None:
            base = Path.home() / ".local" / "share" / "g1-ble-backend"
            path = base / "feedback.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL    NOT NULL,
                query      TEXT    NOT NULL,
                stage1     TEXT    NOT NULL,
                summary    TEXT    NOT NULL,
                keywords   TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL    NOT NULL,
                card_id    INTEGER NOT NULL,
                kind       TEXT    NOT NULL,
                detail     TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id);
            CREATE INDEX IF NOT EXISTS idx_cards_time  ON cards(created_at);
            CREATE TABLE IF NOT EXISTS meta (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close:
            close()

    # -- key/value metadata ------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a metadata value (used for cached derived artefacts)."""
        self._conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(value), time.time()),
        )
        self._conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a metadata value, or ``default`` when absent."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def __enter__(self) -> "FeedbackStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def add_card(
        self,
        *,
        query: str,
        stage1: str,
        summary: str,
        keywords: Optional[Iterable[str]] = None,
    ) -> Card:
        """Persist a card that was rendered to the lenses."""
        terms = tuple(keywords) if keywords is not None else tuple(
            extract_keywords(f"{query} {summary}")
        )
        now = time.time()
        cursor = self._conn.execute(
            "INSERT INTO cards (created_at, query, stage1, summary, keywords) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, query, stage1, summary, json.dumps(list(terms))),
        )
        self._conn.commit()
        return Card(
            id=int(cursor.lastrowid),
            created_at=now,
            query=query,
            stage1=stage1,
            summary=summary,
            keywords=terms,
        )

    def add_event(self, card_id: int, kind: str, detail: str = "") -> Interaction:
        """Record a wearer reaction to a card."""
        if kind not in EVENT_WEIGHTS:
            raise ValueError(
                f"unknown event kind {kind!r}; expected one of "
                f"{sorted(EVENT_WEIGHTS)}"
            )
        now = time.time()
        self._conn.execute(
            "INSERT INTO events (created_at, card_id, kind, detail) VALUES (?, ?, ?, ?)",
            (now, card_id, kind, detail),
        )
        self._conn.commit()
        return Interaction(created_at=now, card_id=card_id, kind=kind, detail=detail)

    # -- reads -------------------------------------------------------------

    def get_card(self, card_id: int) -> Optional[Card]:
        row = self._conn.execute(
            "SELECT * FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
        return self._row_to_card(row) if row else None

    def recent_cards(self, *, limit: int = 50, since: Optional[float] = None) -> list[Card]:
        if since is not None:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM cards ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def events_for_card(self, card_id: int) -> list[Interaction]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE card_id = ? ORDER BY id", (card_id,)
        ).fetchall()
        return [
            Interaction(
                created_at=r["created_at"],
                card_id=r["card_id"],
                kind=r["kind"],
                detail=r["detail"],
            )
            for r in rows
        ]

    def counts(self) -> dict[str, int]:
        """Totals, for status output and for gating the higher-level pass."""
        cards = self._conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
        events = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        kinds = {
            r["kind"]: r["n"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind"
            ).fetchall()
        }
        return {"cards": cards, "events": events, **kinds}

    # -- the learning part -------------------------------------------------

    def topic_weights(self, *, since_days: float = 30.0) -> dict[str, float]:
        """
        Weighted interest per topic keyword.

        For every event, the card's keywords are credited or penalised by the
        event's weight, then decayed by age. Repeated expansion of the same
        subject compounds, which is how "this person cares about X" emerges
        from a handful of interactions.
        """
        since = time.time() - since_days * 86400.0
        rows = self._conn.execute(
            "SELECT e.kind AS kind, e.created_at AS created_at, c.keywords AS keywords "
            "FROM events e JOIN cards c ON c.id = e.card_id "
            "WHERE e.created_at >= ?",
            (since,),
        ).fetchall()

        weights: dict[str, float] = {}
        for row in rows:
            base = EVENT_WEIGHTS.get(row["kind"], 0.0)
            if base == 0.0:
                continue
            contribution = base * _decay(time.time() - row["created_at"])
            try:
                terms = json.loads(row["keywords"])
            except (json.JSONDecodeError, TypeError):
                continue
            for term in terms:
                weights[term] = _clamp(weights.get(term, 1.0) + contribution)

        # Keywords that exist but have never been reacted to sit at neutral.
        for card in self.recent_cards(limit=200, since=since):
            for term in card.keywords:
                weights.setdefault(term, 1.0)

        return weights

    def top_topics(self, *, limit: int = 8, since_days: float = 30.0) -> list[tuple[str, float]]:
        """Most-weighted topics first."""
        weights = self.topic_weights(since_days=since_days)
        ranked = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]

    def needs_detail(self, *, since_days: float = 30.0) -> list[str]:
        """
        Topics this wearer kept drilling into.

        Expansion means "interesting, but you did not tell me enough". Topics
        that get expanded far more often than dismissed are the ones where the
        compressor should spend its word budget on specifics rather than a
        summary line.
        """
        since = time.time() - since_days * 86400.0
        tally: dict[str, float] = {}
        rows = self._conn.execute(
            "SELECT e.kind AS kind, c.keywords AS keywords "
            "FROM events e JOIN cards c ON c.id = e.card_id "
            "WHERE e.created_at >= ?",
            (since,),
        ).fetchall()
        for row in rows:
            delta = 0.5 if row["kind"] == "expand" else (-0.5 if row["kind"] == "dismiss" else 0.0)
            if delta == 0.0:
                continue
            try:
                terms = json.loads(row["keywords"])
            except (json.JSONDecodeError, TypeError):
                continue
            for term in terms:
                tally[term] = _clamp(tally.get(term, 0.0) + delta)
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        return [term for term, score in ranked if score > 0.5]

    def preferences(self, *, limit: int = 8, since_days: float = 30.0) -> str:
        """
        Compact preference block for injection into the stage-2 prompt.

        Returns an empty string when there is not enough history to say
        anything useful, so the prompt is unchanged for a new user.
        """
        counts = self.counts()
        if counts.get("events", 0) < 3:
            return ""

        top = self.top_topics(limit=limit, since_days=since_days)
        interesting = [(t, w) for t, w in top if w > 1.05]
        detail = self.needs_detail(since_days=since_days)

        lines: list[str] = []
        if interesting:
            lines.append(
                "This wearer engages most with: "
                + ", ".join(topic for topic, _ in interesting[:limit])
                + ". Weight those subjects when choosing what to relay."
            )
        if detail:
            lines.append(
                "They keep asking for more on: "
                + ", ".join(detail[:limit])
                + ". For those topics, spend words on concrete specifics "
                  "(numbers, mechanisms, names) rather than a summary line."
            )
        if counts.get("dismiss", 0) > counts.get("confirm", 0) + counts.get("star", 0):
            lines.append(
                "They dismiss more than they accept. Lead with the single "
                "hard fact; do not pad toward the word limit."
            )
        elif counts.get("confirm", 0) + counts.get("star", 0) > counts.get("dismiss", 0):
            lines.append(
                "They tend to accept these summaries. Include one supporting "
                "detail alongside the headline."
            )
        return " ".join(lines)

    def event_total(self) -> int:
        """Total reactions recorded; the gate for the higher-level pass."""
        return int(self.counts().get("events", 0))

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> Card:
        try:
            terms = tuple(json.loads(row["keywords"]))
        except (json.JSONDecodeError, TypeError):
            terms = ()
        return Card(
            id=row["id"],
            created_at=row["created_at"],
            query=row["query"],
            stage1=row["stage1"],
            summary=row["summary"],
            keywords=terms,
        )