# Network Architecture

Two small projects about **framing**: how a receiver reading one long-lived TCP connection knows where one message ends and the next begins.

Both are plain Python on raw sockets, with no frameworks and no third-party packages. Each folder has its own README with full documentation; this page is the overview.

| Project | What it is | How messages are framed |
|---|---|---|
| [calculator/](calculator/) | A calculator served over persistent HTTP/1.1 connections | Text: `CRLF CRLF` ends the headers, then `Content-Length` or chunked encoding ends the body |
| [binary_http/](binary_http/) | BHTP/1, a binary framing of HTTP, with a file server and a client | Binary: every frame starts with an 8-byte header that states its own length |

The calculator works within the limits of text HTTP/1.1. BHTP/1 then designs a protocol where framing is part of every frame.

## Layout

```
.
├── calculator/
│   ├── README.md            usage, API, error handling
│   ├── server.py            HTTP/1.1 calculator server (port 8080)
│   └── test_client.py       tests over raw sockets
└── binary_http/
    ├── README.md            usage of bserve and bcurl
    ├── SPEC.md              the BHTP/1 protocol specification
    ├── HEXDUMP.md           one request and response, annotated byte by byte
    ├── bserve               BHTP/1 server (port 9000)
    ├── bcurl                BHTP/1 client
    ├── test_bhttp.py        conformance tests
    └── www/                 sample document root
```

## Requirements

Python 3.8 or newer on Linux or macOS. Only the standard library is used.

## Quick start

**Calculator**

```sh
python3 calculator/server.py                
curl 'http://localhost:8080/add?a=2&b=3' 
```

**BHTP/1**

```sh
cd binary_http
./bserve ./www 9000 
./bcurl -v localhost:9000/index.html
```

## Running the tests

```sh
# calculator: needs its server running
python3 calculator/server.py & SERVER=$!
python3 calculator/test_client.py
kill $SERVER

# binary_http: starts its own server on a free port
python3 binary_http/test_bhttp.py
```

Each suite prints `ALL PASSED` and exits 0 on success, or exits 1 with the failing checks marked `[FAIL]`.

## The two approaches side by side

| | calculator (HTTP/1.1) | binary_http (BHTP/1) |
|---|---|---|
| End of headers | scan for `CRLF CRLF` | the header block's length is in the frame header |
| End of body | `Content-Length` or the zero-size chunk | the END_STREAM flag |
| After a malformed request | `400`, then close, because the stream can't be trusted | `400`, and the connection stays open |
| Several requests in flight | pipelined, answered strictly in order | pipelined, matched by request ID |
| Header names on the wire | full text every time | one byte for the ten common names |
| Unknown message types | not applicable | skipped by length |
| Idle timeout | 15 s | 30 s, announced with GOAWAY |

## Where to read next

- [calculator/README.md](calculator/README.md): the calculator API, status codes, keep-alive and pipelining.
- [binary_http/SPEC.md](binary_http/SPEC.md): the BHTP/1 wire format and why each field is the width it is.
- [binary_http/HEXDUMP.md](binary_http/HEXDUMP.md): a full exchange on the wire, byte by byte.
- [binary_http/README.md](binary_http/README.md): `bserve` and `bcurl` options, tests and limitations.
