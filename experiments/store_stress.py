"""
Concurrency proof for FeedbackStore.

This exists because the first attempt at off-loop persistence -- a shared
connection with ``check_same_thread=False`` -- looked correct and was not.
Measured against that version, at 8 threads x 50 writes:

    rows persisted      285/400   (~29% silently lost)
    duplicate card ids     46     two cards sharing one id
    InterfaceError         101
    OperationalError        9

The failure was silent, which is the part that matters: no exception reached
the caller, the interaction simply never happened, and the wearer saw their
tap swallowed.

Two properties are asserted here, and they are different properties:

  1. DURABILITY  every write that returned is in the database. This is the
     thing that was broken.
  2. IDENTITY    every ``add_card`` returns a distinct id that is actually
     in the cards table. This is what breaks when ``lastrowid`` is read off
     a connection another thread has advanced.

Run: ``.venv/bin/python experiments/store_stress.py``
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import FeedbackStore, StoreClosed   # noqa: E402

THREADS = 8
PER_THREAD = 50


def run_workload(store: FeedbackStore) -> tuple[list[int], list[BaseException]]:
    """Every thread writes cards then events, as the TouchBar path does."""
    ids: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(n: int) -> None:
        mine: list[int] = []
        for i in range(PER_THREAD):
            try:
                card = store.add_card(
                    query=f"q{n}-{i}", stage1="answer", summary="summary",
                    keywords=["alpha", f"t{n}"],
                )
                store.add_event(card.id, "expand", f"{n}/{i}")
                mine.append(card.id)
            except BaseException as exc:      # noqa: BLE001 -- recorded, not swallowed
                with guard:
                    errors.append(exc)
        with guard:
            ids.extend(mine)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ids, errors


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stress.db"

        # -- 1. durability + identity under contention ----------------------
        store = FeedbackStore(path)
        ids, errors = run_workload(store)

        expected = THREADS * PER_THREAD
        cards = store.counts()["cards"]
        events = store.counts()["events"]

        if errors:
            kinds = sorted({type(e).__name__ for e in errors})
            failures.append(f"{len(errors)} write(s) raised: {', '.join(kinds)}")
            print(f"  errors    : {len(errors)} ({', '.join(kinds)})")
        else:
            print(f"  errors    : 0")

        print(f"  cards     : {cards} (expected {expected})")
        print(f"  events    : {events} (expected {expected})")
        if cards != expected:
            failures.append(f"card write loss: {cards}/{expected}")
        if events != expected:
            failures.append(f"event write loss: {events}/{expected}")

        distinct = len(set(ids))
        print(f"  ids       : {len(ids)} returned, {distinct} distinct")
        if distinct != len(ids):
            failures.append(f"duplicate ids handed out: {len(ids) - distinct}")

        present = {r["id"] for r in store._conn.execute("SELECT id FROM cards")}
        orphaned = [i for i in ids if i not in present]
        print(f"  orphans   : {len(orphaned)}")
        if orphaned:
            failures.append(f"{len(orphaned)} returned id(s) not in the cards table")

        # -- 2. no event lands on a card that does not own it ---------------
        mismatched = store._conn.execute(
            "SELECT COUNT(*) AS n FROM events e"
            " LEFT JOIN cards c ON c.id = e.card_id WHERE c.id IS NULL"
        ).fetchone()["n"]
        print(f"  dangling  : {mismatched} event(s) with no card")
        if mismatched:
            failures.append(f"{mismatched} dangling event(s)")

        store.close()

        # -- 3. a closed store refuses writes loudly, not obscurely ----------
        try:
            store.add_card(query="q", stage1="a", summary="s")
            failures.append("write after close() returned instead of raising")
            print("  close     : FAIL (no exception)")
        except StoreClosed:
            print("  close     : StoreClosed raised as intended")
        except Exception as exc:            # noqa: BLE001
            failures.append(f"close raised {type(exc).__name__}, not StoreClosed")
            print(f"  close     : FAIL ({type(exc).__name__}: {exc})")

    # -- 4. close() must not free a connection another thread is inside ----
    #
    # Runs in a SUBPROCESS because the failure mode is a SIGSEGV: close() frees
    # the sqlite3 C object while a peer thread is inside sqlite3_step, and
    # there is nothing for `except sqlite3.Error` to catch. Verified that this
    # check can fail -- the pre-fix code segfaults on the first trial, every
    # time (exit 139), while the current code survives every trial.
    code = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "_close_race_child.py")],
        capture_output=True, text=True, timeout=120,
    )
    print(f"  close race: exit {code.returncode} "
          f"({len(code.stdout.splitlines())} trials survived)")
    if code.returncode != 0:
        failures.append(
            f"close() race crashed the process (exit {code.returncode}): "
            f"{code.stderr.strip().splitlines()[-1] if code.stderr.strip() else ''}"
        )

    print("\n6. every writer commits, not just the ones tests exercise")
    # This section exists because six writers did NOT commit and every test
    # still passed. The store opens its connections in SQLite's DEFERRED mode,
    # so an uncommitted write is invisible to any other process and is rolled
    # back when the connection closes -- and it is completely invisible to a
    # test that writes and reads inside one process.
    #
    # It was found by running the review CLI in a second process and finding
    # an empty table that the seeding process had reported as four rows. The
    # suite could never have caught it, which is the whole point: durability
    # is a property ACROSS processes, so the check has to cross one too.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "commit.db"
        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "from memory import FeedbackStore\n"
            "s = FeedbackStore(%r)\n"
            "print(len(s.unlabelled()))\n"
            "s.close()\n" % (str(ROOT), str(db))
        )
        st = FeedbackStore(db)
        # Distinct `line` values, deliberately. These two used to be the same
        # call and this check asserted 2 rows -- i.e. it was relying on
        # record_decision being NON-idempotent. Making the ledger idempotent
        # (one event, one row) collapsed them to one and the test failed on
        # correct code. The property under test is "every writer commits and is
        # visible to another process", which needs two real events, not one
        # event recorded twice.
        writers = {
            "record_push": lambda: st.record_push("meeting", text="t"),
            "record_decision": lambda: st.record_decision(decision="queue",
                                                           cls="digest",
                                                           line="Item one"),
            "set_label": (lambda: st.set_label(
                st.record_decision(decision="queue", cls="digest",
                                   line="Item two"), 1)),
            "record_misfire": lambda: st.record_misfire("device"),
            "clear_suspension": lambda: st.clear_suspension("device"),
            "settle_push": lambda: st.settle_push(1, "delivered"),
        }
        for name, write in writers.items():
            write()
        st.close()

        # Read it all back in a SEPARATE process. If any writer left its
        # transaction open, this is where it shows.
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            failures.append(f"child process failed: {r.stderr.strip()[-120:]}")
        else:
            # Count TOTAL rows, not unlabelled ones: set_label labels a row,
            # so the unlabelled count legitimately drops. The first version of
            # this check asserted on unlabelled and failed on correct code.
            probe_path = Path(tmp) / "_probe.py"
            probe_path.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from memory import FeedbackStore\n"
                "db = sys.argv[1]\n"
                "s = FeedbackStore(db)\n"
                "with s._lock:\n"
                "    total = s._conn.execute("
                "'SELECT COUNT(*) c FROM decisions').fetchone()['c']\n"
                "    lab = s._conn.execute("
                "'SELECT COUNT(*) c FROM decisions WHERE label IS NOT NULL'"
                ").fetchone()['c']\n"
                "print(len(s.push_rows(since_days=1)), total, lab)\n"
                "s.close()\n"
            )
            pushed = subprocess.run(
                [sys.executable, str(probe_path), str(db)],
                capture_output=True, text=True, timeout=60)
            parts = pushed.stdout.split()
            if len(parts) == 3:
                n_push, total, labelled = (int(x) for x in parts)
                if n_push < 1:
                    failures.append("record_push did not commit across a process")
                if total < 2:
                    failures.append(
                        f"record_decision did not commit across a process "
                        f"({total} rows, expected >=2)")
                if labelled < 1:
                    failures.append("set_label did not commit across a process")
                print(f"  cross-process: {n_push} push rows, {total} decisions, "
                      f"{labelled} labelled - all committed")
            else:
                failures.append(f"could not read child output: {pushed.stdout!r} "
                                f"{pushed.stderr.strip()[-160:]}")

    print()

    # ------------------------------------------------------------------
    # 7. an EXISTING database gets migrated
    # ------------------------------------------------------------------
    # Every other section in this file builds a fresh database, which is
    # exactly why the missing migration went unnoticed: `CREATE TABLE IF NOT
    # EXISTS` applies cleanly to a new file and silently does nothing to an
    # old one. The user who already had a store was the only one who hit it,
    # and they hit it as a bare "no such column: dedup_key".
    print("7. an existing database is migrated, not abandoned")
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "old.db"

        # The `decisions` table exactly as it shipped in 16b94ef: before
        # gate, reason, observable, dry_run and dedup_key existed.
        legacy = sqlite3.connect(old)
        legacy.executescript(
            """
            CREATE TABLE decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   REAL NOT NULL,
                decision     TEXT NOT NULL,
                cls          TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT '',
                line         TEXT NOT NULL DEFAULT '',
                label        INTEGER CHECK (label IN (0,1)),
                already_know INTEGER CHECK (already_know IN (0,1)),
                reviewed_at  REAL
            );
            INSERT INTO decisions (created_at, decision, cls, label, line)
                VALUES (1.0, 'queue', 'digest', 1, 'typed by a human');
            """
        )
        legacy.commit()
        before = {r[1] for r in legacy.execute("PRAGMA table_info(decisions)")}
        legacy.close()

        migrated = FeedbackStore(old)
        after = {r[1] for r in migrated._conn.execute(
            "PRAGMA table_info(decisions)")}

        missing = {c for _, c, _ in FeedbackStore._ADDED_COLUMNS}
        still_missing = missing - after
        if still_missing:
            failures.append(
                f"migration left columns absent on {old.name}: "
                f"{sorted(still_missing)}")
        print(f"  ok   {len(before)} columns -> {len(after)}; "
              f"added {len(after - before)}")

        # The column that matters most is the one nobody can re-derive.
        kept = migrated._conn.execute(
            "SELECT label, line FROM decisions WHERE line = 'typed by a human'"
        ).fetchone()
        if kept is None or kept["label"] != 1:
            failures.append("migration destroyed a human's existing label")
        print("  ok   a label typed before the migration survived it")

        # And the migrated database must be USABLE, not merely openable.
        rid = migrated.record_decision(decision="queue", cls="digest",
                                       line="after upgrade", ts=5.0)
        again = migrated.record_decision(decision="queue", cls="digest",
                                         line="after upgrade", ts=5.0)
        total = migrated._conn.execute(
            "SELECT COUNT(*) c FROM decisions").fetchone()["c"]
        if rid != again or total != 2:
            failures.append(
                f"migrated db misbehaves: ids {rid}/{again}, {total} rows")
        print(f"  ok   writes work on the migrated file, replay folds in "
              f"({total} rows)")
        migrated.close()

        # Re-opening must be a no-op. A migration that is not idempotent is a
        # migration that fails on the second launch.
        reopened = FeedbackStore(old)
        cols2 = len(reopened._conn.execute(
            "PRAGMA table_info(decisions)").fetchall())
        reopened.close()
        if cols2 != len(after):
            failures.append(f"re-open changed the schema: {len(after)} -> {cols2}")
        print(f"  ok   re-opening is idempotent ({cols2} columns, unchanged)")

    # The list of columns to migrate is maintained by hand, which is exactly
    # how the bug happened. So check it against the schema that actually ships,
    # and fail loudly when a column is declared in one place and not the other.
    with tempfile.TemporaryDirectory() as tmp:
        probe = FeedbackStore(Path(tmp) / "probe.db")
        declared = {r[1] for r in probe._conn.execute(
            "PRAGMA table_info(decisions)")}
        probe.close()
    unlisted = declared - {c for _, c, _ in FeedbackStore._ADDED_COLUMNS}
    # Columns present in the ORIGINAL table are not migrations; anything else
    # is a column that was added later and must be listed.
    original = {"id", "created_at", "decision", "cls", "source", "line",
                "label", "already_know", "reviewed_at"}
    forgotten = unlisted - original
    if forgotten:
        failures.append(
            f"decisions columns added but not in _ADDED_COLUMNS: "
            f"{sorted(forgotten)} -- they will be missing on every store that "
            f"existed before they were added")
    print(f"  ok   every post-hoc column is listed for migration "
          f"({len(FeedbackStore._ADDED_COLUMNS)} listed)")

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("OK    400/400 writes durable, ids unique, close is safe, and every "
          "writer commits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())