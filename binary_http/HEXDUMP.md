# Annotated exchange: `bcurl -v localhost:9000/docs/hello.txt`

These are the real bytes from one run against `bserve ./www 9000`, where `www/docs/hello.txt` contains `hello\n`. `>` marks bytes from client to server and `<` marks bytes from server to client. Totals: client 69 bytes (8 + 61), server 145 bytes (8 + 123 + 14).

## 1. Preface, both directions (section 2)

```
> 42 48 54 50  01  00 00 00        "BHTP", version 1, 3 reserved zero bytes
< 42 48 54 50  01  00 00 00        server echoes it back: version 1 accepted
```

## 2. Request: one HEADERS frame (61 bytes)

```
> 00 35 01 01 00 00 00 01 01 00 03 47 45 54 02 00  .5.........GET..
> 0f 2f 64 6f 63 73 2f 68 65 6c 6c 6f 2e 74 78 74  ./docs/hello.txt
> 04 00 0e 6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30  ...localhost:900
> 30 07 00 09 62 63 75 72 6c 2f 31 2e 30           0...bcurl/1.0
```

| Bytes | Meaning |
|---|---|
| `00 35` | Length = 0x35 = **53** payload bytes |
| `01` | Type = HEADERS |
| `01` | Flags = END_STREAM (no DATA follows, the request is complete) |
| `00 00 00 01` | Request ID = **1** |
| `01` | field name: static index 1 = `:method` |
| `00 03` `47 45 54` | value: length 3, `GET` |
| `02` | static index 2 = `:path` |
| `00 0f` `2f 64 … 74` | value: length 15, `/docs/hello.txt` |
| `04` | static index 4 = `:authority` |
| `00 0e` `6c 6f … 30` | value: length 14, `localhost:9000` |
| `07` | static index 7 = `user-agent` |
| `00 09` `62 63 … 30` | value: length 9, `bcurl/1.0` |

Check: 6 + 18 + 17 + 12 = 53 payload bytes, and the frame has no spare bytes. A header name costs 1 byte here instead of the 8 to 12 bytes of text HTTP/1.1 spends on it (`User-Agent: `).

## 3. Response: HEADERS frame (123 bytes)

```
< 00 73 01 00 00 00 00 01 03 00 03 32 30 30 05 00  .s.........200..
< 19 74 65 78 74 2f 70 6c 61 69 6e 3b 20 63 68 61  .text/plain; cha
< 72 73 65 74 3d 75 74 66 2d 38 06 00 01 36 08 00  rset=utf-8...6..
< 0a 62 73 65 72 76 65 2f 31 2e 30 09 00 1d 46 72  .bserve/1.0...Fr
< 69 2c 20 32 35 20 53 65 70 20 32 30 32 36 20 31  i, 25 Sep 2026 1
< 30 3a 31 35 3a 31 33 20 47 4d 54 0a 00 1d 46 72  0:15:13 GMT...Fr
< 69 2c 20 32 35 20 53 65 70 20 32 30 32 36 20 31  i, 25 Sep 2026 1
< 30 3a 31 34 3a 30 34 20 47 4d 54                 0:14:04 GMT
```

| Bytes | Meaning |
|---|---|
| `00 73` | Length = 0x73 = **115** |
| `01` | Type = HEADERS |
| `00` | Flags = none, so a body follows in DATA frames |
| `00 00 00 01` | Request ID = **1**, the answer to request 1 |
| `03` `00 03` `32 30 30` | `:status` = `200` |
| `05` `00 19` `74 … 38` | `content-type`, 25 bytes = `text/plain; charset=utf-8` |
| `06` `00 01` `36` | `content-length` = `6` |
| `08` `00 0a` `62 … 30` | `server`, 10 bytes = `bserve/1.0` |
| `09` `00 1d` `46 … 54` | `date`, 29 bytes = `Fri, 25 Sep 2026 10:15:13 GMT` |
| `0a` `00 1d` `46 … 54` | `last-modified`, 29 bytes = `Fri, 25 Sep 2026 10:14:04 GMT` |

Check: 6 + 28 + 4 + 13 + 32 + 32 = 115.

## 4. Response: DATA frame (14 bytes)

```
< 00 06 02 01 00 00 00 01 68 65 6c 6c 6f 0a        ........hello.
```

| Bytes | Meaning |
|---|---|
| `00 06` | Length = **6** |
| `02` | Type = DATA |
| `01` | Flags = END_STREAM, the last frame of response 1 |
| `00 00 00 01` | Request ID = **1** |
| `68 65 6c 6c 6f 0a` | body: `hello\n` |

The client knows the response is complete because of END_STREAM, not because the connection closed. The connection stays open for request 2. When bcurl has nothing more to fetch, it closes the connection at a frame boundary, and bserve logs `closed after 1 frame(s)`.
