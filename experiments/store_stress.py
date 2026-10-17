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

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("OK    400/400 writes durable, ids unique and present, close is safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())