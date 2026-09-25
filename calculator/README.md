# Keep-Alive Calculator Server

A simple calculator server built using **Python sockets and HTTP/1.1**.

It supports basic arithmetic operations and keeps the TCP connection open so that multiple requests can be sent over the **same connection**.

## Features

* HTTP/1.1 persistent connections (Keep-Alive)
* `GET` and `HEAD` requests
* Addition, subtraction, multiplication and division
* Multiple requests on the same TCP connection
* HTTP request pipelining
* `Content-Length` based request/response handling
* Chunked request bodies
* `Connection: close` support
* 15-second idle timeout
* Proper HTTP error responses
* No external libraries or frameworks

## Requirements

* Python 3.8+
* No external packages required

## Running the Server

Start the server on the default port:

```bash
python3 server.py
```

The default port is **8080**.

You can also specify a different port:

```bash
python3 server.py 9090
```

Or specify both port and interface:

```bash
python3 server.py 8080 127.0.0.1
```

## Testing

Run the test client:

```bash
python3 test_client.py
```

For another host or port:

```bash
python3 test_client.py 127.0.0.1 9090
```

If everything passes, the output ends with:

```text
ALL PASSED
```

## API

The calculator uses the following format:

```text
GET /<operation>?a=<number>&b=<number>
```

Supported operations:

| Request         | Result |
| --------------- | -----: |
| `/add?a=2&b=3`  |    `5` |
| `/sub?a=10&b=4` |    `6` |
| `/mul?a=6&b=7`  |   `42` |
| `/div?a=7&b=2`  |  `3.5` |

Example:

```bash
curl 'http://localhost:8080/add?a=2&b=3'
```

Response:

```text
5
```

## Error Handling

The server returns appropriate HTTP status codes for invalid requests.

Examples:

| Situation                | Status |
| ------------------------ | -----: |
| Invalid numbers          |    400 |
| Division by zero         |    400 |
| Missing parameters       |    400 |
| Unknown operation        |    404 |
| Unsupported method       |    405 |
| Unsupported HTTP version |    505 |
| Request too large        |    413 |

Errors such as invalid numbers or division by zero **do not close the connection**.

If the HTTP request itself is malformed and the server can no longer safely determine where the next request starts, the connection is closed.

## Keep-Alive

The main feature of this project is that multiple requests can use the **same TCP connection**.

For example:

```text
Request 1 → /add?a=2&b=3
Response 1 → 5

Request 2 → /sub?a=10&b=4
Response 2 → 6

Request 3 → /mul?a=6&b=7
Response 3 → 42
```

The connection remains open instead of being closed after every request.

The server uses `Content-Length` to determine where each response ends.

## Pipelining

The client can send multiple requests without waiting for each response.

Example:

```text
GET /add?a=2&b=3
GET /sub?a=10&b=4
GET /mul?a=6&b=7
```

The server processes them in order and sends the responses in the same order.

## Request Framing

Since the connection stays open, the server must know exactly where one request ends and the next begins.

The server handles this using:

* `\r\n\r\n` to identify the end of HTTP headers
* `Content-Length` to determine body size
* Chunked encoding when `Transfer-Encoding: chunked` is used

Extra bytes received after one request are kept in a buffer and used for the next request.

## Project Structure

```text
.
├── server.py
├── test_client.py
└── README.md
```

### `server.py`

Contains the HTTP server, request parsing, connection handling and calculator logic.

### `test_client.py`

Tests the server using raw TCP sockets, including:

* Multiple requests on one connection
* Pipelining
* Invalid requests
* Fragmented requests
* Chunked requests
* Connection closing
* Keep-alive behaviour

## Design

The server uses:

* Python `socket` for TCP communication
* A buffered reader for handling incoming data
* One thread per client connection
* HTTP/1.1 Keep-Alive
* Exact `Content-Length` for message framing

No web framework or `http.server` is used.
