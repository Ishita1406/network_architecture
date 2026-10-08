# BHTP/1: HTTP in Binary Frames

BHTP/1 carries HTTP-style requests and responses over one long-lived TCP connection as binary frames. Every frame states its own length, so a receiver never has to scan for delimiters to find where one message ends and the next begins. **MUST**, **SHOULD** and **MAY** are used as in RFC 2119. All integers are unsigned and big-endian.

## 1. Overview

```
client                                          server
  |-- preface  42 48 54 50 01 00 00 00 ---------->|
  |<--------------------------- preface (echo) ---|
  |-- HEADERS id=1 END_STREAM (:method, :path) -->|
  |<------------------ HEADERS id=1 (:status) ----|
  |<------------------ DATA    id=1 ... ----------|
  |<------------------ DATA    id=1 END_STREAM ---|
  |-- HEADERS id=2 ... (same connection) -------->|
```

After the preface, both directions carry only frames. A **message** (request or response) is one HEADERS frame followed by zero or more DATA frames, and its last frame has END_STREAM set. Every frame carries the **request ID** of the request it belongs to. The connection stays open between requests.

## 2. Preface

The client MUST open with these 8 bytes: `42 48 54 50 01 00 00 00`. That is the ASCII magic `BHTP`, version `0x01`, and three reserved zero bytes.

- A server that supports the version MUST echo the same 8 bytes before sending any frame.
- Otherwise it MUST send GOAWAY `BAD_PREFACE` and close.
- A client MUST check the echo before reading frames.

The magic makes a wrong-protocol peer fail at once (an HTTP/1.1 `GET ` is not `BHTP`). The version byte leaves room for version 2. The padding keeps every frame at an 8-byte-aligned offset.

## 3. Frames

### 3.1 Layout

```
 0               1               2               3
+-------------------------------+---------------+---------------+
|          Length (16)          |   Type (8)    |   Flags (8)   |
+-------------------------------+---------------+---------------+
|                        Request ID (32)                        |
+---------------------------------------------------------------+
|                   Payload (Length bytes) ...                  |
```

`Length` counts payload bytes only (0 to 65,535). To read a frame, read exactly 8 bytes, then exactly `Length` bytes. EOF before the first header byte is a clean close; EOF anywhere else is a truncated frame. Frames may be split across TCP segments, or share one, so receivers MUST NOT rely on `recv()` boundaries.

### 3.2 Why these widths

- **Length 16.** A frame is a unit of buffering, not a message; large bodies are sent as many DATA frames. Sixteen bits cap per-frame memory at 64 KiB with no negotiation, at 0.01% overhead.
- **Type 8, Flags 8.** Version 1 uses 3 types and 1 flag, which leaves plenty of room to extend.
- **Request ID 32.** It never wraps in practice. Responses are matched by ID rather than by order, so a later version can multiplex without changing the header. ID 0 means the connection itself.
- **8 bytes total**, with every field naturally aligned.

**Comparison with HTTP/2 (24/8/8/31).** 24 bits of length allow frames of up to 16 MiB, but the default maximum is 16 KiB and a larger one must be negotiated through SETTINGS. Type and flags are 8 bits each for the same reasons as here. Stream IDs are 31 bits because they only increase and are split odd (client) and even (server), and the space must never run out on one connection. The top bit is reserved (a leftover of SPDY's control bit), and 31 bits also suits languages without unsigned 32-bit integers. BHTP/1 has no SETTINGS frame, so it fixes the frame-size cap in the header width itself and stays aligned.

### 3.3 Frame types

| Type | Name | ID | Payload |
|---|---|---|---|
| `0x01` | HEADERS | ≠ 0 | header block (section 4) |
| `0x02` | DATA | ≠ 0 | body bytes |
| `0x03` | GOAWAY | 0 | `last-id u32`, `error u16`, optional UTF-8 reason (≤ 1024 B) |

The only flag is **END_STREAM `0x01`**, on HEADERS or DATA: this is the last frame of the message.

The GOAWAY error codes are `0` NO_ERROR, `1` PROTOCOL_ERROR, `2` BAD_PREFACE and `3` IDLE_TIMEOUT. Unknown codes are treated as PROTOCOL_ERROR. `last-id` is the highest request ID the sender processed; requests above it were not processed and MAY be retried. The sender closes after GOAWAY.

### 3.4 Unknown frame types and flags

**A receiver that meets a frame type it does not know MUST skip it cleanly: read and discard exactly `Length` bytes, and carry on as if the frame were absent.** It MUST NOT error or close. Unknown flag bits MUST be ignored. A later version can therefore add frame types that version-1 peers pass over. A change that old peers cannot safely ignore needs a new preface version. The 8-byte header layout itself never changes.

## 4. Header blocks

A HEADERS payload is a sequence of fields that fills the frame exactly:

```
field = name value
name  = index (1 byte, 0x01-0x0A)            ; static table entry
      | 0x00  len(u8, 1-255)  ASCII bytes    ; literal name
value = len(u16)  bytes                      ; 0-65,535 bytes
```

The static table holds the ten most common names:

| # | Name | # | Name |
|---|---|---|---|
| 1 | `:method` | 6 | `content-length` |
| 2 | `:path` | 7 | `user-agent` |
| 3 | `:status` | 8 | `server` |
| 4 | `:authority` | 9 | `date` |
| 5 | `content-type` | 10 | `last-modified` |

For example, `:method: GET` is `01 00 03 47 45 54`, and `x-id: 7` is `00 04 78 2d 69 64 00 01 37`.

Rules:

- A sender MUST use the index for a name that is in the table.
- Literal names are 1–255 bytes of lowercase ASCII and MUST NOT start with `:`. Pseudo-fields exist only as indexes.
- A block is **malformed** if any length runs past the end of the frame, a name byte is `0x0B`–`0xFF`, or a literal name is empty, non-ASCII, uppercase, or starts with `:`.
- Receivers ignore literal names they do not know.
- A block MUST fit in one frame; there is no continuation.

This takes HPACK's static table and length-prefixed literals, and leaves out its dynamic table and Huffman coding. Fixed `u8`/`u16` lengths keep every length readable in a hexdump.

## 5. Requests and responses

- **Request:** exactly one `:method` and one `:path` (starting with `/`), at most one `:authority`, and no `:status`.
- **Response:** exactly one `:status`, written as three ASCII digits (for example `"200"`), and no request pseudo-fields. It SHOULD include `content-length`.
- **Request IDs** are chosen by the client. They are non-zero and strictly increasing on a connection. Every frame of a response carries its request's ID.
- **END_STREAM ends a message.** `content-length` is informational only. A message with no body is a single HEADERS frame with END_STREAM. HEAD responses carry the GET headers and no DATA.
- The server MUST NOT answer before the request's END_STREAM, except to send a 400 for a malformed HEADERS frame. DATA frames on an ID with no open request are ignored.
- A client MAY pipeline (send several requests before any response arrives). A version-1 server sends each response whole, in the order requests complete. **Clients MUST still match responses by ID, not by order.**

## 6. Errors

| Problem | Scope | Action |
|---|---|---|
| Malformed header block, or pseudo-fields missing, duplicated or wrong | request | `400` on that ID; the connection stays open |
| Method not supported | request | `405` |
| Path not found, or outside the root | request | `404` |
| Bad preface | connection | GOAWAY `BAD_PREFACE`, close |
| HEADERS on ID 0, or a second HEADERS on an open ID | connection | GOAWAY `PROTOCOL_ERROR`, close |
| No bytes for the idle timeout (30 s in `bserve`) | connection | GOAWAY `IDLE_TIMEOUT`, close |
| EOF inside a frame | connection | close |

Every frame states its length, so a bad header block never throws the stream out of sync. It costs one 400, not the connection. Only damage to the framing itself ends the connection.

## 7. Server behaviour

A server keeps the connection open after every response, including error responses. For `GET` and `HEAD` it maps `:path` to a file:

1. Strip any `?query` or `#fragment` and percent-decode the rest.
2. Resolve the result under the document root, following `..` and symlinks.
3. If the resolved path lies outside the root, answer **404**. A directory is served as its `index.html`.

A success is HEADERS (`:status`, `content-type`, `content-length`, `server`, `date`, `last-modified`) followed by DATA frames. Senders SHOULD keep DATA frames to 16 KiB or less.

## 8. Client behaviour

A client sends all its requests to a server over a single connection. It writes response bodies unchanged, and matches responses to requests by ID. It MUST skip unknown frame types, and MUST treat a response without a valid `:status` as a protocol error.

## 9. Extending

| Room left | Reserved for |
|---|---|
| Frame types `0x04`–`0xFF` | new frames, safe because of the skip rule |
| Flag bits `0x02`–`0x80` | hints that do not change payload layout |
| Name bytes `0x0B`–`0xFF` | a larger static table or indexed values, only after the preface negotiates a new version |
| Request IDs | multiplexed responses |
| Preface version byte | any incompatible change |
