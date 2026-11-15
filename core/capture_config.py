"""Lightweight shared contract for the bounded camera-helper configuration write."""

from __future__ import annotations

import json
import math
import sys


CAPTURE_CONFIG_MAX_BYTES = 1920
# Python's JSON encoding of a finite binary64 float needs at most 24 ASCII
# characters, including sign, decimal point and exponent. Reserve that whole
# field before launching a helper; its actual deadline is known only at ready.
_RESERVED_DEADLINE = -sys.float_info.max


def build_capture_config(source, *, width=None, height=None, target_fps=None,
                         open_timeout=3.0, read_timeout=3.0):
    """Build the exact helper fields from CaptureProcess constructor options."""
    try:
        return {"source": source, "width": width, "height": height,
                "target_fps": target_fps,
                "open_timeout_ms": max(1, int(open_timeout * 1000)),
                "read_timeout_ms": max(1, int(read_timeout * 1000))}
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid camera connection configuration") from None


def serialize_capture_config(config, *, open_deadline_mono=None):
    """Encode one bounded newline-terminated helper write.

    With no deadline this is a preflight only: the returned bytes contain a
    maximum-width reserved deadline and MUST NOT be sent to a helper. Pass the
    actual monotonic deadline to encode the final write. Both paths use the same
    JSON representation (including Unicode escaping), field set and byte limit.
    This module deliberately imports no camera, application or database code.
    """
    try:
        deadline = (_RESERVED_DEADLINE if open_deadline_mono is None
                    else float(open_deadline_mono))
        if not math.isfinite(deadline):
            raise ValueError
        payload = dict(config)
        payload["open_deadline_mono"] = deadline
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid camera connection configuration") from None
    # Below the minimum platform pipe buffer, even with a child stalled before
    # reading config. Include the dynamic deadline and trailing newline.
    if len(encoded) > CAPTURE_CONFIG_MAX_BYTES:
        raise ValueError("Camera connection configuration is too long")
    return encoded
