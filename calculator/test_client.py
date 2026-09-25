#!/usr/bin/env python3
"""
Runs the core request sequence against server.py, plus the
framing edge cases that matter once the connection stays open.

Usage:  python3 test_client.py [host] [port]      (default localhost 8080)
"""

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080

failures = 0


def check(label, cond, detail=""):
    global failures
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures += 1
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))


class Conn:
    """One socket, plus the leftover bytes between responses."""

    def __init__(self):
        self.s = socket.create_connection((HOST, PORT))
        self.s.settimeout(5)
        self.buf = b""

    def send(self, data):
        self.s.sendall(data)

    def _fill(self):
        d = self.s.recv(4096)
        if not d:
            raise EOFError("server closed the connection")
        self.buf += d

    def read_response(self):
        """Parse exactly one response, using Content-Length to find its end."""
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for l in lines[1:]:
            k, _, v = l.partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0"))
        while len(self.buf) < n:
            self._fill()
        body, self.buf = self.buf[:n], self.buf[n:]
        return status, body.decode(), headers

    def still_open(self):
        """True if the peer has not sent FIN: a non-blocking peek finds no data and no EOF."""
        self.s.setblocking(False)
        try:
            data = self.s.recv(1, socket.MSG_PEEK)
            return len(data) > 0
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            self.s.settimeout(5)

    def close(self):
        self.s.close()


def req(method, path, host=True, extra=""):
    h = f"Host: {HOST}:{PORT}\r\n" if host else ""
    return f"{method} {path} HTTP/1.1\r\n{h}{extra}\r\n".encode()


CORE = [
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]


def test_core_sequence():
    print("\n1. Core sequence: one socket, six requests sent one at a time")
    c = Conn()
    for method, path, want_status, want_body in CORE:
        c.send(req(method, path))
        status, body, _ = c.read_response()
        ok = status == want_status and (want_body is None or body == want_body)
        check(f"{method} {path} -> {want_status} {want_body or ''}", ok, f"got {status} {body!r}")
    check("socket still open", c.still_open())
    c.close()


def test_full_feature_set():
    print("\n2. Full feature set from the task box")
    cases = [
        (req("GET", "/add?a=2&b=3"), 200, "5"),
        (req("GET", "/sub?a=10&b=4"), 200, "6"),
        (req("GET", "/mul?a=6&b=7"), 200, "42"),
        (req("GET", "/div?a=9&b=3"), 200, "3"),
        (req("GET", "/div?a=1&b=0"), 400, None),
        (req("GET", "/add?a=x&b=3"), 400, None),
        (req("GET", "/pow?a=2&b=8"), 404, None),
        (req("POST", "/add"), 405, None),
        (req("GET", "/add", host=False), 400, None),
    ]
    c = Conn()
    for raw, want_status, want_body in cases:
        c.send(raw)
        status, body, _ = c.read_response()
        first = raw.split(b"\r\n")[0].decode()
        ok = status == want_status and (want_body is None or body == want_body)
        check(f"{first} -> {want_status}", ok, f"got {status} {body!r}")
    check("socket still open after 9 requests", c.still_open())
    c.close()


def test_pipelining():
    print("\n3. Pipelining: all six in a single send, answers must come back in order")
    c = Conn()
    c.send(b"".join(req(m, p) for m, p, _, _ in CORE))
    for method, path, want_status, want_body in CORE:
        status, body, _ = c.read_response()
        ok = status == want_status and (want_body is None or body == want_body)
        check(f"{method} {path} -> {want_status}", ok, f"got {status} {body!r}")
    check("socket still open", c.still_open())
    c.close()


def test_body_does_not_bleed():
    print("\n4. Content-Length framing: byte n+1 belongs to the next request")
    c = Conn()
    evil = b"GET /pow?a=2&b=8 HTTP/1.1\r\nHost: x\r\n\r\n"
    post = (f"POST /add HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: {len(evil)}\r\n\r\n").encode() + evil
    c.send(post + req("GET", "/add?a=1&b=1"))
    s1, _, _ = c.read_response()
    s2, b2, _ = c.read_response()
    check("POST with request-shaped body -> 405", s1 == 405, f"got {s1}")
    check("next request parsed correctly -> 200 2", (s2, b2) == (200, "2"), f"got {s2} {b2!r}")
    c.close()


def test_fragmented():
    print("\n5. Request split across many TCP segments (one byte at a time)")
    c = Conn()
    c.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for byte in req("GET", "/mul?a=6&b=7"):
        c.send(bytes([byte]))
        time.sleep(0.001)
    s, b, _ = c.read_response()
    check("byte-by-byte request -> 200 42", (s, b) == (200, "42"), f"got {s} {b!r}")
    c.close()


def test_chunked():
    print("\n6. Chunked request body is consumed exactly")
    c = Conn()
    body = b"4\r\nWiki\r\n5;ext=1\r\npedia\r\n0\r\nX-Trailer: yes\r\n\r\n"
    c.send(f"POST /add HTTP/1.1\r\nHost: {HOST}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
           + body + req("GET", "/sub?a=10&b=4"))
    s1, _, _ = c.read_response()
    s2, b2, _ = c.read_response()
    check("chunked POST -> 405", s1 == 405, f"got {s1}")
    check("following request -> 200 6", (s2, b2) == (200, "6"), f"got {s2} {b2!r}")
    c.close()


def test_connection_close():
    print("\n7. Connection: close is honoured")
    c = Conn()
    c.send(req("GET", "/add?a=2&b=3", extra="Connection: close\r\n"))
    s, b, h = c.read_response()
    check("response 200 5", (s, b) == (200, "5"))
    check("response says Connection: close", h.get("connection") == "close")
    try:
        eof = c.s.recv(1) == b""
    except OSError:
        eof = True
    check("server closed the socket", eof)
    c.close()


def test_malformed_closes():
    print("\n8. Garbage request line -> 400, then close (the framing can't be trusted)")
    c = Conn()
    c.send(b"THIS IS NOT HTTP\r\n\r\n")
    s, _, h = c.read_response()
    check("400", s == 400, f"got {s}")
    check("Connection: close", h.get("connection") == "close")
    c.close()


def main():
    try:
        socket.create_connection((HOST, PORT), timeout=2).close()
    except OSError as e:
        sys.exit(f"cannot reach {HOST}:{PORT}: {e}  (is server.py running?)")

    test_core_sequence()
    test_full_feature_set()
    test_pipelining()
    test_body_does_not_bleed()
    test_fragmented()
    test_chunked()
    test_connection_close()
    test_malformed_closes()

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
