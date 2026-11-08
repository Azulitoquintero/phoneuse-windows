"""Client for the HID daemon's command protocol: hid_daemon.py's Unix socket on
Linux, PhoneHid.exe's TCP port (127.0.0.1:8791) on Windows."""
import os
import socket
import threading

from .config import HID_SOCKET

HID_TCP = ("127.0.0.1", 8791)


class Hid:
    """One connection to the daemon. Thread-safe: commands are serialized."""

    def __init__(self, path=HID_SOCKET):
        if os.name == "nt":
            self.sock = socket.create_connection(HID_TCP, timeout=10)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(path)
        self.f = self.sock.makefile(encoding="utf-8", newline="\n")
        self.lock = threading.Lock()

    def cmd(self, line):
        with self.lock:
            self.sock.sendall((line + "\n").encode("utf-8"))
            reply = self.f.readline().strip()
        if not reply.startswith("ok"):
            raise RuntimeError(reply)
        return reply
