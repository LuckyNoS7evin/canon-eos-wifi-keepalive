#!/usr/bin/env python3
"""End-to-end test against a fake PTP/IP Canon camera on localhost.

Runs eos_wifi_keepalive.py as a subprocess, lets it poll for a moment, stops
it with SIGTERM and checks the camera saw the right operations in order.
"""
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "eos_wifi_keepalive.py")


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def recv_packet(sock):
    length, ptype = struct.unpack("<II", recv_exact(sock, 8))
    return ptype, recv_exact(sock, length - 8)


def send_packet(sock, ptype, payload=b""):
    sock.sendall(struct.pack("<II", 8 + len(payload), ptype) + payload)


class FakeCamera(threading.Thread):
    def __init__(self, pair_delay=0.5):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.pair_delay = pair_delay
        self.ops = []
        self.guid = None
        self.name = None
        self.pong = False
        self.done = threading.Event()

    def run(self):
        cmd, _ = self.srv.accept()
        ptype, payload = recv_packet(cmd)
        assert ptype == 1, ptype
        self.guid = payload[:16]
        self.name = payload[16:-4].decode("utf-16-le").rstrip("\0")
        time.sleep(self.pair_delay)            # user pressing OK
        send_packet(cmd, 2, struct.pack("<I", 7) + b"\x11" * 16 +
                    "Canon EOS M50".encode("utf-16-le") + b"\0\0" +
                    struct.pack("<I", 0x10000))
        evt, _ = self.srv.accept()
        ptype, payload = recv_packet(evt)
        assert ptype == 3 and struct.unpack("<I", payload)[0] == 7
        send_packet(evt, 4)

        pinged = False
        try:
            while True:
                ptype, payload = recv_packet(cmd)
                assert ptype == 6, ptype
                _, op, tid = struct.unpack_from("<IHI", payload)
                self.ops.append(op)
                if op == 0x9116:               # GetEvent: empty event list
                    events = struct.pack("<II", 8, 0)
                    send_packet(cmd, 9, struct.pack("<IQ", tid, len(events)))
                    send_packet(cmd, 12, struct.pack("<I", tid) + events)
                    if not pinged:
                        send_packet(evt, 13)   # camera ping on event channel
                        pinged = True
                    evt.settimeout(0)
                    try:
                        if recv_packet(evt)[0] == 14:
                            self.pong = True
                    except (BlockingIOError, ConnectionError, OSError):
                        pass
                    evt.settimeout(None)
                send_packet(cmd, 7, struct.pack("<HI", 0x2001, tid))
                if op == 0x1003:               # CloseSession
                    break
        except ConnectionError:
            pass
        finally:
            self.done.set()


def main():
    cam = FakeCamera()
    cam.start()
    with tempfile.TemporaryDirectory() as tmp:
        guid_file = os.path.join(tmp, "guid")
        proc = subprocess.Popen(
            [sys.executable, TOOL, "--camera", "127.0.0.1", "--port", str(cam.port),
             "--guid-file", guid_file, "--poll", "0.05",
             "--keep-on-interval", "0.3", "--name", "testpc", "--once"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2.0)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert cam.done.wait(5), "camera never saw the session end"
        saved_guid = open(guid_file).read().strip()

    print(out)
    ops = cam.ops
    assert ops[:3] == [0x1002, 0x9114, 0x9115], [hex(o) for o in ops[:3]]
    assert ops.count(0x9116) >= 10, f"only {ops.count(0x9116)} GetEvent polls"
    assert ops.count(0x911D) >= 3, f"only {ops.count(0x911D)} KeepDeviceOn"
    assert ops[-1] == 0x1003, "no CloseSession on shutdown"
    assert cam.name == "testpc"
    assert cam.guid.hex() == saved_guid.replace("-", ""), "GUID not persisted"
    assert cam.pong, "no Pong for the camera's Ping"
    assert proc.returncode == 0, proc.returncode
    print(f"OK: {ops.count(0x9116)} polls, {ops.count(0x911D)} keep-alives, "
          "clean CloseSession, GUID persisted, ping answered")


if __name__ == "__main__":
    main()
