"""Private descriptor subprocess. No Flask, configuration, or database imports.

RGB frames and returned vectors exist only in memory. The binary protocol does
not accept settings, identities, paths, keys, models, or arbitrary commands.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading


MAX_FRAME_BYTES = 64 * 1024 * 1024


def _read_exact(stream, length):
    value = bytearray(length)
    view = memoryview(value)
    offset = 0
    while offset < length:
        count = stream.readinto(view[offset:])
        if not count:
            raise EOFError
        offset += count
    return value


def _read_request(stream):
    header_length = struct.unpack("!I", _read_exact(stream, 4))[0]
    if not 0 < header_length <= 8192:
        raise ValueError("Invalid encoding request")
    header = json.loads(_read_exact(stream, header_length))
    if not isinstance(header, dict) or set(header) != {"kind", "id", "shape", "locations", "nbytes"}:
        raise ValueError("Invalid encoding request")
    if header["kind"] != "encode" or type(header["id"]) is not int or header["id"] < 1:
        raise ValueError("Invalid encoding request")
    shape = header["shape"]
    if (not isinstance(shape, list) or len(shape) != 3
            or any(type(value) is not int or value < 1 for value in shape) or shape[2] != 3):
        raise ValueError("Invalid RGB dimensions")
    length = header["nbytes"]
    if type(length) is not int or not 0 < length <= MAX_FRAME_BYTES or length != shape[0] * shape[1] * 3:
        raise ValueError("Invalid RGB payload length")
    locations = header["locations"]
    if not isinstance(locations, list) or len(locations) != 1:
        raise ValueError("Invalid face location")
    location = locations[0]
    if not isinstance(location, list) or len(location) != 4 or any(type(value) is not int for value in location):
        raise ValueError("Invalid face location")
    top, right, bottom, left = location
    if not (0 <= top < bottom <= shape[0] and 0 <= left < right <= shape[1]):
        raise ValueError("Invalid face location")
    return header, _read_exact(stream, length)


def run(encoder=None):
    """Run identical standard descriptors; encoder injection is for synthetic tests."""
    if sys.platform.startswith("linux"):
        import ctypes
        import signal
        expected_parent = int(os.environ["CSD_FACE_ENCODING_PARENT_PID"])
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError("Unable to contain encoding helper lifetime")
        if os.getppid() != expected_parent:
            os._exit(0)
    # Load native dependencies before the CRT stdin watcher starts on Windows.
    import numpy as np
    if encoder is None:
        import face_recognition
        encoder = face_recognition.face_encodings

    requests = queue.Queue(maxsize=1)

    def watch_parent():
        try:
            while True:
                requests.put(_read_request(sys.stdin.buffer))
        except Exception:
            # OS job/prctl containment also covers EOF while native code holds GIL.
            os._exit(0)

    threading.Thread(target=watch_parent, daemon=True, name="encoding-parent-watch").start()
    output = sys.stdout.buffer

    def reply(header, payload=b""):
        header["nbytes"] = len(payload)
        packed = json.dumps(header, separators=(",", ":")).encode("ascii")
        output.write(struct.pack("!I", len(packed)))
        output.write(packed)
        if payload:
            output.write(payload)
        output.flush()

    reply({"kind": "ready", "ok": True, "protocol": 1})
    while True:
        header, pixels = requests.get()
        try:
            frame = np.frombuffer(pixels, dtype=np.uint8).reshape(header["shape"])
            locations = [tuple(header["locations"][0])]
            # Preserve the exact existing call, including default model and jitters.
            vectors = encoder(frame, locations)
            if not isinstance(vectors, (list, tuple)) or len(vectors) > 1:
                raise ValueError("Invalid descriptor result")
            payload = b""
            if vectors:
                vector = np.asarray(vectors[0], dtype="<f8")
                if vector.shape != (128,) or not np.isfinite(vector).all():
                    raise ValueError("Invalid descriptor result")
                payload = vector.tobytes(order="C")
            reply({"kind": "encoded", "id": header["id"], "ok": True, "count": len(vectors)}, payload)
        except Exception:
            # Exception text may contain sensitive application or native details.
            reply({"kind": "encoded", "id": header["id"], "ok": False, "count": 0,
                   "error": "encoding_failed"})
        finally:
            # Do not retain the previous request while waiting for another one.
            frame = vectors = vector = locations = payload = pixels = header = None


if __name__ == "__main__":
    run()
