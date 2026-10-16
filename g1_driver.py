"""
g1_driver.py
============

Low-level BLE driver for the Even Realities G1 smart glasses, built on `bleak`.

Design goals
------------
* Minimal, readable, foundational. Every protocol constant that matters is
  named at the top of the file so it can be tweaked without reading logic.
* Pure packet construction is factored out into module-level functions that
  take bytes and return bytes, so the wire format can be unit-tested without
  a radio (see `selftest()` at the bottom of this file, and `main.py --selftest`).

Hardware reality checks (why this file looks the way it does)
------------------------------------------------------------
1. THE G1 IS *TWO* BLE PERIPHERALS, NOT ONE.
   Each arm carries its own radio and is discovered as a separate GATT server.
   The official app pairs them by a shared channel number embedded in the
   advertised local name:

       G1_<channel>_<L|R>_<serial>      e.g.  G1_07_L_S110LAAL103842

   A single connection only reaches one arm. Text has to be pushed to *both*
   arms to appear across the full waveguide, so `EvenG1Controller` owns a left
   arm and a right arm and fans writes out across the pair.

2. THE G1 WILL NOT EMIT NOTIFICATIONS UNTIL AN MTU IS NEGOTIATED.
   With an ATT MTU left at the 23-byte minimum the glasses stay silent, so
   TouchBar taps never reach us. bleak negotiates MTU automatically on Windows
   and macOS; the BlueZ backend always reports 23 and needs an explicit
   AcquireWrite call (a private API -- see `G1Arm._negotiate_mtu`, guarded so
   a bleak upgrade degrades to a warning instead of an exception).

3. THE LINK DROPS AFTER ~32 SECONDS WITHOUT A HEARTBEAT.
   `_heartbeat_loop` sends CMD_HEARTBEAT (0x25) on a timer well inside that
   window. Without it, a 30-second session dies at almost exactly the moment
   it would have finished.

4. THE G1 USES A STANDARD NORDIC UART SERVICE.
   Service 6e400001-... with a write characteristic 6e400002-... and a notify
   characteristic 6e400003-.... NUS is generic though: many unrelated devices
   advertise it, so name-pattern matches are ranked above UUID-only matches.

Protocol references
-------------------
* even-realities/EvenDemoApp README (official demo, protocol section)
* AGiXT/mobile "Even Realities G1 BLE Protocol.txt" (field-level reverse
  engineering incl. the 0x4E ByteBuffer layout reproduced below)

Neither document is a contractual API. Treat every constant here as a
*hypothesis to verify against your own hardware*, which is exactly what
`--verbose` in main.py is for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import textwrap
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

from bleak import BleakBackend, BleakClient, BleakScanner
from bleak.backends.scanner import AdvertisementData, BLEDevice
from bleak.exc import BleakError

log = logging.getLogger("g1")


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# Nordic UART Service (NUS) as implemented on both G1 arms.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
# Host -> glasses. Written by us.
NUS_TX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
# Glasses -> host. Subscribed to via CCCP notification.
NUS_RX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Command bytes (first byte of every packet).
CMD_TEXT = 0x4E      # render text on the waveguide
CMD_HEARTBEAT = 0x25  # keepalive; link dies after ~32s without one
CMD_BATTERY = 0x2C    # getter
CMD_HARD_RESET = 0x23  # 0x23 0x72 reboots the glasses; no response

# Sub-command for CMD_BATTERY.
SUBCMD_BATTERY = 0x01

# Generic acknowledgement status byte that follows an echoed command byte.
RSP_SUCCESS = 0xC9
RSP_FAILURE = 0xCA

# `newscreen` field of the 0x4E packet.
#   low nibble  0x01 = display new content
#   high nibble 0x70 = "Text Show" state
# => 0x71. Even AI uses 0x31/0x41/0x51/0x60 in the same slot.
SCREEN_TEXT_SHOW = 0x71

# Command byte used by spontaneous messages from the glasses, including every
# TouchBar interaction.
EVENT_CMD = 0xF5

# Decode table for 0xF5 sub-commands. Unmapped values still print as raw hex —
# this table is a convenience, not a requirement.
TOUCH_EVENT_NAMES: dict[int, str] = {
    0x00: "TouchBar double tap",
    0x01: "TouchBar single tap",
    0x02: "head up",
    0x03: "head down",
    0x04: "TouchBar triple tap",
    0x05: "TouchBar triple tap (alt)",
    0x06: "glasses worn",
    0x07: "glasses removed (not in case)",
    0x08: "placed in case, lid open",
    0x09: "case charging state",
    0x0A: "case charging state (alt)",
    0x0B: "placed in case, lid closed",
    0x0E: "case charging flag",
    0x0F: "case battery percent",
    0x11: "BLE paired",
    0x12: "BLE pairing event (alt)",
    0x17: "TouchBar held",
    0x18: "TouchBar released",
    0x1E: "open dashboard",
    0x1F: "close dashboard",
    0x20: "double tap (translate/transcribe mode)",
}

# Timing constants.
HEARTBEAT_INTERVAL = 10.0   # spec allows 28-30s; 10s is cheap and safe
LINK_TIMEOUT = 32.0         # observed auto-disconnect window
SCAN_TIMEOUT = 12.0         # default seconds to look for the glasses
CONNECT_RETRIES = 3
CONNECT_RETRY_BACKOFF = 2.0  # seconds, doubled each attempt
ACK_TIMEOUT = 2.0            # how long to wait for a command acknowledgement

# Preferred ATT MTU. The official app requests 251 after connecting.
PREFERRED_MTU = 251

# --- advertisement matching -------------------------------------------------

# The advertised name pattern documented by the official demo app.
G1_NAME_RE = re.compile(
    r"^G1[_-](?P<channel>[0-9A-Fa-f]{1,2})[_-](?P<side>[LR])(?:[_-](?P<serial>.*))?$",
    re.IGNORECASE,
)

# Name prefixes requested for matching, checked case-insensitively. These are
# kept in addition to the pattern above because firmware revisions have been
# seen advertising alternate strings.
G1_NAME_PREFIXES: tuple[str, ...] = ("even g1", "even realities")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def hexdump(data: bytes) -> str:
    """Format bytes as lowercase hex pairs separated by spaces."""
    return " ".join(f"{b:02x}" for b in data)


@dataclass(frozen=True)
class G1Identity:
    """Everything we can infer about one G1 arm from its advertisement."""

    channel: Optional[str]
    side: Optional[str]        # "L" or "R"
    serial: Optional[str]
    name: Optional[str]
    address: str
    rssi: int = -999

    @property
    def label(self) -> str:
        """Human-friendly arm label, e.g. ``LEFT  G1_07_L_S110...``."""
        side = {"L": "LEFT ", "R": "RIGHT"}.get((self.side or "").upper(), "?????")
        name = self.name or self.address
        return f"{side}  {name}  [{self.address}] rssi={self.rssi}"


def parse_identity(device: BLEDevice, adv: Optional[AdvertisementData]) -> G1Identity:
    """
    Derive a :class:`G1Identity` from a discovered device.

    The advertised local name is preferred over the OS-reported name, because
    on macOS the two differ and only the advertisement carries the
    ``G1_<channel>_<side>_<serial>`` structure.
    """
    name = (adv.local_name if adv else None) or device.name or ""
    match = G1_NAME_RE.match(name.strip())

    if match:
        return G1Identity(
            channel=match.group("channel").upper(),
            side=match.group("side").upper(),
            serial=match.group("serial") or None,
            name=name.strip(),
            address=device.address,
            rssi=adv.rssi if adv else -999,
        )

    # Name did not match the structured pattern. We may still be looking at a
    # G1 running different firmware; surface the name without guessing a side.
    return G1Identity(
        channel=None,
        side=None,
        serial=None,
        name=name.strip() or None,
        address=device.address,
        rssi=adv.rssi if adv else -999,
    )


def matches_g1(identity: G1Identity, adv: Optional[AdvertisementData]) -> bool:
    """
    True if this advertisement plausibly belongs to G1 hardware.

    Two tiers, in order of confidence:
      1. the structured ``G1_<channel>_<L|R>_<serial>`` name pattern;
      2. the Nordic UART service UUID in the advertisement payload.
    """
    if identity.channel and identity.side:
        return True
    if identity.name and identity.name.lower().startswith(G1_NAME_PREFIXES):
        return True
    if adv is not None:
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        uuids |= {u.lower() for u in (adv.service_data or {}).keys()}
        if NUS_SERVICE_UUID.lower() in uuids:
            return True
    return False


def has_nus(adv: Optional[AdvertisementData]) -> bool:
    """True if the advertisement itself advertises the Nordic UART service."""
    if adv is None:
        return False
    uuids = {u.lower() for u in (adv.service_uuids or [])}
    uuids |= {u.lower() for u in (adv.service_data or {}).keys()}
    return NUS_SERVICE_UUID.lower() in uuids


# ---------------------------------------------------------------------------
# Pure packet builders (no I/O — unit-testable)
# ---------------------------------------------------------------------------

def build_text_packet(
    text: str,
    *,
    seq: int = 0,
    packet_index: int = 0,
    total_packets: int = 1,
    page: int = 0,
    total_pages: int = 1,
    screen_status: int = SCREEN_TEXT_SHOW,
    char_pos: int = 0,
) -> bytes:
    """
    Wrap ``text`` in a single G1 text-rendering packet (command ``0x4E``).

    Layout (9-byte header + UTF-8 payload), reproduced from the ByteBuffer
    construction in the AGiXT protocol notes::

        +--------+-------------------+--------------------+------------------+
        | byte 0 | byte 1            | byte 2             | byte 3           |
        | 0x4E   | seq               | total_package_num  | current_package_ |
        |        |                   |                    | num              |
        +--------+-------------------+--------------------+------------------+
        | byte 4 | byte 5            | byte 6             | byte 7           |
        | screen | new_char_pos high | new_char_pos low   | current_page_num |
        | status |                   |                    |                  |
        +--------+-------------------+--------------------+------------------+
        | byte 8 | byte 9..                                                      |
        | max_   | UTF-8 text bytes                                             |
        | page   |                                                             |
        +--------+------------------------------------------------------------+

    ``char_pos`` is a 16-bit big-endian scroll position; 0 means "draw at the
    current position".

    Returns raw bytes; no BLE framing is applied here.
    """
    payload = text.encode("utf-8")
    header = bytes(
        [
            CMD_TEXT,
            seq & 0xFF,
            total_packets & 0xFF,
            packet_index & 0xFF,
            screen_status & 0xFF,
            (char_pos >> 8) & 0xFF,
            char_pos & 0xFF,
            page & 0xFF,
            total_pages & 0xFF,
        ]
    )
    return header + payload


def build_heartbeat(seq: int = 0) -> bytes:
    """Heartbeat packet. ``seq`` uses its own counter, independent of text seq."""
    return bytes([CMD_HEARTBEAT, seq & 0xFF])


def build_battery_query() -> bytes:
    """Request a battery report. Answer arrives as a 0x2C notification."""
    return bytes([CMD_BATTERY, SUBCMD_BATTERY])


def build_firmware_query() -> bytes:
    """Request firmware info. Response is raw ASCII starting with ``net``."""
    return bytes([0x23, 0x74])


def decode_touch_event(data: bytes) -> Optional[str]:
    """
    Best-effort decode of a spontaneous glasses->host notification.

    Returns a description string for recognised events, or ``None`` when the
    payload is not a known event (the caller still prints the raw hex).
    """
    if len(data) < 2 or data[0] != EVENT_CMD:
        return None
    sub = data[1]
    label = TOUCH_EVENT_NAMES.get(sub, "unmapped TouchBar event")
    # A few events carry a single trailing parameter (battery %, charging...).
    extra = ""
    if sub in (0x0F, 0x09, 0x0E) and len(data) >= 3:
        extra = f" (param=0x{data[2]:02x} / {data[2]})"
    return f"{label}{extra}"


def decode_status(data: bytes) -> Optional[str]:
    """
    Decode the generic response form: ``<command> <C9|CA> ...``.

    Used to confirm whether the glasses accepted a command.
    """
    if len(data) < 2:
        return None
    if data[1] == RSP_SUCCESS:
        return f"ack 0x{data[0]:02x} OK"
    if data[1] == RSP_FAILURE:
        return f"ack 0x{data[0]:02x} FAILED"
    return None


# ---------------------------------------------------------------------------
# One BLE link = one arm
# ---------------------------------------------------------------------------

class G1Arm:
    """
    A single connected G1 arm (left or right).

    Owns one :class:`bleak.BleakClient` and knows how to subscribe to the
    notify characteristic and write to the write characteristic.
    """

    def __init__(
        self,
        identity: G1Identity,
        *,
        mtu: int = PREFERRED_MTU,
        on_packet: Optional[Callable[["G1Arm", bytes], Awaitable[None] | None]] = None,
    ) -> None:
        self.identity = identity
        self.desired_mtu = mtu
        self._on_packet = on_packet
        self.client: Optional[BleakClient] = None
        self.mtu: int = 23
        self._connected = False
        self._seq: int = 0
        self._heartbeat_seq: int = 0
        self._rx_char = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def label(self) -> str:
        return self.identity.label

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, *, timeout: float = 15.0) -> None:
        """
        Open the GATT connection, negotiate MTU, then subscribe to
        notifications.

        Order matters: the glasses stay silent until the MTU is negotiated, so
        subscriptions are only useful *after* this step.
        """
        if self._connected:
            return

        log.debug("[%s] connecting to %s", self.identity.side, self.identity.address)
        client = BleakClient(
            self.identity.address,
            timeout=timeout,
            disconnected_callback=self._on_disconnected,
        )

        await client.connect()
        self.client = client
        self._connected = True

        await self._negotiate_mtu()
        await self._subscribe()

        log.info("[%s] connected, MTU=%d", self.identity.side, self.mtu)

    async def disconnect(self) -> None:
        """Tear the link down. Never raises."""
        client, self.client = self.client, None
        self._connected = False
        self._rx_char = None
        if client is None:
            return
        try:
            await client.disconnect()
            log.info("[%s] disconnected", self.identity.side)
        except BleakError as exc:
            log.warning("[%s] disconnect reported: %s", self.identity.side, exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[%s] unexpected disconnect error: %s", self.identity.side, exc)

    def _on_disconnected(self, _client: BleakClient) -> None:
        """Bleak callback. Fires on the backend's thread/task."""
        self._connected = False

    # -- MTU ---------------------------------------------------------------

    async def _negotiate_mtu(self) -> None:
        """
        Force MTU negotiation.

        bleak negotiates automatically on the WinRT and CoreBluetooth backends.
        The BlueZ backend has no public API for this and always reports 23, so
        we call the same private helper bleak's own ``mtu_size.py`` example uses.
        Anything unexpected here degrades to a warning: a 23-byte MTU still lets
        a short text packet through, it just means no notifications.
        """
        client = self.client
        if client is None:
            return

        self.mtu = 23
        try:
            if getattr(client, "backend_id", None) == BleakBackend.BLUEZ_DBUS:
                # Touching .services ensures discovery has happened, which the
                # BlueZ AcquireWrite call asserts on.
                _ = client.services
                await client._backend._acquire_mtu()  # type: ignore[attr-defined]
                self.mtu = client.mtu_size
            else:
                self.mtu = client.mtu_size

            if self.mtu < self.desired_mtu:
                log.debug(
                    "[%s] negotiated MTU %d (wanted %d)",
                    self.identity.side, self.mtu, self.desired_mtu,
                )
        except Exception as exc:
            log.warning(
                "[%s] MTU negotiation failed (%s); notifications may not arrive "
                "on this backend", self.identity.side, exc,
            )

    # -- notifications -----------------------------------------------------

    async def _subscribe(self) -> None:
        """Enable notifications on the NUS receive characteristic."""
        client = self.client
        if client is None:
            raise BleakError("not connected")

        char = self._find_characteristic(NUS_RX_CHAR_UUID)
        if char is None:
            raise BleakError(
                f"notify characteristic {NUS_RX_CHAR_UUID} not found; "
                f"available: {list(self.list_characteristics())}"
            )

        self._rx_char = char
        await client.start_notify(NUS_RX_CHAR_UUID, self._handle_notification)
        log.debug("[%s] subscribed to %s", self.identity.side, NUS_RX_CHAR_UUID)

    async def _handle_notification(
        self, characteristic, data: bytearray
    ) -> None:
        """
        bleak notification callback.

        Always hands the raw bytes to the controller, which is what prints the
        TouchBar hex you use to map input packets.
        """
        payload = bytes(data)
        if self._on_packet is not None:
            result = self._on_packet(self, payload)
            if asyncio.iscoroutine(result):
                await result

    # -- writes ------------------------------------------------------------

    def list_characteristics(self) -> Iterable[str]:
        """UUIDs of every characteristic visible on the connected link."""
        client = self.client
        if client is None:
            return ()
        try:
            return list(client.services.characteristics.keys())
        except BleakError:
            return ()

    def _find_characteristic(self, uuid: str):
        client = self.client
        if client is None:
            return None
        try:
            return client.services.characteristics.get(uuid.lower())
        except BleakError:
            return None

    def next_seq(self) -> int:
        """Global sequence counter shared by text packets."""
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    @property
    def max_payload(self) -> int:
        """
        Largest payload that fits in one BLE write.

        A write-without-response packet carries an ATT header of 3 bytes.
        The 9-byte 0x4E header is subtracted by the caller.
        """
        return max(1, self.mtu - 3)

    async def write(self, data: bytes, *, response: Optional[bool] = None) -> None:
        """
        Write one packet to the NUS transmit characteristic.

        ``response=None`` lets bleak pick write-with-response vs
        write-without-response from the characteristic properties. If that
        write is rejected we retry the other mode, because the two arms have
        been observed advertising different property sets.
        """
        client = self.client
        if client is None or not self._connected:
            raise BleakError(f"[{self.identity.side}] not connected")

        if len(data) > self.max_payload:
            raise ValueError(
                f"payload {len(data)}B exceeds ATT capacity {self.max_payload}B "
                f"(MTU {self.mtu}); split it before writing"
            )

        char = self._find_characteristic(NUS_TX_CHAR_UUID)
        modes: list[Optional[bool]]
        if response is not None:
            modes = [response]
        elif char is not None:
            props = [p.lower() for p in char.properties]
            if "write-without-response" in props:
                modes = [False, True]
            elif "write" in props:
                modes = [True, False]
            else:
                modes = [None]
        else:
            modes = [None]

        last_error: Optional[Exception] = None
        for mode in modes:
            try:
                await client.write_gatt_char(NUS_TX_CHAR_UUID, data, response=mode)
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "[%s] TX %s (%dB, response=%s)",
                        self.identity.side, hexdump(data), len(data), mode,
                    )
                return
            except (BleakError, OSError) as exc:
                last_error = exc
                log.debug(
                    "[%s] write failed with response=%s: %s",
                    self.identity.side, mode, exc,
                )

        raise BleakError(f"write to {NUS_TX_CHAR_UUID} failed: {last_error}")

    async def _controller_wait(
        self,
        predicate: Callable[[bytes], bool],
        *,
        since: int = 0,
    ) -> Optional[bytes]:
        """
        Wait on the owning controller's shared packet log.

        The controller is reachable through the bound ``_on_packet`` callback
        it handed us at construction time. An arm used standalone has no
        controller, in which case this simply times out to ``None``.
        """
        owner = getattr(self._on_packet, "__self__", None)
        if owner is None or not hasattr(owner, "wait_for"):
            return None
        return await owner.wait_for(predicate, since=since)

    async def send_heartbeat(self) -> None:
        """Send one heartbeat. Best-effort: a failure here is not fatal."""
        seq = self._heartbeat_seq
        self._heartbeat_seq = (self._heartbeat_seq + 1) & 0xFF
        with contextlib.suppress(BleakError, OSError):
            await self.write(build_heartbeat(seq))

    async def read_battery(self, *, since: int = 0) -> Optional[int]:
        """
        Request a battery report. Returns the percentage when the glasses
        answer before ``ACK_TIMEOUT``, otherwise ``None``.

        ``since`` should be the controller's packet-log length from before the
        query, so an older battery report is not misread as this answer.
        """
        await self.write(build_battery_query())
        raw = await self._controller_wait(
            lambda d: len(d) >= 3 and d[0] == CMD_BATTERY, since=since
        )
        if raw is None:
            return None
        # Layout per protocol notes: 2C <len> 00 <pad> <battery%> ...
        return raw[4] if len(raw) >= 5 else None


# ---------------------------------------------------------------------------
# Paired-arm controller
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    """Outcome of one scan: recognised arms grouped by channel number."""

    channels: dict[str, dict[str, BLEDevice]] = field(default_factory=dict)
    identities: dict[str, G1Identity] = field(default_factory=dict)
    nus_only: list[G1Identity] = field(default_factory=list)
    elapsed: float = 0.0

    def best_channel(self) -> Optional[str]:
        """Channel with both arms present, else the most complete one."""
        if not self.channels:
            return None
        complete = [c for c, arms in self.channels.items() if len(arms) >= 2]
        return complete[0] if complete else next(iter(self.channels))

    def describe(self) -> str:
        if not self.channels:
            return "no G1 advertisements seen"
        lines = []
        for channel, arms in sorted(self.channels.items()):
            sides = ",".join(sorted(side.upper() for side in arms))
            lines.append(f"channel {channel}: arms [{sides}]")
        return "; ".join(lines)


class EvenG1Controller:
    """
    Async facade over a matched pair of G1 arms.

    Typical use::

        controller = EvenG1Controller()
        try:
            await controller.connect()
            await controller.send_text_packet("Terminal connection established.")
            await asyncio.sleep(30)
        finally:
            await controller.disconnect()
    """

    def __init__(
        self,
        *,
        scan_timeout: float = SCAN_TIMEOUT,
        mtu: int = PREFERRED_MTU,
        echo_packets: bool = True,
    ) -> None:
        self.scan_timeout = scan_timeout
        self.mtu = mtu
        self.echo_packets = echo_packets

        self.left: Optional[G1Arm] = None
        self.right: Optional[G1Arm] = None
        self.channel: Optional[str] = None

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._packet_log: list[tuple[str, bytes]] = []
        self._listeners: list[Callable[[str, bytes], None]] = []

    def add_listener(self, fn: Callable[[str, bytes], None]) -> Callable[[str, bytes], None]:
        """
        Register a synchronous observer for incoming packets.

        Listeners receive ``(side, raw_bytes)`` for every notification, after
        the packet has been recorded and printed. They must not block: the
        callback runs on the BLE notification path. Schedule any real work as a
        task. Returns the callback so it can be used as a decorator.
        """
        self._listeners.append(fn)
        return fn

    # -- discovery ---------------------------------------------------------

    async def discover(
        self,
        *,
        timeout: Optional[float] = None,
        require_both_arms: bool = True,
    ) -> DiscoveryResult:
        """
        Scan for G1 advertisements and group the arms by channel.

        ``require_both_arms`` picks a channel that has both a left and a right
        arm. When it is False, a single-arm channel is acceptable, which is
        occasionally all that is advertising if the other arm is already
        bonded to another host.

        NUS-only matches (any device advertising the Nordic UART service) are
        recorded in ``result.nus_only`` but never auto-selected: NUS is a
        generic service and unrelated hardware advertises it too.
        """
        timeout = self.scan_timeout if timeout is None else timeout
        result = DiscoveryResult()
        seen: dict[str, G1Identity] = {}
        started = time.monotonic()

        def on_advertisement(device: BLEDevice, adv: AdvertisementData) -> None:
            identity = parse_identity(device, adv)

            if not matches_g1(identity, adv):
                return

            # Weak match: NUS UUID only. Track it, but do not treat it as G1.
            if not (identity.channel and identity.side):
                if has_nus(adv) and not result.nus_only:
                    log.info(
                        "NUS-only advertisement (not claiming it is a G1): "
                        "name=%r address=%s rssi=%d",
                        identity.name, identity.address, identity.rssi,
                    )
                    result.nus_only.append(identity)
                return

            # Keep the strongest advertisement per address.
            previous = seen.get(device.address)
            if previous is None or identity.rssi > previous.rssi:
                seen[device.address] = identity

            bucket = result.channels.setdefault(identity.channel or "??", {})
            bucket[identity.side or "?"] = device
            result.identities[device.address] = identity
            log.info("discovered %s", identity.label)

        scanner = BleakScanner(detection_callback=on_advertisement)
        await scanner.start()
        try:
            await asyncio.sleep(timeout)
        finally:
            await scanner.stop()

        result.elapsed = time.monotonic() - started

        # Drop channels that cannot satisfy the caller's requirement.
        if require_both_arms:
            for channel in [c for c, arms in result.channels.items() if len(arms) < 2]:
                for device in result.channels.pop(channel).values():
                    result.identities.pop(device.address, None)

        return result

    # -- connection --------------------------------------------------------

    def _make_arm(self, identity: G1Identity) -> G1Arm:
        return G1Arm(identity, mtu=self.mtu, on_packet=self._on_packet)

    async def connect(
        self,
        *,
        channel: Optional[str] = None,
        scan_timeout: Optional[float] = None,
        allow_single_arm: bool = False,
    ) -> DiscoveryResult:
        """
        Scan, pair the arms by channel, and connect both.

        ``channel`` pins a specific channel number; otherwise the first
        channel with both arms is used. Raises :class:`BleakError` when nothing
        suitable is found, and disconnects anything already open on failure so
        the caller never has to unwind a half-open pair.
        """
        result = await self.discover(
            timeout=scan_timeout, require_both_arms=not allow_single_arm
        )

        chosen = channel or result.best_channel()
        if chosen is None or chosen not in result.channels:
            await self.disconnect()
            raise BleakError(
                "no G1 found during scan. "
                f"Saw: {result.describe() or 'nothing'}. "
                "Check the glasses are powered on, unpaired from the Even app, "
                "and in range; on Linux confirm the adapter is up and your user "
                "is in the 'bluetooth' group."
            )

        self.channel = chosen
        arms = result.channels[chosen]
        log.info("connecting to channel %s (%s)", chosen, ", ".join(sorted(arms)))

        # Left first: the official app requires the left arm to be written to
        # before the right, and the arms are independent links.
        pending = []
        for side, key in (("L", "left"), ("R", "right")):
            device = arms.get(side) or arms.get(side.lower())
            if device is None:
                continue
            identity = result.identities.get(device.address)
            if identity is None:
                continue
            arm = self._make_arm(identity)
            setattr(self, key, arm)
            pending.append(arm)

        if not pending:
            await self.disconnect()
            raise BleakError(f"channel {chosen} resolved to no usable arms")

        connected: list[G1Arm] = []
        try:
            for arm in pending:
                await self._connect_arm(arm)
                connected.append(arm)
        except Exception:
            # Unwind the half-open pair before re-raising.
            for arm in connected:
                await arm.disconnect()
            await self.disconnect()
            raise

        self._start_heartbeat()
        return result

    async def _connect_arm(self, arm: G1Arm) -> None:
        """Connect one arm, retrying with exponential backoff."""
        last_error: Optional[Exception] = None
        backoff = CONNECT_RETRY_BACKOFF

        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                await arm.connect()
                return
            except (BleakError, OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning(
                    "connect attempt %d/%d for %s failed: %s",
                    attempt, CONNECT_RETRIES, arm.identity.address, exc,
                )
                await arm.disconnect()
                if attempt < CONNECT_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2

        raise BleakError(
            f"could not connect to {arm.identity.address} after "
            f"{CONNECT_RETRIES} attempts: {last_error}"
        )

    async def disconnect(self) -> None:
        """Stop the heartbeat and close every arm. Safe to call twice."""
        await self._stop_heartbeat()

        arms = [a for a in (self.left, self.right) if a is not None]
        if arms:
            await asyncio.gather(
                *(a.disconnect() for a in arms), return_exceptions=True
            )

        self.left = None
        self.right = None
        self.channel = None

    async def __aenter__(self) -> "EvenG1Controller":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    @property
    def arms(self) -> list[G1Arm]:
        """Currently connected arms, left first."""
        return [a for a in (self.left, self.right) if a is not None and a.is_connected]

    @property
    def is_connected(self) -> bool:
        return bool(self.arms)

    # -- heartbeat ---------------------------------------------------------

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _heartbeat_loop(self) -> None:
        """Keep the link alive; the G1 drops the connection after ~32s idle."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                for arm in self.arms:
                    await arm.send_heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("heartbeat loop stopped: %s", exc)

    # -- packet reception --------------------------------------------------

    def _on_packet(self, arm: G1Arm, data: bytes) -> None:
        """
        Called by each arm for every notification.

        Records the packet and prints raw hex. This is the hook to instrument
        while you map out TouchBar input packets.
        """
        self._packet_log.append((arm.identity.side or "?", data))

        # Observers run after recording and printing, and are isolated: one
        # misbehaving listener must not break the BLE notification path.
        for listener in tuple(self._listeners):
            try:
                listener(arm.identity.side or "?", data)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("packet listener raised: %s", exc)

        if not self.echo_packets:
            return

        stamp = time.strftime("%H:%M:%S")
        side = (arm.identity.side or "?").upper()

        touch = decode_touch_event(data)
        status = decode_status(data)

        if touch:
            print(f"[{stamp}] [{side:^5}] RX  {hexdump(data):<40} | {touch}")
        elif status:
            print(f"[{stamp}] [{side:^5}] RX  {hexdump(data):<40} | {status}")
        else:
            print(f"[{stamp}] [{side:^5}] RX  {hexdump(data)}")

    async def wait_for(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = ACK_TIMEOUT,
        since: int = 0,
    ) -> Optional[bytes]:
        """
        Wait for a notification matching ``predicate``.

        ``since`` is an index into :attr:`_packet_log`. Pass the log length
        captured *before* triggering a command so a stale ack from an earlier
        exchange cannot be mistaken for this one.

        Returns the payload, or ``None`` on timeout.
        """
        deadline = time.monotonic() + timeout
        # The reply may already have landed between the write and this call.
        for _side, data in reversed(self._packet_log[since:]):
            if predicate(data):
                return data
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.05, remaining))
            for _side, data in reversed(self._packet_log[since:]):
                if predicate(data):
                    return data

    # -- writes ------------------------------------------------------------

    async def send_text_packet(
        self,
        text: str,
        *,
        chars_per_line: int = 24,
        clear_first: bool = False,
        require_ack: bool = False,
        response: Optional[bool] = None,
    ) -> list[bytes]:
        """
        Render ``text`` on the waveguide.

        The string is wrapped to ``chars_per_line`` and split into as many 0x4E
        packets as the negotiated MTU requires. Packs go to the left arm first,
        then the right arm, matching the ordering the official app uses.

        ``chars_per_line`` approximates the official app's pixel-based wrapping
        (488px display width at font size 21, so roughly 24 characters). Without
        the real font metrics this is an estimate -- tune it and watch how the
        line breaks land.

        Returns the list of packets that were written, for inspection.
        """
        arms = self.arms
        if not arms:
            raise BleakError("not connected")

        packets = self.build_text_packets(
            text,
            chars_per_line=chars_per_line,
            mtu=min(a.mtu for a in arms),
        )

        if clear_first:
            packets = [bytes([0x18])] + packets  # 0x18 clears screen/text

        # Mark the log so a following ack wait only inspects fresh packets.
        since = len(self._packet_log)

        log.info("sending %d packet(s) of text", len(packets))
        for arm in arms:
            for packet in packets:
                await arm.write(packet, response=response)
                # Pacing: back-to-back writes can overrun the arm's UART FIFO.
                await asyncio.sleep(0.03)
            log.info("[%s] sent %d packet(s)", arm.identity.side, len(packets))

        if require_ack:
            acked = await self.wait_for(
                lambda d: (
                    len(d) >= 2
                    and d[0] == CMD_TEXT
                    and d[1] in (RSP_SUCCESS, RSP_FAILURE)
                ),
                timeout=ACK_TIMEOUT,
                since=since,
            )
            if acked is None:
                log.warning("no acknowledgement for the text packets")
            else:
                state = "OK" if acked[1] == RSP_SUCCESS else "FAILED"
                log.info("glasses acknowledged the text packet: %s", state)

        return packets

    def build_text_packets(
        self,
        text: str,
        *,
        chars_per_line: int = 24,
        mtu: int = PREFERRED_MTU,
    ) -> list[bytes]:
        """
        Split ``text`` into MTU-sized 0x4E packets.

        The 9-byte command header and the 3-byte ATT write header are
        subtracted from ``mtu`` before chunking the UTF-8 payload.
        """
        capacity = max(1, mtu - 3 - 9)
        encoded = text.encode("utf-8")

        # Wrap on characters, then re-pack so no packet exceeds the capacity.
        lines = textwrap.wrap(text, width=chars_per_line) or [""]
        chunks: list[bytes] = []
        buffer = b""
        for line in lines:
            encoded_line = line.encode("utf-8")
            if buffer and len(buffer) + 1 + len(encoded_line) > capacity:
                chunks.append(buffer)
                buffer = encoded_line
            else:
                buffer = encoded_line if not buffer else buffer + b" " + encoded_line
        if buffer:
            chunks.append(buffer)

        total = len(chunks)
        seq = 0
        packets: list[bytes] = []
        for index, chunk in enumerate(chunks):
            packets.append(
                build_text_packet(
                    chunk.decode("utf-8", errors="replace"),
                    seq=seq,
                    packet_index=index,
                    total_packets=total,
                    page=index,
                    total_pages=total,
                )
            )
            seq = (seq + 1) & 0xFF
        return packets

    async def send_raw(self, packet: bytes, *, response: Optional[bool] = None) -> None:
        """Write an arbitrary packet to every arm. Useful while experimenting."""
        for arm in self.arms:
            await arm.write(packet, response=response)

    async def query_battery(self) -> dict[str, Optional[int]]:
        """Ask both arms for their battery percentage. Returns ``{side: pct}``."""
        out: dict[str, Optional[int]] = {}
        for arm in self.arms:
            since = len(self._packet_log)
            try:
                out[arm.identity.side or "?"] = await arm.read_battery(since=since)
            except (BleakError, OSError) as exc:
                log.warning("battery query failed for %s: %s", arm.identity.side, exc)
                out[arm.identity.side or "?"] = None
        return out

    # -- diagnostics -------------------------------------------------------

    def dump_services(self) -> None:
        """Print every service/characteristic the connected arms expose."""
        for arm in self.arms:
            print(f"\n=== {arm.identity.side} arm {arm.identity.address} ===")
            try:
                services = arm.client.services  # type: ignore[union-attr]
            except BleakError as exc:
                print(f"  services unavailable: {exc}")
                continue
            for service in services:
                print(f"  service {service.uuid}")
                for char in service.characteristics:
                    print(f"    {char.uuid}  props={list(char.properties)}")


# ---------------------------------------------------------------------------
# Offline self-test
# ---------------------------------------------------------------------------

def selftest() -> int:
    """
    Verify packet construction without a radio.

    Run with ``python main.py --selftest``. Returns a process exit code.
    """
    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual == expected:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}\n         expected {expected!r}\n         actual   {actual!r}")
            failures.append(label)

    print("identity parsing")
    ident = parse_identity(
        _FakeDevice("AA:BB:CC:DD:EE:FF", "ignored"),
        _FakeAdv(local_name="G1_07_L_S110LAAL103842", rssi=-55),
    )
    check("channel", ident.channel, "07")
    check("side", ident.side, "L")
    check("serial", ident.serial, "S110LAAL103842")

    ident_r = parse_identity(
        _FakeDevice("11:22:33:44:55:66", "ignored"),
        _FakeAdv(local_name="G1_07_R_S110LAAL103842", rssi=-60),
    )
    check("right side", ident_r.side, "R")
    check("both arms match", (ident.channel == ident_r.channel), True)

    print("\npacket building")
    packet = build_text_packet("Hello")
    check("header length", len(packet), 9 + len("Hello"))
    check("command byte", packet[0], CMD_TEXT)
    check("status byte", packet[4], SCREEN_TEXT_SHOW)
    check("payload", packet[9:], b"Hello")

    utf8 = build_text_packet("héllo → 世界")
    check("utf-8 payload round-trips", utf8[9:].decode("utf-8"), "héllo → 世界")

    print("\nchunking")
    controller = EvenG1Controller()
    many = controller.build_text_packets("word " * 200, chars_per_line=24, mtu=251)
    check("all packets fit MTU", all(len(p) <= 248 for p in many), True)
    check("produced multiple packets", len(many) > 1, True)
    check("packet count matches header", all(p[2] == len(many) for p in many), True)
    check(
        "indices are ordered",
        [p[3] for p in many],
        list(range(len(many))),
    )

    print("\nevent decoding")
    check("single tap", decode_touch_event(bytes([0xF5, 0x01])), "TouchBar single tap")
    check("held", decode_touch_event(bytes([0xF5, 0x17])), "TouchBar held")
    check("non-event", decode_touch_event(bytes([0x2C, 0x66, 0x37])), None)

    print()
    if failures:
        print(f"selftest FAILED: {len(failures)} check(s) failed")
        return 1
    print("selftest passed")
    return 0


class _FakeDevice:
    """Minimal stand-in for bleak's BLEDevice, for offline tests."""

    def __init__(self, address: str, name: str) -> None:
        self.address = address
        self.name = name
        self.details = {}


class _FakeAdv:
    """Minimal stand-in for bleak's AdvertisementData, for offline tests."""

    def __init__(
        self,
        local_name: str,
        rssi: int = -50,
        service_uuids: Optional[list[str]] = None,
        service_data: Optional[dict[str, bytes]] = None,
    ) -> None:
        self.local_name = local_name
        self.rssi = rssi
        self.service_uuids = service_uuids or []
        self.service_data = service_data or {}
        self.manufacturer_data: dict[int, bytes] = {}
        self.tx_power = None
        self.platform_data = ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(selftest())