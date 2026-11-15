"""Run CSD Attendance behind its pinned, private-LAN HTTPS gateway."""

from __future__ import annotations

import argparse
import http.client
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from waitress import create_server

try:
    from . import lan_setup
except ImportError:  # Direct execution by the Windows launcher.
    import lan_setup


MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_REQUEST_HEADER_BYTES = 64 * 1024


def configure_lan_app(
    flask_app: Any, server_ipv4: str, *, prefix_length: int
) -> Any:
    """Apply LAN-only Flask controls before the application's first request."""
    address, _ = lan_setup.parse_private_server_network(server_ipv4, prefix_length)
    # A generic LAN configuration does not prove any browser kit's identity.
    # Only the validated installation loader below supplies that public record.
    flask_app.config.pop("CSD_LAN_KIT_CONNECTION", None)
    flask_app.config.update(
        CSD_SERVING_MODE="lan",
        CSD_PUBLIC_URL=f"https://{address}",
        TRUSTED_HOSTS=[str(address)],
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_NAME="__Host-csd_session",
        SESSION_COOKIE_DOMAIN=None,
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PARTITIONED=False,
        WTF_CSRF_ENABLED=True,
        WTF_CSRF_SSL_STRICT=True,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    )

    extension_key = "csd_lan_security_headers"
    if not flask_app.extensions.get(extension_key):
        from flask import request

        @flask_app.after_request
        def apply_lan_response_controls(response):
            for name, value in lan_setup.SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
            response.headers.setdefault("Referrer-Policy", "same-origin")
            if not request.path.startswith("/static/"):
                response.headers.setdefault("Cache-Control", "no-store, private")
                response.headers.setdefault("Pragma", "no-cache")
            return response

        flask_app.extensions[extension_key] = True
    return flask_app


def _load_application(root: Path, state: dict[str, Any]):
    environment_path = root / ".env"
    if not environment_path.is_file():
        raise lan_setup.LanSetupError("The private .env is missing. Run FIRST_RUN.cmd.")
    load_dotenv(environment_path, override=True)
    os.environ["SESSION_WORKER_MODE"] = "process"
    os.environ["FLASK_HOST"] = "127.0.0.1"
    os.environ["FLASK_DEBUG"] = "false"
    os.environ["FLASK_PORT"] = str(state["backendPort"])
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app import app

    from services.stream_capacity import configure_worker_capacity
    configure_worker_capacity(app, int(state["workerThreads"]))
    configure_lan_app(app, state["serverIpv4"], prefix_length=state["prefixLength"])
    app.config.update(
        CSD_INSTALL_ROOT=str(root),
        CSD_LAN_KIT_CONNECTION=lan_setup._connection_document(state),
    )
    return app


def _wait_for_backend(port: int, host_header: str, server_thread: threading.Thread) -> None:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            raise lan_setup.LanSetupError("The loopback application server stopped during startup.")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/login", headers={"Host": host_header})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
            last_error = RuntimeError(f"unexpected /login status {response.status}")
        except OSError as exc:
            last_error = exc
        finally:
            connection.close()
        time.sleep(0.25)
    raise lan_setup.LanSetupError(
        f"The loopback application server did not become ready: {last_error}"
    )


def _stop_caddy(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        process.terminate()
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
    process.kill()
    process.wait(timeout=10)


def _create_backend(flask_app, state):
    return create_server(
        flask_app,
        host="127.0.0.1",
        port=int(state["backendPort"]),
        threads=int(state["workerThreads"]),
        url_scheme="https",
        # Only the local Caddy gateway may supply a client address. Keep Host,
        # scheme and all other forwarding headers outside this trust boundary.
        trusted_proxy="127.0.0.1",
        trusted_proxy_count=1,
        trusted_proxy_headers={"x-forwarded-for"},
        clear_untrusted_proxy_headers=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        max_request_header_size=MAX_REQUEST_HEADER_BYTES,
        expose_tracebacks=False,
        ident=None,
    )


def run(root: Path) -> int:
    root = root.resolve(strict=True)
    state = lan_setup.check_ready(root)
    flask_app = _load_application(root, state)
    from services.session_lifecycle import start_coordinator
    start_coordinator()
    server = _create_backend(flask_app, state)
    server_thread = threading.Thread(
        target=server.run,
        name="csd-waitress-loopback",
        daemon=True,
    )

    caddy_executable = root / "tools" / "caddy" / "caddy.exe"
    caddyfile = root / lan_setup.CADDYFILE_PATH
    caddy_environment = os.environ.copy()
    caddy_environment.update(lan_setup.caddy_environment(root))
    caddy_environment.pop("HTTP_PROXY", None)
    caddy_environment.pop("HTTPS_PROXY", None)
    caddy_environment.pop("ALL_PROXY", None)

    validate = subprocess.run(
        [
            str(caddy_executable),
            "validate",
            "--config",
            str(caddyfile),
            "--adapter",
            "caddyfile",
        ],
        cwd=root,
        env=caddy_environment,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if validate.returncode != 0:
        raise lan_setup.LanSetupError(
            "The pinned HTTPS gateway rejected its generated configuration."
        )

    caddy_process: subprocess.Popen[Any] | None = None
    try:
        server_thread.start()
        _wait_for_backend(
            int(state["backendPort"]),
            str(state["serverIpv4"]),
            server_thread,
        )
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        caddy_process = subprocess.Popen(
            [
                str(caddy_executable),
                "run",
                "--config",
                str(caddyfile),
                "--adapter",
                "caddyfile",
            ],
            cwd=root,
            env=caddy_environment,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        print(f"CSD Attendance LAN server is ready at {state['publicUrl']}", flush=True)
        print("Keep this window open. Press Ctrl+C once to stop safely.", flush=True)
        while True:
            exit_code = caddy_process.poll()
            if exit_code is not None:
                raise lan_setup.LanSetupError(
                    f"The HTTPS gateway stopped unexpectedly with exit code {exit_code}."
                )
            if not server_thread.is_alive():
                raise lan_setup.LanSetupError(
                    "The loopback application server stopped unexpectedly."
                )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping CSD Attendance LAN server...", flush=True)
        return 0
    finally:
        if caddy_process is not None:
            _stop_caddy(caddy_process)
        server.close()
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CSD Attendance LAN server.")
    parser.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return run(arguments.root)
    except (lan_setup.LanSetupError, OSError) as exc:
        print(f"LAN SERVER FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
