"""One deadline for a whole local HTTP exchange (HANCORE review of 3452749: a reply that
trickles in below the socket timeout kept the status worker far beyond its budget)."""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gpu_usage  # noqa: E402
import loopback  # noqa: E402
import provider_watch  # noqa: E402


class Server:
    """A loopback server: 'trickle' sends headers then one body byte every 50 ms (forever),
    'silent' accepts and sends nothing, 'ok' answers at once."""

    def __init__(self, mode: str):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.closed = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        conn.settimeout(0.2)
        try:
            conn.recv(65536)
            if self.mode == "ok":
                body = b'{"ok": true}'
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                             + body)
            elif self.mode in ("trickle", "close-trickle"):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n"
                             + (b"Connection: close\r\n" if self.mode == "close-trickle" else b"") + b"\r\n")
                while not self.stop.is_set():
                    conn.sendall(b" ")
                    time.sleep(0.05)
            else:
                while not self.stop.is_set():
                    try:
                        if conn.recv(1) == b"":
                            break
                    except socket.timeout:
                        continue
        except OSError:
            self.closed.set()          # the client went away
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


class DeadlineTest(unittest.TestCase):
    def serve(self, mode):
        server = Server(mode)
        self.addCleanup(server.close)
        return server

    def test_trickling_reply_stops_at_the_total_budget(self):
        server = self.serve("trickle")
        started = time.monotonic()
        reply = loopback.bounded_request(server.port, "GET", "/x", None, total=0.6, max_bytes=1 << 20)
        self.assertIsNone(reply)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(server.closed.wait(2))      # the socket was really closed

    def test_trickle_after_connection_close_headers(self):
        server = self.serve("close-trickle")     # getresponse() drops connection.sock here
        started = time.monotonic()
        self.assertIsNone(loopback.bounded_request(server.port, "GET", "/x", None, total=0.6, max_bytes=1 << 20))
        self.assertLess(time.monotonic() - started, 1.5)
        entry = {"unload": {"path": "/u", "body": {}}}
        started = time.monotonic()
        with mock.patch.object(provider_watch, "own_listener", return_value=True), \
                mock.patch.object(provider_watch, "UNLOAD_TOTAL", 0.5):
            self.assertFalse(provider_watch.unload(entry, server.port))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_silent_server_stops_at_the_total_budget(self):
        server = self.serve("silent")
        started = time.monotonic()
        self.assertIsNone(loopback.bounded_request(server.port, "GET", "/", None, total=0.5, max_bytes=100))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_normal_reply_and_a_later_poll(self):
        server = self.serve("ok")
        with mock.patch.object(gpu_usage, "_own_port", return_value=True):
            self.assertEqual(gpu_usage.http_json(server.port, "/", timeout=2), {"ok": True})
            self.assertEqual(gpu_usage.http_json(server.port, "/", timeout=2), {"ok": True})

    def test_gpu_http_json_trickle(self):
        server = self.serve("trickle")
        started = time.monotonic()
        with mock.patch.object(gpu_usage, "_own_port", return_value=True):
            self.assertIsNone(gpu_usage.http_json(server.port, "/", timeout=0.5))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_watcher_unload_trickle(self):
        server = self.serve("trickle")
        entry = {"unload": {"path": "/api/extra/unload", "body": {}}}
        started = time.monotonic()
        with mock.patch.object(provider_watch, "own_listener", return_value=True), \
                mock.patch.object(provider_watch, "UNLOAD_TOTAL", 0.5):
            self.assertFalse(provider_watch.unload(entry, server.port))
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
