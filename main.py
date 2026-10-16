#!/usr/bin/env python3
"""
main.py
=======

CLI entry point for the Even Realities G1 developer backend.

Default run::

    python main.py

Scans for the nearest G1 pair, connects both arms, pushes a short test string
to the waveguide, then stays connected for 30 seconds and echoes every TouchBar
packet that arrives as hex.

Examples::

    python main.py --selftest                # offline packet-format checks, no radio
    python main.py --scan-only               # list what is advertising
    python main.py --hold 120                # longer TouchBar capture session
    python main.py --text "hello" --verbose   # custom text + wire-level logging
    python main.py --dump-services           # GATT dump after connecting
    python main.py --battery                 # query both arms, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time

from bleak import BleakBackend
from bleak.backends import get_default_backend
from bleak.exc import BleakError

from g1_driver import EvenG1Controller, hexdump, selftest

log = logging.getLogger("g1")

DEFAULT_TEST_TEXT = "Terminal connection established. System online."
DEFAULT_HOLD_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Adapter diagnostics
# ---------------------------------------------------------------------------

def preflight() -> None:
    """
    Fail fast with an actionable message if the host cannot do BLE at all.

    Runs before the first scan so the user gets a Bluetooth problem instead of
    a 12-second timeout and an empty result.
    """
    try:
        backend = get_default_backend()
    except Exception as exc:
        print(f"\n  ! No usable BLE backend on this host: {exc}", file=sys.stderr)
        print("    bleak could not load a platform backend.", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"BLE backend: {backend}")

    if backend == BleakBackend.BLUEZ_DBUS:
        if not sys.platform.startswith("linux"):
            return
        # A D-Bus BLE object existing is the strongest cheap signal available
        # without shelling out to bluetoothctl.
        try:
            import dbus_fast  # noqa: F401  (import check only)
        except ImportError:
            print(
                "\n  ! dbus-fast is missing — bleak cannot talk to BlueZ.\n"
                "    fix: pip install -r requirements.txt",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(
            "  tip: ensure the adapter is powered on and your user is in the\n"
            "       'bluetooth' group (sudo usermod -aG bluetooth $USER)."
        )
    elif backend in (BleakBackend.WIN_RT, BleakBackend.CORE_BLUETOOTH):
        print("  MTU is negotiated automatically on this backend.")
    else:
        print(f"  ! Unrecognised BLE backend {backend!r}.", file=sys.stderr)


def humanise_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds >= 60:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds:.0f}s"


# ---------------------------------------------------------------------------
# Interactive phase
# ---------------------------------------------------------------------------

async def monitor_touchbars(controller: EvenG1Controller, hold: float) -> int:
    """
    Stay connected for ``hold`` seconds, counting TouchBar packets as they land.

    Returns the number of packets received, so the caller can report a useful
    summary instead of just "done".
    """
    deadline = time.monotonic() + hold
    print(
        f"\nConnected. Monitoring TouchBars for {humanise_duration(hold)} "
        f"— tap, double-tap or hold either bar."
    )
    print("Press Ctrl+C to finish early.\n")

    start = time.monotonic()
    last_report = start

    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break

        # Periodic progress line so an idle session is visibly alive.
        if now - last_report >= 10.0:
            last_report = now
            elapsed = now - start
            print(
                f"  ... {humanise_duration(elapsed)} elapsed, "
                f"{humanise_duration(remaining)} left, "
                f"{len(controller._packet_log)} packet(s) received"
            )

        await asyncio.sleep(0.1)

    elapsed = time.monotonic() - start
    print(
        f"\nMonitor window closed after {humanise_duration(elapsed)}: "
        f"{len(controller._packet_log)} packet(s) received."
    )
    return len(controller._packet_log)


def summarise_packets(controller: EvenG1Controller) -> None:
    """Print a per-command tally of everything received this session."""
    counts: dict[str, int] = {}
    for _side, data in controller._packet_log:
        if not data:
            continue
        key = f"0x{data[0]:02x}"
        if data[0] == 0xF5 and len(data) >= 2:
            key += f" 0x{data[1]:02x}"
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        print("No packets were received.")
        return

    print("\nPacket tally (command byte -> count):")
    for key, count in sorted(counts.items()):
        print(f"  {key:<8} {count}")

    if all(side == "L" for side, _ in controller._packet_log):
        print(
            "\n  note: only the LEFT arm sent packets. On BlueZ the MTU may "
            "have stayed at 23; try --verbose and check 'MTU=' in the log."
        )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    preflight()

    controller = EvenG1Controller(
        scan_timeout=args.scan_timeout,
        mtu=args.mtu,
        echo_packets=not args.quiet,
    )

    print(f"\nScanning for G1 arms for {args.scan_timeout:.0f}s ...")
    exit_code = 0

    try:
        if args.scan_only:
            result = await controller.discover(
                timeout=args.scan_timeout,
                require_both_arms=not args.allow_single_arm,
            )
            print(f"\n{result.describe()}  (in {result.elapsed:.1f}s)")
            for identity in result.identities.values():
                print(f"  {identity.label}")
            for identity in result.nus_only:
                print(f"  [nus-only, not claimed as G1] {identity.label}")
            if not result.channels:
                print(
                    "\nNothing matched. Confirm the glasses are powered on and "
                    "not bonded to the Even Realities app."
                )
                return 1
            return 0

        print("Connecting ...")
        result = await controller.connect(
            channel=args.channel,
            allow_single_arm=args.allow_single_arm,
        )
        print(f"  {result.describe()}")

        for arm in controller.arms:
            print(
                f"  {arm.identity.side} arm {arm.identity.address} "
                f"connected, MTU={arm.mtu}"
            )

        if len(controller.arms) < 2:
            print(
                "\n  ! Connected to only one arm. Text will appear on that side "
                "only.\n    Re-run with the other arm awake and unpaired."
            )

        if args.dump_services:
            controller.dump_services()

        if args.battery:
            levels = await controller.query_battery()
            print(f"\nBattery: {levels}")

        if args.text:
            print(f"\nSending: {args.text!r}")
            packets = await controller.send_text_packet(
                args.text,
                chars_per_line=args.chars_per_line,
                clear_first=args.clear,
                require_ack=args.require_ack,
                response=args.response_mode,
            )
            print(f"  {len(packets)} packet(s) written")
            if args.verbose:
                for index, packet in enumerate(packets):
                    print(f"    [{index}] {hexdump(packet)}")

        if args.hold > 0:
            await monitor_touchbars(controller, args.hold)
            summarise_packets(controller)

    except BleakError as exc:
        # Flush first so the error is not printed ahead of the progress lines
        # when stdout is block-buffered (piped output, CI logs).
        sys.stdout.flush()
        print(f"\nBLE error: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted — shutting down")
    except asyncio.CancelledError:
        print("\ncancelled — shutting down")
    finally:
        # Always tear the link down, even on failure, so BlueZ releases the
        # adapter and the next run can reconnect immediately.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.disconnect(), timeout=10.0)

    print("done.")
    return exit_code


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Scan for Even Realities G1 glasses, connect, render text, and "
            "echo TouchBar packets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py --selftest\n"
            "  python main.py --scan-only\n"
            "  python main.py --hold 120 --verbose\n"
            "  python main.py --text 'custom payload' --chars-per-line 30\n"
        ),
    )

    parser.add_argument(
        "--text", metavar="STR", nargs="?", const=DEFAULT_TEST_TEXT,
        default=DEFAULT_TEST_TEXT,
        help=f"text to render (default: {DEFAULT_TEST_TEXT!r})",
    )
    parser.add_argument(
        "--no-text", action="store_true",
        help="connect and monitor without sending any text",
    )
    parser.add_argument(
        "--hold", type=float, default=DEFAULT_HOLD_SECONDS, metavar="SEC",
        help=f"seconds to stay connected and log TouchBars (default: {DEFAULT_HOLD_SECONDS:.0f})",
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=12.0, metavar="SEC",
        help="seconds to scan for the glasses (default: 12)",
    )
    parser.add_argument(
        "--channel", metavar="XX",
        help="pin a specific G1 channel number (e.g. 07) instead of auto-selecting",
    )
    parser.add_argument(
        "--chars-per-line", type=int, default=24, metavar="N",
        help="characters per display line; the official app wraps at 488px / "
             "font 21, i.e. roughly 24 (default: 24)",
    )
    parser.add_argument(
        "--mtu", type=int, default=251, metavar="N",
        help="preferred ATT MTU (default: 251, as used by the official app)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="send the 0x18 clear-screen command before the text",
    )
    parser.add_argument(
        "--require-ack", action="store_true",
        help="wait for the glasses to acknowledge the text packets",
    )
    parser.add_argument(
        "--response", choices=("write", "no-response"), default=None,
        help="force a BLE write mode instead of choosing from characteristic "
             "properties (default: auto)",
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="scan and print what is advertising, then exit",
    )
    parser.add_argument(
        "--allow-single-arm", action="store_true",
        help="accept a channel with only one arm present",
    )
    parser.add_argument(
        "--dump-services", action="store_true",
        help="print the GATT service/characteristic table after connecting",
    )
    parser.add_argument(
        "--battery", action="store_true",
        help="query both arms for battery level",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="run offline packet-format checks and exit (no radio required)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="suppress per-packet RX lines",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug logging, including BLE writes and MTU negotiation",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # bleak is extremely chatty at DEBUG; keep its output behind our own flag.
    if not args.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)

    if args.selftest:
        return selftest()

    if args.no_text:
        args.text = None

    # argparse yields a string here; the driver wants True/False/None.
    response_mode: bool | None
    if args.response == "write":
        response_mode = True
    elif args.response == "no-response":
        response_mode = False
    else:
        response_mode = None
    args.response_mode = response_mode

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())