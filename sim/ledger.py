"""
sim/ledger.py
=============

The decisions ledger, implemented from the DDL proposed in
`documents/09-schema.sql`.

Applying that file verbatim is deliberate: it means the schema is exercised
rather than merely written down. If the DDL is wrong, this fails loudly at
simulation time instead of after a fortnight of real wear.

Two things are enforced here that the DDL alone cannot:

* every decision **freezes its feature vector** at decision time, because the
  posterior moves and an unfrozen audit cannot reconstruct why decision #412
  fired on Tuesday;
* an impression is recorded **separately** from a reaction, because "shown but
  never examined" and "examined and rejected" are different facts and
  collapsing them is what makes naive implicit feedback learn the wrong thing.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "documents" / "09-schema.sql"


class Ledger:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text())
        self.conn.commit()

    # -- features ----------------------------------------------------------

    def ensure_feature(self, key: str, kind: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO features (key, kind, created_at) VALUES (?, ?, ?)",
            (key, kind, time.time()),
        )

    def _feature_row(self, key: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM feature_score WHERE feature_key = ?", (key,)
        ).fetchone()
        return row if row else {"feature_key": key, "kind": "other", "w": 0.0,
                                "n": 0}

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self,
        *,
        candidate: str,
        cls: str,
        keywords: list[str],
        decision: str,
        score: float,
        threshold: float,
        reason: str,
        depth: int = 0,
        session_id: str = "",
    ) -> int:
        """
        Write one decision with its feature vector frozen.

        Features are the structured signals that matter, NOT keyword counts:
        source, class, depth and rendered length. Keyword weighting was shown
        to spread signal too thin to ever decide anything.
        """
        display_len = len(candidate)
        features = {
            f"source:{cls}": 1.0,
            "depth:0" if depth == 0 else f"depth:{min(depth, 3)}": 1.0,
            "len:short" if display_len <= 60 else
            ("len:med" if display_len <= 90 else "len:long"): 1.0,
            "charnum" if any(c.isdigit() for c in candidate) else "charnodigit": 1.0,
        }

        cur = self.conn.execute(
            "INSERT INTO decisions (created_at, card_id, session_id, depth,"
            " candidate, candidate_chars, candidate_words, decision, score,"
            " threshold, reason, observable)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (time.time(), None, session_id, depth, candidate, display_len,
             len(candidate.split()), decision, score, threshold, reason),
        )
        did = int(cur.lastrowid)

        for key, value in features.items():
            self.ensure_feature(key, key.split(":")[0])
            self.conn.execute(
                "INSERT INTO decision_features (decision_id, feature_key, value)"
                " VALUES (?,?,?)", (did, key, value),
            )
        self.conn.commit()
        return did

    def _has(self, table: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    # -- impressions and gaze ---------------------------------------------

    def note_shown(self, decision_id: int, *, examined: bool = False,
                   re_examined: bool = False, dwell_ms: int = 0) -> None:
        """
        Record that the item reached the lens, and whether it was looked at.

        ``examined`` is False both when the wearer looked away and when the
        glasses were in their case — which are not the same fact. The caller
        marks the second case by not calling this at all, so an unobserved card
        is censored rather than counted as a miss.
        """
        self.conn.execute(
            "UPDATE decisions SET outcome=?, outcome_at=?, exam_count=?,"
            " first_exam_at=?, last_exam_at=?, examined=? WHERE id=?",
            ("confirm" if examined else "timeout", time.time(),
             1 + (1 if re_examined else 0), time.time(), time.time(),
             1 if examined else 0, decision_id),
        )
        self.conn.commit()

    def note_censored(self, decision_id: int) -> None:
        """Glasses were in the case / dashboard open: not observable."""
        self.conn.execute(
            "UPDATE decisions SET observable=0, outcome='unobservable' WHERE id=?",
            (decision_id,),
        )
        self.conn.commit()

    # -- labels ------------------------------------------------------------

    def label(self, decision_id: int, worth: int, already_knew: int = 0) -> None:
        """Write the review label. This is the ONLY writer of ground truth."""
        self.conn.execute(
            "UPDATE decisions SET label=?, already_knew=?, reviewed_at=? WHERE id=?",
            (worth, already_knew, time.time(), decision_id),
        )
        self.conn.commit()

    def apply_label_counts(self, decision_id: int) -> None:
        """
        Fold this decision's labels into the per-feature Beta counts.

        This is where the two channels separate: ``pos``/``neg`` are the
        reviewer's explicit yes/no and are the only ground truth. TouchBar
        reactions, if ever folded in, go to ``implicit_*`` and can be switched
        on by changing this function — deliberately not by default.
        """
        row = self.conn.execute(
            "SELECT label FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if not row or row["label"] is None:
            return
        delta = "pos" if row["label"] == 1 else "neg"
        keys = [r["feature_key"] for r in self.conn.execute(
            "SELECT feature_key FROM decision_features WHERE decision_id=?",
            (decision_id,)).fetchall()]
        for key in keys:
            self.conn.execute(
                "INSERT INTO feature_counts (feature_key, {d}, updated_at)"
                " VALUES (?, 1, ?) ON CONFLICT(feature_key) DO UPDATE SET"
                " {d} = {d} + 1, updated_at = excluded.updated_at".format(d=delta),
                (key, time.time()),
            )
        self.conn.commit()

    # -- scoring -----------------------------------------------------------

    def score(self, decision_id: int) -> float:
        """Dot product of frozen features and their posterior log-odds,
        normalised by the feature vector's L2 norm."""
        row = self.conn.execute(
            "SELECT score FROM decision_score WHERE id=?", (decision_id,)
        ).fetchone()
        return float(row["score"]) if row else 0.0

    def explain(self, decision_id: int) -> list[dict]:
        """The 'why did it interrupt me' audit, exactly as documents/09 shows it."""
        out = []
        for r in self.conn.execute(
            "SELECT df.feature_key, df.value, fs.w, fs.w_lo95, fs.w_hi95,"
            " fs.p_hat, fs.n FROM decision_features df"
            " JOIN feature_score fs ON fs.feature_key = df.feature_key"
            " WHERE df.decision_id = ? ORDER BY abs(df.value * fs.w) DESC",
            (decision_id,),
        ).fetchall():
            out.append(dict(r))
        return out

    # -- aggregates used by the policy axes --------------------------------

    def counts(self) -> dict:
        c = self.conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        l = self.conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE label IS NOT NULL").fetchone()["n"]
        return {"decisions": c, "labels": l}

    def queue_worth_rate(self, since: float | None = None) -> float | None:
        """The metric `g1_model` turns on."""
        row = self.conn.execute(
            "SELECT AVG(label) r, COUNT(*) n FROM decisions"
            " WHERE label IS NOT NULL AND decision='queue'"
            + (" AND created_at >= ?" if since else ""),
            (since,) if since else (),
        ).fetchone()
        if not row or row["n"] == 0:
            return None
        return float(row["r"])

    def drill_miss_rate(self) -> float | None:
        """The metric `detail_generation` turns on."""
        shown = self.conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE depth > 0").fetchone()["n"]
        if not shown:
            return None
        miss = self.conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE outcome='unobservable'").fetchone()["n"]
        return miss / shown if shown else None

    def evidence(self) -> dict:
        """Everything the policy axes need, gathered in one place."""
        counts = self.counts()
        return {
            "g1_model": {
                "n_labels": counts["labels"],
                "queue_worth_rate": self.queue_worth_rate(),
            },
            "detail_generation": {"drill_miss_rate": self.drill_miss_rate()},
            "interrupt_gate": {
                "clean_pushes": self.conn.execute(
                    "SELECT COUNT(*) n FROM decisions WHERE decision='push'"
                " AND label=1 AND already_knew=0").fetchone()["n"],
            },
            "push_enabled": {
                "label_days": self.conn.execute(
                    "SELECT COUNT(DISTINCT date(created_at,'unixepoch')) n"
                    " FROM decisions WHERE label IS NOT NULL").fetchone()["n"],
            },
        }

    def verdict(self, t0: float, days: int) -> dict:
        """The Milestone 1 verdict query from DESIGN.md."""
        row = self.conn.execute(
            "SELECT COUNT(*) shown,"
            " SUM(CASE WHEN label=1 AND outcome='confirm' THEN 1 ELSE 0 END) confirmed,"
            " SUM(CASE WHEN label=0 THEN 1 ELSE 0 END) rejected,"
            " SUM(CASE WHEN label IS NULL THEN 1 ELSE 0 END) unreviewed"
            " FROM decisions WHERE created_at BETWEEN ? AND ?",
            (t0, t0 + days * 86400),
        ).fetchone()
        return dict(row)

    def close(self) -> None:
        self.conn.close()