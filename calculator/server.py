#!/usr/bin/env python3
"""
A keep-alive calculator.

HTTP/1.1 over a raw socket -- no framework, no http.server.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3
    GET /div?a=1&b=0   -> 400
    GET /add?a=x&b=3   -> 400
    GET /pow?a=2&b=8   -> 404
    POST /add          -> 405
    GET /add (no Host) -> 400

The hard part is framing. With a persistent connection, EOF no longer marks
the end of a request, so each request is delimited explicitly:

    head  = everything up to and including the first CRLFCRLF
    body  = exactly Content-Length bytes, or a chunked body up to its
            terminating zero-size chunk; with neither, the body is empty.

Anything after that belongs to the *next* request and stays in the buffer.

Connection features:
  * Connection: close is honoured (and HTTP/1.0 defaults to close).
  * Idle timeout (IDLE_TIMEOUT seconds with no bytes from the client).
  * Chunked request bodies (Transfer-Encoding: chunked).
  * Pipelining: several requests in one segment are answered in order.

Usage:  python3 server.py [port] [host]      (default 8080, 0.0.0.0)
"""

import socket
import sys
import threading
from urllib.parse import parse_qsl, urlsplit

IDLE_TIMEOUT = 15.0
MAX_HEAD = 8 * 1024
MAX_BODY = 1024 * 1024
RECV_SIZE = 4096

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}


class ProtocolError(Exception):
    """The byte stream is broken. We answer once and then hang up, because we
    can no longer tell where the next request starts."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class ConnectionClosed(Exception):
    """The peer closed or went idle while we still needed more bytes."""


class Reader:
    """A buffered view over a socket.

    Every read takes bytes from the front of `buf`. Bytes left behind in `buf`
    belong to the next request, which is how pipelining works."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        try:
            data = self.sock.recv(RECV_SIZE)
        except socket.timeout:
            raise ConnectionClosed("idle timeout")
        except OSError:
            raise ConnectionClosed("reset")
        if not data:
            raise ConnectionClosed("eof")
        self.buf += data

    def has_pending(self):
        return len(self.buf) > 0

    def read_until(self, delim, limit, too_big_status):
        """Return bytes up to and including `delim`, reading no more than `limit`."""
        start = 0
        while True:
            idx = self.buf.find(delim, start)
            if idx != -1:
                end = idx + len(delim)
                if end > limit:
                    raise ProtocolError(too_big_status, "line too long")
                out = bytes(self.buf[:end])
                del self.buf[:end]
                return out
            if len(self.buf) > limit:
                raise ProtocolError(too_big_status, "line too long")
            start = max(0, len(self.buf) - len(delim) + 1)
            self._fill()

    def read_exact(self, n):
        """Consume exactly n bytes, and not one more."""
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def read_request(reader):
    """Read one complete request from the stream.

    Returns (method, target, version, headers, body). `headers` maps
    lower-cased names to lists of values."""
    while reader.buf[:2] == b"\r\n":
        del reader.buf[:2]

    head = reader.read_until(b"\r\n\r\n", MAX_HEAD, 431)
    lines = head[:-4].split(b"\r\n")

    try:
        request_line = lines[0].decode("ascii")
    except UnicodeDecodeError:
        raise ProtocolError(400, "request line is not ASCII")
    parts = request_line.split(" ")
    if len(parts) != 3 or not all(parts):
        raise ProtocolError(400, "malformed request line")
    method, target, version = parts
    if not version.startswith("HTTP/") or version not in ("HTTP/1.0", "HTTP/1.1"):
        if version.startswith("HTTP/"):
            raise ProtocolError(505, "unsupported HTTP version")
        raise ProtocolError(400, "malformed HTTP version")
    if not method.isalpha() or not method.isupper():
        raise ProtocolError(400, "malformed method")

    headers = {}
    for raw in lines[1:]:
        if raw[:1] in (b" ", b"\t"):
            raise ProtocolError(400, "obsolete line folding")
        name, sep, value = raw.partition(b":")
        if not sep or not name or name != name.strip():
            raise ProtocolError(400, "malformed header line")
        try:
            key = name.decode("ascii").lower()
            val = value.strip(b" \t").decode("latin-1")
        except UnicodeDecodeError:
            raise ProtocolError(400, "malformed header name")
        headers.setdefault(key, []).append(val)

    body = read_body(reader, headers)
    return method, target, version, headers, body


def read_body(reader, headers):
    """Work out where this message's body ends and consume exactly that much."""
    te = headers.get("transfer-encoding")
    cl = headers.get("content-length")

    if te is not None:
        if cl is not None:
            raise ProtocolError(400, "both Transfer-Encoding and Content-Length")
        codings = [c.strip().lower() for v in te for c in v.split(",") if c.strip()]
        if codings != ["chunked"]:
            raise ProtocolError(501, "unsupported Transfer-Encoding")
        return read_chunked(reader)

    if cl is not None:
        values = {v.strip() for line in cl for v in line.split(",")}
        if len(values) != 1:
            raise ProtocolError(400, "conflicting Content-Length")
        (value,) = values
        if not value.isdigit():
            raise ProtocolError(400, "invalid Content-Length")
        length = int(value)
        if length > MAX_BODY:
            raise ProtocolError(413, "body too large")
        return reader.read_exact(length)

    return b""


def read_chunked(reader):
    body = bytearray()
    while True:
        line = reader.read_until(b"\r\n", 1024, 400)
        size_str = line[:-2].split(b";", 1)[0].strip()
        try:
            size = int(size_str, 16)
        except ValueError:
            raise ProtocolError(400, "invalid chunk size")
        if size_str.startswith((b"-", b"+", b"0x", b"0X")) or size < 0:
            raise ProtocolError(400, "invalid chunk size")
        if size == 0:
            break
        if len(body) + size > MAX_BODY:
            raise ProtocolError(413, "body too large")
        body += reader.read_exact(size)
        if reader.read_exact(2) != b"\r\n":
            raise ProtocolError(400, "chunk not terminated by CRLF")
    total = 0
    while True:
        line = reader.read_until(b"\r\n", MAX_HEAD, 431)
        total += len(line)
        if total > MAX_HEAD:
            raise ProtocolError(431, "trailers too large")
        if line == b"\r\n":
            break
    return bytes(body)


OPS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": None,
}


def parse_number(s):
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def format_number(x):
    if isinstance(x, float):
        if x.is_integer() and abs(x) < 1e16:
            return str(int(x))
        return repr(x)
    return str(x)


def calculate(path, query):
    """Return (status, body_text)."""
    try:
        params = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
    except ValueError:
        return 400, "malformed query string"
    seen = {}
    for k, v in params:
        if k in seen:
            return 400, f"duplicate parameter '{k}'"
        seen[k] = v
    if "a" not in seen or "b" not in seen:
        return 400, "need both a and b"
    a, b = parse_number(seen["a"]), parse_number(seen["b"])
    if a is None or b is None:
        return 400, "a and b must be numbers"

    if path == "/div":
        if b == 0:
            return 400, "division by zero"
        if isinstance(a, int) and isinstance(b, int) and a % b == 0:
            result = a // b
        else:
            result = a / b
    else:
        result = OPS[path](a, b)

    if isinstance(result, float) and result in (float("inf"), float("-inf")):
        return 400, "result out of range"
    return 200, format_number(result)


def handle(method, target, version, headers):
    """Map a parsed request to (status, body, extra_headers)."""
    if version == "HTTP/1.1":
        hosts = headers.get("host", [])
        if len(hosts) != 1:
            return 400, "missing or duplicate Host header", {}

    if not target.startswith("/"):
        return 400, "only origin-form request targets are supported", {}
    parts = urlsplit(target)
    path, query = parts.path, parts.query

    if path not in OPS:
        return 404, f"no such operation: {path}", {}
    if method not in ("GET", "HEAD"):
        return 405, f"method {method} not allowed", {"Allow": "GET, HEAD"}
    status, text = calculate(path, query)
    return status, text, {}


def build_response(status, body_text, keep_alive, head_only=False, extra=None):
    body = body_text.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: " + ("keep-alive" if keep_alive else "close"),
    ]
    if keep_alive:
        lines.append(f"Keep-Alive: timeout={int(IDLE_TIMEOUT)}")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    return head if head_only else head + body


def wants_keep_alive(version, headers):
    tokens = {t.strip().lower()
              for v in headers.get("connection", [])
              for t in v.split(",")}
    if "close" in tokens:
        return False
    if version == "HTTP/1.0":
        return "keep-alive" in tokens
    return True


def log(peer, msg):
    print(f"[{peer[0]}:{peer[1]}] {msg}", flush=True)


def serve_connection(sock, peer):
    sock.settimeout(IDLE_TIMEOUT)
    reader = Reader(sock)
    served = 0
    try:
        while True:
            out = bytearray()
            keep_alive = True
            while keep_alive:
                try:
                    method, target, version, headers, _body = read_request(reader)
                except ProtocolError as e:
                    out += build_response(e.status, e.message, keep_alive=False)
                    log(peer, f"{e.status} (protocol error: {e.message}) -> closing")
                    keep_alive = False
                    break

                keep_alive = wants_keep_alive(version, headers)
                status, text, extra = handle(method, target, version, headers)
                out += build_response(status, text, keep_alive,
                                      head_only=(method == "HEAD"), extra=extra)
                served += 1
                log(peer, f"{method} {target} {version} -> {status} {text!r}"
                          + ("" if keep_alive else "  [close]"))

                if not reader.has_pending():
                    break

            sock.sendall(out)
            if not keep_alive:
                break
    except ConnectionClosed as e:
        if str(e) == "idle timeout" and reader.has_pending():
            try:
                sock.sendall(build_response(408, "request timeout", keep_alive=False))
            except OSError:
                pass
        log(peer, f"connection ended ({e}) after {served} response(s)")
    except OSError as e:
        log(peer, f"socket error: {e}")
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    print(f"calculator listening on {host}:{port} (idle timeout {IDLE_TIMEOUT:g}s)", flush=True)

    try:
        while True:
            conn, peer = srv.accept()
            log(peer, "connected")
            threading.Thread(target=serve_connection, args=(conn, peer), daemon=True).start()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
