#!/usr/bin/env python3
"""Query the coordinator's browser_round RPC directly over WebSocket, bypassing the REST
API/DynamoDB entirely - unlike ./get-execution-status (GET /executions/{id}), this reflects
the coordinator's own live state, including the ProgramFinished terminal round that nothing
currently writes back to DynamoDB (see app.py's _add_orchestration_lambdas comment: nothing
tracks a terminal SUCCEEDED/FAILED-after-admission status once RUNNING).

No external dependencies - implements just enough of RFC 6455 by hand (one text frame out,
one text frame in) rather than requiring the `websockets` package.

Requires the coordinator to be running code with browser_round's authentication requirement
removed - pre-fix coordinators reject this with a JSON-RPC "missing field `request`" error,
since they still expect the full signed request wrapper every other browser_* RPC uses.

Usage: ./get-execution-status-coord.py <execution_id>
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys

STACK = "StoffelEc2UserDeploymentPersistentStack"
COORD_BROWSER_PORT = 31416  # Fixed across this deployment - see deploy's COORD_BROWSER_PORT.
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def get_stack_output(key: str) -> str:
    result = subprocess.run(
        [
            "aws", "cloudformation", "describe-stacks",
            "--stack-name", STACK,
            "--query", f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
            "--output", "text",
        ],
        capture_output=True, text=True, check=True,
    )
    value = result.stdout.strip()
    if not value or value == "None":
        raise RuntimeError(f"could not resolve {STACK}'s {key} output - has it been deployed?")
    return value


def hex_to_execution_id_bytes(execution_id_hex: str) -> list[int]:
    if len(execution_id_hex) != 64 or not all(c in "0123456789abcdefABCDEF" for c in execution_id_hex):
        raise ValueError("execution id must be 64 hex characters")
    return list(bytes.fromhex(execution_id_hex))


def ws_handshake(sock: ssl.SSLSocket, host: str, port: int) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode())

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed during WebSocket handshake")
        response += chunk
    header_text = response.split(b"\r\n\r\n", 1)[0].decode(errors="replace")

    lines = header_text.split("\r\n")
    status_line = lines[0]
    if " 101 " not in status_line:
        raise ConnectionError(f"handshake rejected: {status_line}")

    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    expected_accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()
    actual_accept = headers.get("sec-websocket-accept")
    if actual_accept != expected_accept:
        raise ConnectionError(
            f"handshake accepted but Sec-WebSocket-Accept did not match "
            f"(expected {expected_accept!r}, got {actual_accept!r})"
        )


def send_text_frame(sock: ssl.SSLSocket, payload: bytes) -> None:
    # Client-to-server frames must be masked per RFC 6455 section 5.1.
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytearray([0x81])  # FIN=1, opcode=1 (text)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    sock.sendall(bytes(header) + masked)


def recv_text_frame(sock: ssl.SSLSocket, timeout_s: float = 15.0) -> str:
    sock.settimeout(timeout_s)

    def recv_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed while reading a frame")
            buf += chunk
        return buf

    first_two = recv_exact(2)
    opcode = first_two[0] & 0x0F
    masked = bool(first_two[1] & 0x80)
    length = first_two[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(8))[0]
    mask = recv_exact(4) if masked else None
    payload = recv_exact(length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        raise ConnectionError(f"server sent a close frame: {payload!r}")
    return payload.decode()


def query_round(host: str, port: int, execution_id_hex: str) -> dict:
    raw_sock = socket.create_connection((host, port), timeout=15)
    context = ssl.create_default_context()
    # The coordinator's browser-facing listener serves a self-signed cert for its raw
    # public IP (no real CA issues certs for bare IPs) - this is an operator diagnostic
    # script against infrastructure you already control, so skip verification rather than
    # trying to pin/import that cert here.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    sock = context.wrap_socket(raw_sock, server_hostname=host)
    try:
        ws_handshake(sock, host, port)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "browser_round",
            "params": [{"execution_id": hex_to_execution_id_bytes(execution_id_hex)}],
        }
        send_text_frame(sock, json.dumps(request).encode())
        return json.loads(recv_text_frame(sock))
    finally:
        sock.close()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <execution_id>", file=sys.stderr)
        sys.exit(1)
    execution_id_hex = sys.argv[1]

    coord_ip = get_stack_output("CoordPublicIp")
    print(f"Coordinator: {coord_ip}:{COORD_BROWSER_PORT}", file=sys.stderr)

    response = query_round(coord_ip, COORD_BROWSER_PORT, execution_id_hex)

    if "error" in response:
        error = response["error"]
        print(json.dumps(error, indent=2))
        if "missing field `request`" in str(error.get("data", "")):
            print(
                "\nThis coordinator still requires authentication on browser_round - "
                "the fix (dropping authenticate() from that RPC) hasn't been deployed to "
                "it yet. See stoffel-mpc-coordinator's browser_rpc.rs.",
                file=sys.stderr,
            )
        sys.exit(1)

    print(response["result"])


if __name__ == "__main__":
    main()
