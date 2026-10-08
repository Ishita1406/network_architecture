# BHTP/1: HTTP in binary

A binary framing of HTTP, with a server and a client.

| File | What it is |
|---|---|
| [`SPEC.md`](SPEC.md) | The protocol specification |
| [`HEXDUMP.md`](HEXDUMP.md) | One request and response, annotated byte by byte |
| [`bserve`](bserve) | Server: serves a directory, keeps connections open |
| [`bcurl`](bcurl) | Client: fetches URLs over one connection, `-v` hexdumps every frame |
| [`test_bhttp.py`](test_bhttp.py) | Conformance tests |
| [`www/`](www/) | Sample document root |

`bserve` and `bcurl` share no code. Each is written from `SPEC.md` alone.

Requirements: Python 3.8+, standard library only, Linux or macOS.

## Quick start

```sh
./bserve ./www 9000                              

./bcurl localhost:9000/index.html                
./bcurl -v localhost:9000/docs/hello.txt         
./bcurl -I localhost:9000/style.css              
./bcurl localhost:9000/ localhost:9000/style.css 

python3 test_bhttp.py                           
```

## The protocol in brief

- **Preface:** the client sends `42 48 54 50 01 00 00 00` (`BHTP`, version 1) and the server echoes it.
- **Frames:** an 8-byte header, `Length u16 | Type u8 | Flags u8 | Request-ID u32`, then `Length` bytes of payload. The types are HEADERS `0x01`, DATA `0x02` and GOAWAY `0x03`. Any other type is skipped. Flag `0x01` (END_STREAM) ends a message.
- **Headers:** ten common names are one byte each (`0x01`–`0x0A`). Any other name is `0x00`, a `u8` length, then the name. Every value is a `u16` length, then the bytes.
- **Errors:** a malformed request gets a `400`, and the connection stays open. Only broken framing ends the connection, with a GOAWAY.

See [SPEC.md](SPEC.md) for the full rules.

## bserve

```
./bserve ROOT PORT [HOST]      # HOST defaults to 0.0.0.0
```

- Serves `GET` and `HEAD`. Other methods get `405`.
- Maps `:path` under `ROOT`. Missing files, and paths that resolve outside `ROOT`, get `404`. A directory is served as its `index.html`.
- Sends bodies in 16 KiB DATA frames.
- Uses one thread per connection. A connection closes when the client closes it, or after 30 s idle (with GOAWAY `IDLE_TIMEOUT`).
- Logs one line per request to stdout, including skipped unknown frames.

## bcurl

```
./bcurl [-v] [-I] [-X METHOD] [-H 'Name: value']... URL [URL...]
```

| Option | Meaning |
|---|---|
| `-v` | Hexdump every frame sent and received to stderr, with decoded headers |
| `-I` | Send `HEAD` and print the status and headers |
| `-X METHOD` | Use another method |
| `-H 'Name: value'` | Add a request header (ASCII name, repeatable) |
| `URL` | `[bhttp://]host[:port][/path]`. The port defaults to 9000. |

- All URLs must share one `host:port`, and bcurl uses a single connection for them.
- Requests are pipelined, and responses are matched by request ID.
- Bodies go to stdout unchanged, in URL order. Diagnostics go to stderr.
- Unknown frame types are skipped.

**Exit status:** `0` means every response was below 400. `1` means at least one was 4xx or 5xx. `2` means a usage, connection or protocol error.

## Tests

```sh
python3 test_bhttp.py
```

The script starts `bserve` on a free port. It checks the server with a raw-socket peer that builds frames by hand and shares no code with either program, and it runs `bcurl` as a subprocess. It covers:

- many requests on one connection
- large bodies split across DATA frames
- unknown frame types skipped (by the server and by `bcurl`)
- malformed headers answered with `400` without closing the connection
- `405`, `HEAD`, and path traversal
- requests with bodies
- pipelining and byte-at-a-time delivery
- a bad preface
- `bcurl`'s output and exit codes

It prints `ALL PASSED` and exits 0, or exits 1 with the failures marked `[FAIL]`.

## Limitations

- Tested only against itself and the hand-built test peer.
- `bcurl` buffers each body in memory before writing it out.
- `bserve` discards request bodies, and does not cap how many requests a client can leave open.
- IPv4 only, no TLS.
