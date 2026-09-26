#!/usr/bin/env python3
"""Keep a Canon EOS camera awake over Wi-Fi by holding a PTP/IP session open.

This is what EOS Utility's Wi-Fi "remote control" connection does on
Windows/macOS: while a computer holds a remote session and polls for events,
the camera does not auto power off, and HDMI output keeps running.

Usage:
  eos_wifi_keepalive.py                 # discover the camera on the LAN
  eos_wifi_keepalive.py --camera 192.168.50.42

On the first connection the camera asks to pair with this computer: press OK
on the camera. The pairing identity (a GUID) is stored in
~/.config/eos-wifi-keepalive/guid so later connections are recognised.

Only the Python standard library is used.
"""
import argparse
import logging
import os
import re
import select
import signal
import socket
import struct
import sys
import time
import uuid

log = logging.getLogger("eos-wifi-keepalive")

PTPIP_PORT = 15740
PTPIP_VERSION = 0x00010000

# PTP/IP packet types (ISO 15740 / CIPA DC-005)
INIT_CMD_REQ, INIT_CMD_ACK, INIT_EVT_REQ, INIT_EVT_ACK, INIT_FAIL = 1, 2, 3, 4, 5
OP_REQ, OP_RESP, EVENT, START_DATA, DATA, CANCEL, END_DATA, PING, PONG = range(6, 15)

# Operation codes
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_EOS_SET_REMOTE_MODE = 0x9114
OC_EOS_SET_EVENT_MODE = 0x9115
OC_EOS_GET_EVENT = 0x9116
OC_EOS_KEEP_DEVICE_ON = 0x911D

RC_OK = 0x2001
RC_SESSION_ALREADY_OPEN = 0x201E

SSDP_ADDR = ("239.255.255.250", 1900)
CANON_ST = "urn:schemas-canon-com:service:ICPO-WFTEOSSystemService:1"


class PTPError(Exception):
    pass


def load_guid(path):
    """A stable 16-byte client GUID: it is the camera's pairing key."""
    try:
        with open(path) as f:
            return uuid.UUID(f.read().strip()).bytes
    except (OSError, ValueError):
        pass
    guid = uuid.uuid4()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(guid) + "\n")
    log.info("created new pairing identity %s (%s)", guid, path)
    return guid.bytes


def discover(timeout=5.0):
    """Ask the LAN for Canon EOS cameras; return the first camera IP found."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {CANON_ST}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        deadline = time.monotonic() + timeout
        s.sendto(msg, SSDP_ADDR)
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except socket.timeout:
                continue
            text = data.decode(errors="replace")
            if "canon" in text.lower() and CANON_ST.lower() in text.lower():
                m = re.search(r"^location:\s*http://([0-9.]+)", text, re.I | re.M)
                return m.group(1) if m else ip
        return None
    finally:
        s.close()


def utf16z(text):
    return text.encode("utf-16-le") + b"\0\0"


class PTPIPSession:
    """Minimal PTP/IP initiator: just enough for a Canon EOS remote session."""

    def __init__(self, host, guid, name, port=PTPIP_PORT, pair_timeout=90.0,
                 timeout=10.0):
        self.host = host
        self.port = port
        self.guid = guid
        self.name = name
        self.pair_timeout = pair_timeout
        self.timeout = timeout
        self.cmd = None
        self.evt = None
        self.tid = 0
        self.session_open = False

    # -- packet I/O ---------------------------------------------------------
    @staticmethod
    def _recv_exact(sock, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("camera closed the connection")
            buf += chunk
        return bytes(buf)

    def _recv(self, sock):
        length, ptype = struct.unpack("<II", self._recv_exact(sock, 8))
        if length < 8 or length > 64 << 20:
            raise PTPError(f"bad packet length {length}")
        return ptype, self._recv_exact(sock, length - 8)

    @staticmethod
    def _send(sock, ptype, payload=b""):
        sock.sendall(struct.pack("<II", 8 + len(payload), ptype) + payload)

    # -- connection ---------------------------------------------------------
    def connect(self):
        log.info("connecting to %s:%d", self.host, self.port)
        self.cmd = socket.create_connection((self.host, self.port), self.timeout)
        self.cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(self.cmd, INIT_CMD_REQ,
                   self.guid + utf16z(self.name) + struct.pack("<I", PTPIP_VERSION))

        # The camera answers only after the user accepts pairing (first time).
        self.cmd.settimeout(self.pair_timeout)
        log.info("waiting for the camera (press OK on the camera if it asks to pair)")
        ptype, payload = self._recv(self.cmd)
        self.cmd.settimeout(self.timeout)
        if ptype == INIT_FAIL:
            reason = struct.unpack_from("<I", payload)[0] if len(payload) >= 4 else 0
            raise PTPError(f"camera refused the connection (reason {reason:#x}); "
                           "is it in 'connect to computer' mode, and was pairing accepted?")
        if ptype != INIT_CMD_ACK:
            raise PTPError(f"unexpected init reply type {ptype}")
        conn_id = struct.unpack_from("<I", payload)[0]
        cam_name = payload[20:-4].decode("utf-16-le", errors="replace").rstrip("\0")
        log.info("camera accepted: %s (connection %d)", cam_name or "?", conn_id)

        self.evt = socket.create_connection((self.host, self.port), self.timeout)
        self._send(self.evt, INIT_EVT_REQ, struct.pack("<I", conn_id))
        ptype, _ = self._recv(self.evt)
        if ptype != INIT_EVT_ACK:
            raise PTPError(f"event channel refused (type {ptype})")

    def close(self):
        if self.session_open:
            try:
                self.transact(OC_CLOSE_SESSION)
            except Exception:  # best effort on the way out
                pass
            self.session_open = False
        for s in (self.evt, self.cmd):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.cmd = self.evt = None

    # -- operations ---------------------------------------------------------
    def service_event_channel(self):
        """Answer pings and drain anything the camera sends on the event socket."""
        while self.evt and select.select([self.evt], [], [], 0)[0]:
            ptype, _ = self._recv(self.evt)
            if ptype == PING:
                self._send(self.evt, PONG)

    def transact(self, opcode, *params):
        """Run one operation with no data-out phase. Returns (code, data)."""
        self.tid += 1
        tid = self.tid
        self._send(self.cmd, OP_REQ, struct.pack(
            f"<IHI{len(params)}I", 1, opcode, tid, *params))
        data = bytearray()
        while True:
            ptype, payload = self._recv(self.cmd)
            if ptype in (DATA, END_DATA):
                data += payload[4:]
            elif ptype == OP_RESP:
                code = struct.unpack_from("<H", payload)[0]
                return code, bytes(data)
            elif ptype == PING:
                self._send(self.cmd, PONG)
            elif ptype != START_DATA:
                raise PTPError(f"unexpected packet type {ptype} during {opcode:#06x}")

    def check(self, what, opcode, *params, allow=(RC_OK,)):
        code, data = self.transact(opcode, *params)
        if code not in allow:
            raise PTPError(f"{what} failed: response {code:#06x}")
        return data

    def start_remote(self, remote_mode=True):
        self.tid = 0
        code, _ = self.transact(OC_OPEN_SESSION, 1)
        if code not in (RC_OK, RC_SESSION_ALREADY_OPEN):
            raise PTPError(f"OpenSession failed: response {code:#06x}")
        self.session_open = True
        if remote_mode:
            self.check("SetRemoteMode", OC_EOS_SET_REMOTE_MODE, 1)
        self.check("SetEventMode", OC_EOS_SET_EVENT_MODE, 1)


class StopRequested(Exception):
    pass


def run_session(host, args, guid, stop):
    sess = PTPIPSession(host, guid, args.name, port=args.port,
                        pair_timeout=args.pair_timeout)
    try:
        sess.connect()
        sess.start_remote(remote_mode=not args.no_remote_mode)
        log.info("session open; keeping the camera awake (poll every %gs)", args.poll)
        keep_on_supported = True
        last_keep_on = 0.0
        while not stop["flag"]:
            sess.service_event_channel()
            sess.check("GetEvent", OC_EOS_GET_EVENT)
            now = time.monotonic()
            if keep_on_supported and now - last_keep_on >= args.keep_on_interval:
                code, _ = sess.transact(OC_EOS_KEEP_DEVICE_ON)
                if code != RC_OK:
                    log.info("KeepDeviceOn not supported (%#06x); relying on polling", code)
                    keep_on_supported = False
                last_keep_on = now
            time.sleep(args.poll)
    finally:
        sess.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--camera", help="camera IP (default: discover via SSDP)")
    ap.add_argument("--port", type=int, default=PTPIP_PORT, help=argparse.SUPPRESS)
    ap.add_argument("--name", default=socket.gethostname(),
                    help="computer name shown on the camera (default: hostname)")
    ap.add_argument("--guid-file", default=os.path.expanduser(
        "~/.config/eos-wifi-keepalive/guid"))
    ap.add_argument("--poll", type=float, default=1.0,
                    help="seconds between event polls (default 1)")
    ap.add_argument("--keep-on-interval", type=float, default=60.0,
                    help="seconds between KeepDeviceOn commands (default 60)")
    ap.add_argument("--pair-timeout", type=float, default=90.0,
                    help="seconds to wait for OK on the camera (default 90)")
    ap.add_argument("--retry", type=float, default=5.0,
                    help="seconds between reconnect attempts (default 5)")
    ap.add_argument("--no-remote-mode", action="store_true",
                    help="skip SetRemoteMode (try this if HDMI output changes)")
    ap.add_argument("--once", action="store_true",
                    help="exit instead of reconnecting when the session ends")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    guid = load_guid(args.guid_file)

    stop = {"flag": False}

    def on_signal(signum, frame):
        # Raising unwinds any blocking wait; run_session's finally still
        # closes the session so the camera leaves remote mode cleanly.
        stop["flag"] = True
        raise StopRequested()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        return loop(args, guid, stop)
    except StopRequested:
        log.info("stopped")
        return 0


def loop(args, guid, stop):
    while not stop["flag"]:
        host = args.camera or discover()
        if not host:
            log.debug("no camera found; is it in Wi-Fi 'connect to computer' mode?")
        else:
            try:
                run_session(host, args, guid, stop)
                if stop["flag"]:
                    break
                log.warning("session ended")
            except (OSError, ConnectionError, PTPError) as e:
                log.warning("session with %s ended: %s", host, e)
        if args.once:
            return 1
        # Sleep in small steps so a signal ends us promptly.
        for _ in range(int(args.retry * 10)):
            if stop["flag"]:
                break
            time.sleep(0.1)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
