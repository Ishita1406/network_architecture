#!/usr/bin/env python3
"""
Conformance tests for bserve and bcurl against SPEC.md.

Starts ./bserve on a free port, then checks it two ways: with a raw-socket
peer that builds frames by hand (a third implementation of the spec, sharing
no code with either program), and by running ./bcurl.

    python3 test_bhttp.py
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PREFACE = b"BHTP\x01\x00\x00\x00"
HEADERS, DATA, GOAWAY = 1, 2, 3
END = 1

failures = 0


def check(label, cond, detail=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def frame(ftype, flags, rid, payload=b""):
    return struct.pack(">HBBI", len(payload), ftype, flags, rid) + payload


def field(index_or_name, value):
    v = value.encode()
    if isinstance(index_or_name, int):
        head = bytes([index_or_name])
    else:
        n = index_or_name.encode()
        head = bytes([0, len(n)]) + n
    return head + struct.pack(">H", len(v)) + v


def get(rid, path, method="GET", end=True):
    return frame(HEADERS, END if end else 0, rid,
                 field(1, method) + field(2, path) + field(4, "test"))


class Peer:
    def __init__(self, port, preface=PREFACE):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""
        if preface is not None:
            self.s.sendall(preface)
            check("server echoes preface", self.recv_exact(8) == PREFACE)

    def recv_exact(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        length, ftype, flags, rid = struct.unpack(">HBBI", self.recv_exact(8))
        return ftype, flags, rid, self.recv_exact(length)

    def response(self):
        """Read one full response. Returns (rid, status, fields, body)."""
        ftype, flags, rid, payload = self.frame()
        assert ftype == HEADERS, f"expected HEADERS, got type {ftype}"
        fields, i = {}, 0
        while i < len(payload):
            b = payload[i]; i += 1
            if b == 0:
                n = payload[i]; name = payload[i + 1:i + 1 + n].decode(); i += 1 + n
            else:
                name = b
            (vl,) = struct.unpack_from(">H", payload, i); i += 2
            fields[name] = payload[i:i + vl].decode("latin-1"); i += vl
        body = b""
        while not flags & END:
            ftype, flags, drid, payload = self.frame()
            assert ftype == DATA and drid == rid
            body += payload
        return rid, int(fields[3]), fields, body

    def open(self):
        self.s.settimeout(0.2)
        try:
            return self.s.recv(1, socket.MSG_PEEK) != b""
        except socket.timeout:
            return True
        except OSError:
            return False
        finally:
            self.s.settimeout(5)

    def close(self):
        self.s.close()


def run_tests(port, root):
    index = open(os.path.join(root, "index.html"), "rb").read()

    print("\n1. One connection, many requests")
    p = Peer(port)
    for rid, (path, want) in enumerate([("/index.html", 200), ("/", 200), ("/missing", 404),
                                        ("/style.css", 200)], start=1):
        p.s.sendall(get(rid, path))
        r, status, _, _ = p.response()
        check(f"#{rid} GET {path} -> {want}", (r, status) == (rid, want), f"got #{r} {status}")
    check("connection still open", p.open())
    p.close()

    print("\n2. Body and headers")
    p = Peer(port)
    p.s.sendall(get(7, "/index.html"))
    _, status, fields, body = p.response()
    check("body matches file", body == index)
    check("content-length matches", fields.get(6) == str(len(index)))
    check("content-type text/html", fields.get(5, "").startswith("text/html"))
    p.close()

    print("\n3. Large file is split across many DATA frames")
    p = Peer(port)
    p.s.sendall(get(1, "/big.bin"))
    ftype, flags, _, _ = p.frame()
    count, body = 0, b""
    while not flags & END:
        ftype, flags, _, payload = p.frame()
        body += payload
        count += 1
    want = open(os.path.join(root, "big.bin"), "rb").read()
    check(f"{len(want)} bytes reassembled correctly", body == want)
    check("used more than one DATA frame", count > 1, f"{count} frames")
    p.close()

    print("\n4. Unknown frame types are skipped cleanly")
    p = Peer(port)
    junk = frame(0x7F, 0xFF, 1, b"\xff" * 300) + frame(0x42, 0, 0)
    p.s.sendall(junk + get(1, "/index.html") + frame(0xEE, 0, 99, b"future") + get(2, "/style.css"))
    r1 = p.response()
    r2 = p.response()
    check("request after unknown frame -> 200", (r1[0], r1[1]) == (1, 200))
    check("request after second unknown frame -> 200", (r2[0], r2[1]) == (2, 200))
    check("connection still open", p.open())
    p.close()

    print("\n5. Malformed HEADERS -> 400, connection survives")
    p = Peer(port)
    bad = [
        ("unknown name index 0x63", frame(HEADERS, END, 1, field(1, "GET") + b"\x63\x00\x00")),
        ("value runs past frame", frame(HEADERS, END, 2, field(1, "GET") + b"\x02\x00\xff/x")),
        ("missing :path", frame(HEADERS, END, 3, field(1, "GET"))),
        ("path without /", frame(HEADERS, END, 4, field(1, "GET") + field(2, "index.html"))),
        ("duplicate :method", frame(HEADERS, END, 5, field(1, "GET") + field(1, "GET") + field(2, "/"))),
        ("literal pseudo name", frame(HEADERS, END, 6, field(":method", "GET") + field(2, "/"))),
    ]
    for label, raw in bad:
        p.s.sendall(raw)
        _, status, _, _ = p.response()
        check(f"{label} -> 400", status == 400, f"got {status}")
    p.s.sendall(get(10, "/index.html"))
    check("next good request -> 200", p.response()[1] == 200)
    p.close()

    print("\n6. Method and path rules")
    p = Peer(port)
    p.s.sendall(get(1, "/index.html", method="POST"))
    check("POST -> 405", p.response()[1] == 405)
    p.s.sendall(get(2, "/index.html", method="HEAD"))
    _, status, fields, body = p.response()
    check("HEAD -> 200, no body, content-length set",
          status == 200 and body == b"" and fields.get(6) == str(len(index)))
    for rid, path in enumerate(["/../SPEC.md", "/%2e%2e/SPEC.md", "/docs/../../bserve"], start=3):
        p.s.sendall(get(rid, path))
        check(f"traversal {path} -> 404", p.response()[1] == 404)
    p.s.sendall(get(9, "/index.html?x=1"))
    check("query string ignored -> 200", p.response()[1] == 200)
    p.close()

    print("\n7. Request with a body: answered only after END_STREAM")
    p = Peer(port)
    p.s.sendall(get(1, "/style.css", end=False) + frame(DATA, 0, 1, b"abc"))
    p.s.settimeout(0.3)
    try:
        early = p.s.recv(1)
    except socket.timeout:
        early = b""
    p.s.settimeout(5)
    check("no response before END_STREAM", early == b"")
    p.s.sendall(frame(DATA, END, 1, b"def"))
    check("response after END_STREAM -> 200", p.response()[1] == 200)
    p.close()

    print("\n8. Pipelining and fragmentation")
    p = Peer(port)
    p.s.sendall(b"".join(get(rid, "/index.html") for rid in range(1, 6)))
    ids = [p.response()[0] for _ in range(5)]
    check("5 pipelined requests all answered", sorted(ids) == [1, 2, 3, 4, 5], str(ids))
    p.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for b in get(6, "/style.css"):
        p.s.sendall(bytes([b]))
        time.sleep(0.001)
    check("byte-at-a-time request -> 200", p.response()[1] == 200)
    p.close()

    print("\n9. Broken preface -> GOAWAY and close")
    p = Peer(port, preface=None)
    p.s.sendall(b"GET / HTTP/1.1\r\n\r\n")
    ftype, _, rid, payload = p.frame()
    check("GOAWAY on id 0", (ftype, rid) == (GOAWAY, 0))
    check("error code BAD_PREFACE (2)", struct.unpack_from(">IH", payload)[1] == 2)
    try:
        closed = p.s.recv(1) == b""
    except OSError:
        closed = True
    check("server closed", closed)
    p.close()

    print("\n10. bcurl")
    bcurl = os.path.join(HERE, "bcurl")
    base = f"localhost:{port}"
    r = subprocess.run([bcurl, f"{base}/index.html"], capture_output=True)
    check("fetches body to stdout, exit 0", r.returncode == 0 and r.stdout == index, f"exit {r.returncode}")
    r = subprocess.run([bcurl, f"{base}/missing"], capture_output=True)
    check("404 -> non-zero exit", r.returncode == 1, f"exit {r.returncode}")
    r = subprocess.run([bcurl, "-v", f"{base}/index.html", f"{base}/style.css"], capture_output=True)
    err = r.stderr.decode()
    check("-v hexdumps frames", "> HEADERS" in err and "< DATA" in err and "0000  " in err)
    check("two URLs, one connection", err.count("connected to") == 1 and r.returncode == 0)
    r = subprocess.run([bcurl, f"{base}/a", "localhost:1/b"], capture_output=True)
    check("refuses a second host:port", r.returncode == 2)
    r = subprocess.run([bcurl, "-I", f"{base}/index.html"], capture_output=True)
    check("-I prints headers", b"BHTP/1 200" in r.stdout and b"content-length:" in r.stdout)
    r = subprocess.run([bcurl, "-H", "naïve: x", f"{base}/index.html"], capture_output=True)
    check("non-ASCII header name -> exit 2, no traceback",
          r.returncode == 2 and b"Traceback" not in r.stderr, f"exit {r.returncode}")

    print("\n11. bcurl skips unknown frame types")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    fake = f"localhost:{srv.getsockname()[1]}"

    def fake_server():
        c, _ = srv.accept()
        c.settimeout(5)
        buf = b""
        while len(buf) < 8:
            buf += c.recv(65536)
        c.sendall(PREFACE)
        while len(buf) < 16:
            buf += c.recv(65536)
        length = struct.unpack(">H", buf[8:10])[0]
        while len(buf) < 16 + length:
            buf += c.recv(65536)
        c.sendall(frame(0x7F, 0xFF, 0, b"ignore me")
                  + frame(HEADERS, 0, 1, field(3, "200") + field(6, "3"))
                  + frame(0x42, 0, 1, b"\x00" * 300)
                  + frame(DATA, END, 1, b"hi\n"))
        time.sleep(0.5)
        c.close()

    t = threading.Thread(target=fake_server, daemon=True)
    t.start()
    r = subprocess.run([bcurl, f"{fake}/x"], capture_output=True, timeout=10)
    t.join(timeout=5)
    srv.close()
    check("unknown frames skipped, body intact, exit 0",
          r.returncode == 0 and r.stdout == b"hi\n",
          f"exit {r.returncode}, stdout {r.stdout!r}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    root = os.path.join(HERE, "www")
    big = os.path.join(root, "big.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(200_000))
    port = free_port()
    log = tempfile.TemporaryFile()
    server = subprocess.Popen([os.path.join(HERE, "bserve"), root, str(port), "127.0.0.1"],
                              stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        run_tests(port, root)
    finally:
        server.terminate()
        server.wait()
        os.remove(big)

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
