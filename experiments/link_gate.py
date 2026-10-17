"""
Can the radio actually carry this? Tested with no radio.

`interrupt.LinkStatus` is a struct and `check()` is a pure predicate, so the
entire link gate is exercisable on a host with no G1 and no BlueZ. That is the
whole reason it was built this shape: every claim this project makes about
interrupt behaviour has to be checkable without hardware, because there is no
hardware.

WHAT THIS IS NOT

It does not prove a real G1 reports any of this truthfully. `g1_driver` has
never touched a device. A gate that passes against a hand-built fake is a gate
whose LOGIC is correct, and those are different claims. Stating them apart is
the point of the file header.

THE FAILURE THAT MATTERS MOST

`EvenG1Controller.arms` filters to whichever arms are connected. A dead right
arm therefore returns [left] rather than raising, and `send_text_packet` takes
`min(mtu)` across the survivors -- so the card renders on ONE lens with no
error raised anywhere. That is not "half an interruption", it is a different
and more alarming one: text appears on one side of the wearer's face with
nothing on the other, and they cannot tell whether the glasses are broken or
the system is broken.

The second failure is quieter. `_heartbeat_loop` used to swallow every
exception and log a warning, so a dead heartbeat task looked exactly like a
healthy link by any other observable. Age of the last confirmed heartbeat is
the only signal that tells them apart.

Run: ``.venv/bin/python experiments/link_gate.py``
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from interrupt import LinkStatus                      # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


HEALTHY = LinkStatus(left_ok=True, right_ok=True, mtu_left=247,
                     mtu_right=247, heartbeat_age_s=1.0)


def main() -> int:
    print("1. a good link is good")
    ok, why = HEALTHY.check()
    check("healthy link passes", ok, why)

    print("\n2. one arm is not a link")
    # The failure this exists for. Both directions, because left-only and
    # right-only are different code paths in the driver.
    for label, st in (
        ("right arm dead", LinkStatus(True, False, 247, 23, 1.0)),
        ("left arm dead", LinkStatus(False, True, 23, 247, 1.0)),
        ("neither", LinkStatus(False, False, 23, 23, 1.0)),
    ):
        ok, why = st.check()
        check(f"{label} -> REFUSED", not ok, why)

    one_armed = LinkStatus(True, False, 247, 23, 1.0)
    check("the refusal says WHY a partial card is refused",
          "one lens" in one_armed.check()[1],
          one_armed.check()[1])

    print("\n3. MTU is a physical precondition, not a preference")
    low = LinkStatus(True, True, 23, 23, 1.0)
    ok, why = low.check()
    check("un-negotiated MTU -> REFUSED", not ok, why)
    mixed = LinkStatus(True, True, 247, 23, 1.0)
    ok, why = mixed.check()
    check("one arm at default MTU -> REFUSED", not ok, why)
    # 24 is the first value above the 23-byte default.
    boundary = LinkStatus(True, True, 24, 24, 1.0)
    check("MTU 24 on both -> accepted (the boundary, stated not implied)",
          boundary.check()[0], boundary.check()[1])

    print("\n4. heartbeat staleness, and the silent version of a dead link")
    stale = LinkStatus(True, True, 247, 247, 45.0)
    ok, why = stale.check()
    check("stale heartbeat -> REFUSED", not ok, why)
    check("the reason names the ~32s drop", "32s" in why, why)

    never = LinkStatus(True, True, 247, 247, None)
    ok, why = never.check()
    check("never any heartbeat -> REFUSED", not ok, why)
    check("absence of a heartbeat is not evidence of a healthy link",
          "cannot prove" in why, why)

    aged = LinkStatus(True, True, 247, 247, LinkStatus.MAX_HEARTBEAT_AGE + 0.1)
    check("just past the limit is refused",
          not aged.check()[0], f"{aged.heartbeat_age_s}s")
    inside = LinkStatus(True, True, 247, 247,
                        LinkStatus.MAX_HEARTBEAT_AGE - 0.1)
    check("just inside the limit passes", inside.check()[0],
          f"{inside.heartbeat_age_s}s")

    print("\n5. a fault short-circuits everything")
    faulted = LinkStatus(True, True, 247, 247, 1.0, fault="driver gave up")
    ok, why = faulted.check()
    check("a reported fault -> REFUSED regardless of health", not ok, why)
    check("and it says so plainly", "fault" in why, why)

    print("\n6. the controller actually reports this")
    # Otherwise LinkStatus is a struct the driver never fills, which is the
    # unwired-code defect in a new costume.
    import g1_driver
    ctl = g1_driver.EvenG1Controller.__new__(g1_driver.EvenG1Controller)
    ctl.left = ctl.right = None
    ctl._fault = ""
    ctl._last_heartbeat = None
    ok, why = ctl.link_status().check()
    check("a disconnected controller refuses", not ok, why)

    class FakeArm:
        def __init__(self, up, mtu):
            self.is_connected, self.mtu = up, mtu

    ctl.left, ctl.right = FakeArm(True, 247), FakeArm(True, 247)
    ctl._last_heartbeat = time.monotonic() - 2
    ok, why = ctl.link_status().check()
    check("a healthy controller passes", ok, why)

    ctl.right = FakeArm(False, 23)
    ok, why = ctl.link_status().check()
    check("a half-connected controller refuses with the partial-card reason",
          (not ok) and "one lens" in why, why)

    ctl.right, ctl._last_heartbeat = FakeArm(True, 247), time.monotonic() - 60
    ok, why = ctl.link_status().check()
    check("a controller whose heartbeat died refuses", not ok, why)

    print("\n7. the check never raises, whatever it is handed")
    # It runs on a wearable loop. A gate that throws when the radio is in an
    # odd state is worse than no gate, because the failure is visible only as
    # a missing interruption.
    weird = [LinkStatus(), LinkStatus(True, True, 0, 0, 0.0),
             LinkStatus(True, True, 9999, 9999, -5.0),
             LinkStatus(True, True, 247, 247, 1.0, fault="")]
    for st in weird:
        try:
            ok, why = st.check()
            check(f"handles {st.left_ok}/{st.mtu_left}/{st.heartbeat_age_s}",
                  isinstance(ok, bool) and isinstance(why, str) and bool(why))
        except Exception as exc:
            check(f"handles {st.mtu_left}", False, f"raised {exc!r}")

    print()
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        return 1
    print("OK    refuses the partial card, the un-negotiated link and the "
          "silent one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())