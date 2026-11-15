"""Operator-invoked, read-only verification of the configured LAN HTTPS login.

This module does not import the application, load dotenv, authenticate, follow
redirects, change trust/firewall state, or start/restart any service.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import IntEnum
import hashlib
import http.client
import math
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

try:
    from . import lan_setup
except ImportError:  # Direct execution from the Windows bundle.
    import lan_setup


MAX_BODY_BYTES = 128 * 1024
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 8.0


class ExitCode(IntEnum):
    SERVING = 0
    CONFIGURATION = 1
    # argparse reserves 2 for an invalid command line.
    UNREACHABLE = 3
    TLS = 4
    TIMEOUT = 5
    RESPONSE = 6


@dataclass(frozen=True)
class ProbeResult:
    code: ExitCode
    message: str


class _UnexpectedResponse(Exception):
    pass


def _pinned_context(root: Path, state: dict) -> ssl.SSLContext:
    certificate = lan_setup._root_file(
        root, lan_setup.CLIENT_KIT_DIRECTORY / lan_setup.CLIENT_CERTIFICATE_NAME
    ).read_bytes()
    if hashlib.sha256(certificate).hexdigest() != state["rootCertificateSha256"]:
        raise lan_setup.LanSetupError("The public LAN certificate identity changed.")
    # Construct an empty client trust store: do not also trust unrelated system
    # roots. The client-kit file is DER and has just been checked against state.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
    return context


def _fetch_login(host: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
    """Verify one HTTPS transaction with an absolute deadline and size cap."""
    deadline = time.monotonic() + timeout
    expired = threading.Event()
    transport = None
    response = None
    connection = http.client.HTTPSConnection(host, port, context=context)

    def remaining(cap: float) -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0 or expired.is_set():
            raise TimeoutError("HTTPS probe deadline expired.")
        return min(cap, seconds)

    def interrupt_transport():
        expired.set()
        current = transport
        if current is not None:
            try:
                # shutdown also interrupts a response's buffered makefile read;
                # close alone may leave that read alive until its next byte.
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, interrupt_transport)
    timer.daemon = True
    timer.start()
    try:
        # State supplies one canonical numeric IPv4 address; no DNS or proxies.
        transport = socket.create_connection(
            (host, port), timeout=remaining(CONNECT_TIMEOUT_SECONDS)
        )
        transport = context.wrap_socket(
            transport, server_hostname=host, do_handshake_on_connect=False
        )
        transport.settimeout(remaining(CONNECT_TIMEOUT_SECONDS))
        transport.do_handshake()
        connection.sock = transport
        transport.settimeout(remaining(READ_TIMEOUT_SECONDS))
        connection.request("GET", "/login", headers={
            "Accept": "text/html",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise _UnexpectedResponse("The HTTPS login did not return HTTP 200.")
        if response.getheader("Content-Type", "").split(";", 1)[0].lower() != "text/html":
            raise _UnexpectedResponse("The HTTPS login did not return HTML.")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise _UnexpectedResponse("The HTTPS login returned an unsupported encoding.")
        declared_length = response.getheader("Content-Length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise _UnexpectedResponse("The HTTPS login length is invalid.") from exc
            if not 0 <= length <= MAX_BODY_BYTES:
                raise _UnexpectedResponse("The HTTPS login exceeds the response limit.")
        body = bytearray()
        while not response.isclosed():
            transport.settimeout(remaining(READ_TIMEOUT_SECONDS))
            chunk = response.read(min(4096, MAX_BODY_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise _UnexpectedResponse("The HTTPS login exceeds the response limit.")
        if response.length not in (None, 0):
            raise _UnexpectedResponse("The HTTPS login response was incomplete.")
        remaining(READ_TIMEOUT_SECONDS)
        if b'id="login-form"' not in body or b"CSD FARS" not in body:
            raise _UnexpectedResponse("The HTTPS page is not the expected CSD login.")
    except (OSError, http.client.HTTPException, _UnexpectedResponse) as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("HTTPS probe deadline expired.") from exc
        raise
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()
        if transport is not None:
            transport.close()


def probe(root: Path, server_ipv4: str, prefix_length: int,
          timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ProbeResult:
    if not math.isfinite(timeout) or not 1 <= timeout <= 30:
        raise ValueError("The HTTPS timeout must be from 1 through 30 seconds.")
    try:
        root = root.resolve(strict=True)
        state = lan_setup.check_ready(
            root, current_server_ipv4=server_ipv4, current_prefix_length=prefix_length
        )
        context = _pinned_context(root, state)
    except (lan_setup.LanSetupError, OSError, ValueError, KeyError):
        return ProbeResult(ExitCode.CONFIGURATION,
                           "LAN configuration or pinned certificate validation failed.")
    try:
        _fetch_login(state["serverIpv4"], lan_setup.HTTPS_PORT, context, timeout)
    except ssl.SSLError:
        return ProbeResult(ExitCode.TLS,
                           "HTTPS certificate, hostname, or TLS negotiation verification failed.")
    except TimeoutError:
        return ProbeResult(ExitCode.TIMEOUT,
                           "The HTTPS login probe exceeded its connection/read deadline.")
    except (OSError, http.client.HTTPException):
        return ProbeResult(ExitCode.UNREACHABLE,
                           "The configured HTTPS server is unreachable or closed the connection.")
    except _UnexpectedResponse as exc:
        return ProbeResult(ExitCode.RESPONSE, str(exc))
    return ProbeResult(ExitCode.SERVING,
                       "Verified HTTPS is serving the expected CSD login page.")


def _timeout_argument(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be a number from 1 to 30.") from exc
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise argparse.ArgumentTypeError("Timeout must be a number from 1 to 30.")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--prefix-length", type=int, required=True)
    parser.add_argument("--timeout", type=_timeout_argument, default=DEFAULT_TIMEOUT_SECONDS)
    arguments = parser.parse_args(argv)
    result = probe(arguments.root, arguments.server_ip, arguments.prefix_length, arguments.timeout)
    stream = sys.stdout if result.code == ExitCode.SERVING else sys.stderr
    print(result.message, file=stream)
    return int(result.code)


if __name__ == "__main__":
    raise SystemExit(main())
