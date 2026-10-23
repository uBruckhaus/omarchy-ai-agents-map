"""loopback.stream_lines against hostile local servers: one deadline over connect, headers and body;
oversized lines, endless headers, drips, silence, errors and Cancel all end it."""
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import loopback  # noqa: E402


class Server:
    """A one-connection server on 127.0.0.1 that answers with `script(conn)`."""

    def __init__(self, script):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.script = script
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conn, _ = self.sock.accept()
        try:
            conn.recv(65536)
            self.script(conn)
        except OSError:
            pass
        finally:
            conn.close()
            self.sock.close()


def lines(server, **kw):
    kw.setdefault("deadline", time.monotonic() + 5)
    return list(loopback.stream_lines(server.port, "/api/pull", b"{}", **kw))


OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


class StreamTest(unittest.TestCase):
    def test_a_normal_chunked_reply(self):
        server = Server(lambda c: c.sendall(OK + chunk(b'{"status":"pulling"}\n{"status":"suc')
                                            + chunk(b'cess"}\n') + b"0\r\n\r\n"))
        self.assertEqual(lines(server), [b'{"status":"pulling"}\n', b'{"status":"success"}\n'])

    def drip(self, data, delay=0.05):
        def script(conn):
            for byte in data:
                conn.sendall(bytes([byte]))
                time.sleep(delay)
        return script

    def test_a_dripped_status_line_ends_by_the_header_deadline(self):
        server = Server(self.drip(b"HTTP/1.1 200 OK\r\n" * 100))
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            lines(server, head_within=0.5)
        self.assertLess(time.monotonic() - start, 2)

    def test_endless_header_lines(self):
        def script(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while True:
                conn.sendall(b"X-A: b\r\n")
        with self.assertRaises(Exception):
            lines(Server(script), head_within=1.0)

    def test_an_oversized_line_is_refused_not_drained(self):
        def script(conn):
            conn.sendall(OK)
            while True:
                conn.sendall(chunk(b"x" * 8192))
        with self.assertRaises(ValueError):
            lines(Server(script), max_line=65536)

    def test_a_dripped_body_ends_at_the_overall_deadline(self):
        server = Server(lambda c: (c.sendall(OK), self.drip(chunk(b"a") * 1000)(c)))
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            lines(server, deadline=time.monotonic() + 1.0)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_cancel_is_heard_while_bytes_trickle_in(self):
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        server = Server(lambda c: (c.sendall(OK), self.drip(chunk(b"a") * 1000)(c)))
        start = time.monotonic()
        with self.assertRaises(loopback.StreamCancelled):
            lines(server, deadline=time.monotonic() + 30, cancelled=stop.is_set)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_a_silent_server(self):
        server = Server(lambda c: time.sleep(3))
        with self.assertRaises(TimeoutError):
            lines(server, head_within=0.5)

    def test_an_error_status_and_too_many_lines(self):
        with self.assertRaises(ConnectionError):
            lines(Server(lambda c: c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")))
        server = Server(lambda c: c.sendall(OK + chunk(b"x\n" * 20) + b"0\r\n\r\n"))
        with self.assertRaises(ValueError):
            lines(server, max_lines=10)


if __name__ == "__main__":
    unittest.main()
