"""Offline application smoke checks in an explicitly owned fresh rehearsal.

The driver creates .fresh-rehearsal.json before initializing its synthetic Dean.
This helper is never an installed-school database inspection or a server start.
"""
from __future__ import annotations

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import io
import json
import logging
import os
from pathlib import Path
import re
import struct
import sys
import tempfile


class SmokeError(RuntimeError):
    pass


class _QuietOutput(io.TextIOBase):
    """Discard dependency/application console output without accumulating it."""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SmokeError("Invalid smoke invocation")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SmokeError("Duplicate document field")
        result[key] = value
    return result


def _normal(path: Path):
    for entry in (path, *path.parents):
        metadata = entry.lstat()
        if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise SmokeError("Linked rehearsal path")


def _read_document(path: Path, limit=16384):
    _normal(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise SmokeError("Invalid rehearsal document")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise SmokeError("Invalid rehearsal document")
    result = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise SmokeError("Invalid rehearsal document")
    return result


def validate_rehearsal(root, nonce):
    """Validate public ownership before opening an environment or database."""
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise SmokeError("Invalid rehearsal identity")
    supplied = Path(root).absolute()
    _normal(supplied)
    stage = supplied.resolve(strict=True)
    if (stage != Path(__file__).resolve(strict=True).parents[2]
            or stage != Path.cwd().resolve(strict=True)
            or not stage.is_dir()
            or not re.fullmatch(r"fresh-rehearsal-[A-Za-z0-9_-]{8,96}", stage.name)):
        raise SmokeError("An installed fresh rehearsal is required")
    marker = _read_document(stage / ".fresh-rehearsal.json")
    manifest = _read_document(stage / "CLIENT_BUNDLE_MANIFEST.json", 65536)
    revision = manifest.get("sourceCommit")
    if (set(marker) != {"format", "nonce", "sourceCommit"}
            or type(marker.get("format")) is not int or marker["format"] != 1
            or marker.get("nonce") != nonce
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or marker.get("sourceCommit") != revision):
        raise SmokeError("Rehearsal ownership differs from the candidate")
    if (stage / "fresh-smoke-complete.json").exists():
        raise SmokeError("A fresh invocation is required")
    return stage, revision


def read_credentials(stream):
    content = stream.read(16385)
    if not content or len(content) > 16384:
        raise SmokeError("Invalid synthetic input")
    value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or set(value) != {"username", "password"}
            or not isinstance(value["username"], str)
            or not re.fullmatch(r"synthetic-[A-Za-z0-9_-]{1,110}", value["username"])
            or not isinstance(value["password"], str)
            or not 8 <= len(value["password"]) <= 4096):
        raise SmokeError("Invalid synthetic input")
    return value


def install_guards(stage):
    """Deny side effects before importing application modules.

    Python audit hooks supplement explicit API refusal. Trusted native runtime
    libraries are not an OS sandbox; the rehearsal driver also bounds this child.
    """
    import cv2
    import socket
    import subprocess
    import threading

    denied = []

    def refuse(*args, **kwargs):
        denied.append(True)
        raise SmokeError("Rehearsal side effect refused")

    cv2.VideoCapture = refuse
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.socket.bind = refuse
    socket.socket.sendto = refuse
    socket.create_connection = refuse
    # Native dependency initialization can query the Windows runtime version.
    # This precedes application imports and never starts the CSD application.
    original_popen = subprocess.Popen

    def hidden_runtime_probe(*args, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return original_popen(*args, **kwargs)

    subprocess.Popen = hidden_runtime_probe
    import face_recognition  # noqa: F401
    subprocess.Popen = refuse
    os.system = refuse
    if hasattr(os, "startfile"):
        os.startfile = refuse
    import simpleaudio
    simpleaudio.play_buffer = refuse
    simpleaudio.WaveObject.play = refuse

    audio_threads = []
    original_start = threading.Thread.start

    def start_idle_audio(thread):
        target = getattr(thread, "_target", None)
        if (audio_threads or not thread.daemon
                or getattr(target, "__module__", None) != "core.audio_queue"
                or getattr(target, "__qualname__", None) != "ThreadedAudioQueue._worker"):
            refuse()
        audio_threads.append(thread)
        return original_start(thread)

    threading.Thread.start = start_idle_audio

    def within(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(value)).resolve().is_relative_to(stage)

    def audit(event, arguments):
        if event == "open":
            name, mode, flags = arguments
            writing = (isinstance(mode, str) and any(flag in mode for flag in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not within(name):
                refuse()
        elif event == "sqlite3.connect" and not within(arguments[0]):
            refuse()
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            if not within(arguments[0]):
                refuse()
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if not within(arguments[0]) or not within(arguments[1]):
                refuse()
        elif event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2",
                       "socket.connect", "socket.bind", "socket.sendto", "sqlite3.load_extension"}:
            refuse()

    sys.addaudithook(audit)
    return denied, audio_threads


def _assert_empty_fixture(database, username):
    import sqlite3
    with closing(sqlite3.connect(database)) as connection:
        users = connection.execute("SELECT username,role,is_active,requires_password_change FROM users").fetchall()
        if users != [(username, "Dean", 1, 0)]:
            raise SmokeError("Synthetic administrator fixture required")
        for table in ("students", "embeddings", "classes", "camera_devices",
                      "attendance_sessions", "attendance_records"):
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
                raise SmokeError("Empty synthetic fixture required")


class _CsrfParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "input" and values.get("name") == "csrf_token":
            self.values.append(values.get("value"))


def exercise_app(stage, credentials):
    from dotenv import dotenv_values
    # Use only this newly generated environment, before config/application import.
    values = dotenv_values(stage / ".env")
    for name, value in values.items():
        if isinstance(value, str):
            os.environ[name] = value
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    sys.path.insert(0, str(stage))
    import config
    database = stage / "database" / "attendance.db"
    if (Path(config.BASE_DIR).resolve() != stage
            or Path(config.DATABASE_PATH).resolve() != database):
        raise SmokeError("Rehearsal database selection failed")
    _assert_empty_fixture(database, credentials["username"])
    import app
    from security.login_notice import VERSION

    app.app.config["TESTING"] = True
    if app.app.config.get("WTF_CSRF_ENABLED", True) is not True:
        raise SmokeError("Normal CSRF enforcement required")
    checks = {"empty_synthetic_fixture": True}
    with app.app.test_client() as browser:
        login = browser.get("/login")
        if login.status_code != 200 or b'id="login-form"' not in login.data:
            raise SmokeError("Login rendering failed")
        parser = _CsrfParser()
        parser.feed(login.get_data(as_text=True))
        if not parser.values or not all(isinstance(value, str) and value for value in parser.values):
            raise SmokeError("Login CSRF token missing")
        if browser.post("/login", data={"username": credentials["username"]}).status_code != 400:
            raise SmokeError("Login CSRF enforcement failed")
        response = browser.post("/login", data={**credentials, "csrf_token": parser.values[0],
            "notice_acknowledged": "1", "notice_version": VERSION})
        if response.status_code != 302 or response.headers.get("Location") != "/dashboard":
            raise SmokeError("Synthetic credential login failed")
        checks.update(login_form=True, csrf_enforced=True, credential_login=True)
        pages = {
            "dashboard": "/dashboard", "camera_setup": "/settings/cameras",
            "attendance_setup": "/settings/attendance", "location_setup": "/settings/locations",
            "program_setup": "/settings/programs", "classes": "/classes",
            "sessions": "/sessions", "reports": "/reports?date_scope=all",
            "camera_catalog": "/api/cameras", "capacity_settings": "/api/cameras/capacity-limits",
        }
        for code, route in pages.items():
            page = browser.get(route)
            if page.status_code != 200 or b'id="login-form"' in page.data:
                raise SmokeError("Authenticated page failed")
            checks[code] = True
        exported = browser.get("/api/reports/export?date_scope=all")
        if (exported.status_code != 200
                or exported.mimetype != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                or exported.headers.get("X-Attendance-Record-Count") != "0"
                or not exported.data.startswith(b"PK")):
            raise SmokeError("Excel export failed")
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(exported.data), read_only=True)
        try:
            if "Attendance Export" not in workbook.sheetnames or workbook["Attendance Export"].max_row != 1:
                raise SmokeError("Excel workbook validation failed")
        finally:
            workbook.close()
        checks["excel_export"] = True
        for template in app.app.jinja_env.list_templates():
            app.app.jinja_env.get_template(template)
        checks["templates"] = True
        if browser.get("/logout").status_code != 302:
            raise SmokeError("Logout failed")
        if browser.get("/settings/cameras").status_code != 302:
            raise SmokeError("Protected-page logout failed")
        checks["logout"] = True
    _assert_empty_fixture(database, credentials["username"])
    from workers.session_manager import session_manager
    if (session_manager.active_sessions or session_manager._camera_leases
            or session_manager._manager is not None or session_manager._manager_control is not None):
        raise SmokeError("Unexpected session work")
    checks["no_camera_or_attendance_work"] = True
    return checks


def main(argv=None):
    stage = None
    revision = "unknown"
    nonce = None
    phase = "ownership"
    audio_threads = []
    try:
        parser = _Parser(description="Check an owned synthetic fresh candidate offline")
        parser.add_argument("--root", required=True)
        parser.add_argument("--nonce", required=True)
        parser.add_argument("--credentials-stdin", action="store_true", required=True)
        arguments = parser.parse_args(argv)
        stage, revision = validate_rehearsal(arguments.root, arguments.nonce)
        nonce = arguments.nonce
        phase = "synthetic_input"
        credentials = read_credentials(sys.stdin.buffer)
        with redirect_stdout(_QuietOutput()), redirect_stderr(_QuietOutput()):
            phase = "runtime"
            import platform
            if platform.python_version() != "3.11.16" or struct.calcsize("P") != 8:
                raise SmokeError("Pinned candidate runtime required")
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(stage))
            # Check the installation binding before importing application code.
            from scripts.client import client_setup
            phase = "installation_binding"
            client_setup.check_install(stage)
            phase = "guards"
            denied, audio_threads = install_guards(stage)
            with tempfile.TemporaryDirectory(prefix=".fresh-smoke-", dir=stage) as temporary:
                tempfile.tempdir = temporary
                os.environ["TEMP"] = temporary
                os.environ["TMP"] = temporary
                phase = "application_checks"
                checks = exercise_app(stage, credentials)
                credentials.clear()
                phase = "shutdown"
                from core.audio_queue import audio_player
                audio_player.shutdown()
                if denied or any(thread.is_alive() for thread in audio_threads):
                    raise SmokeError("Unexpected background work")
                checks["guards_and_audio_shutdown"] = True
                logging.shutdown()
            receipt = {"format": 1, "nonce": nonce, "sourceCommit": revision,
                       "status": "passed", "checks": checks}
            phase = "receipt"
            with (stage / "fresh-smoke-complete.json").open("x", encoding="ascii") as stream:
                json.dump(receipt, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
        return 0
    except BaseException:
        # Never print tracebacks, credentials, raw database values or local paths.
        audio = sys.modules.get("core.audio_queue")
        player = getattr(audio, "audio_player", None)
        if player is not None and any(thread.is_alive() for thread in audio_threads):
            try:
                player.shutdown()
            except BaseException:
                pass
        logging.shutdown()
        print(json.dumps({"format": 1, "nonce": nonce, "sourceCommit": revision,
                          "status": "failed", "stage": phase, "code": "FRESH_SMOKE_FAILED"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
