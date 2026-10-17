"""
Child process for the close() race check in store_stress.py.

Deliberately separate: the bug it hunts is a SIGSEGV, which kills the
interpreter outright, so it cannot live inside the main test process. The
parent asserts this script exits 0.

Before reads were serialised behind the store lock, closing a store while
six threads were reading and writing it reproduced a segfault on the first
trial, every time. Read the rationale at ``FeedbackStore.__init__``.
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import FeedbackStore   # noqa: E402

TRIALS = 4
WORKERS = 6


def trial(n: int) -> None:
    tmp = Path(tempfile.mkdtemp()) / "close.db"
    store = FeedbackStore(tmp)
    cid = store.add_card(query="q", stage1="s", summary="x",
                         keywords=["alpha"]).id
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                store.recent_cards(limit=50)
                store.add_event(cid, "expand")
                store.topic_evidence()
            except Exception:
                pass          # StoreClosed is the correct post-close outcome

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(WORKERS)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    store.close()          # <- the racy call
    stop.set()
    for t in threads:
        t.join(timeout=1)
    print(f"trial {n}: survived", flush=True)


def main() -> int:
    for n in range(TRIALS):
        trial(n)
    print("no segfault")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())