"""Operator guidance from bounded capture evidence, never native error text.

Discovery, authentication and codec negotiation are not independently checked by
the capture backend. Do not infer a rejected password, closed port or bad codec
from its open/read failures, or expose URLs and credentials in diagnostics.
"""


def camera_test_diagnostic(code, camera_type="rtsp"):
    local = camera_type == "local"
    connection_checks = ([
        "Check that the camera is connected to the computer running CSD FARS, not just the computer running your browser.",
        "Check the USB camera number and Windows camera access settings. Close other apps using this camera, then retry.",
    ] if local else [
        "From the computer running CSD FARS, have your installer check the camera's power, network connection and video port. A discovery response does not verify video access.",
        "Check the camera's own username, password and permission to view video. These may differ from your CSD FARS login.",
        "Check the exact stream path and recorder channel with your installer. Port 554 and /Streaming/Channels/101 are defaults, not verified settings for every camera.",
        "If video works only with Wi-Fi off, ask your installer to check overlapping Wi-Fi/camera network address ranges and the Windows route to the camera. The Search options adapter controls discovery only, not video routing.",
    ])
    video_checks = ([
        "Close other apps using the USB camera. Check its selected number and try a supported resolution in Camera details.",
    ] if local else [
        "Ask your installer to verify that the selected recorder channel is online and supplies a continuous video stream.",
        "Check the stream's video format, resolution and connection limit. Try a supported lower-resolution stream supplied by your installer.",
        "If enabling Wi-Fi interrupts the picture, ask your installer to check the Windows route to the camera and overlapping network address ranges. Both connections can remain enabled with suitable network settings.",
    ])
    catalog = {
        "helper_ready_timeout": ("Camera reader initialization timed out", "Starting the reader",
            "The isolated reader did not finish initialization within the remaining test budget. No video connection was verified.",
            ["Retry once after this computer finishes starting. If it repeats, ask support to check this installation and available computer resources."]),
        "helper_initialization_failed": ("Camera reader could not initialize", "Starting the reader",
            "The isolated reader failed before video could be opened. No camera login or network fault was identified.",
            ["Ask support to verify the private runtime and pinned dependencies in this installation, then retry."]),
        "open_timeout": ("Camera startup timed out", "Opening video",
            "The initialized video reader did not finish opening within its opening limit and the remaining test budget. This does not identify a password or network fault.",
            ["Retry once after this computer finishes starting. If it repeats, check the connection settings below and ask support to check camera-reader startup.", *connection_checks]),
        "open_failed": ("Video connection could not open", "Opening video",
            "The video reader could not open the saved connection. It did not report whether the cause was login, network, stream settings or the local reader.", connection_checks),
        "read_timeout": ("Video opened, but a frame timed out", "Receiving video",
            "A video connection opened, but a read did not finish within the test's 3-second frame limit.", video_checks),
        "first_read_timeout": ("Video opened, but the first frame timed out", "Receiving the first image",
            "The video connection opened, but its first frame did not arrive within the remaining test budget.", video_checks),
        "read_failed": ("Video opened, but no usable frame arrived", "Receiving video",
            "The reader opened a video connection, then could not deliver a usable image. This does not confirm which stream or device setting caused it.", video_checks),
        "stale_frame": ("No fresh camera image arrived", "Receiving video",
            "The received image was too old for the fresh-frame test. A stale image cannot pass this test.", video_checks),
        "reader_start_failed": ("Camera reader could not start", "Starting the reader",
            "The local camera reader could not start. No video connection was verified.",
            ["Finish other camera checks and retry. If it repeats, ask support to check the CSD FARS installation and available computer resources."]),
        "cleanup_pending": ("Camera cleanup is still pending", "Stopping the test",
            "The system could not confirm that its test reader stopped. The camera remains reserved until cleanup is confirmed; this test is not recorded as passed.",
            ["Wait for the current camera owner to stop, then retry. Do not repeatedly start more tests.",
             "If it remains reserved, ask support to stop CSD FARS safely after active attendance sessions have ended."]),
        "cleanup_recovered": ("Camera reader stopped after a cleanup error", "Stopping the test",
            "The reader encountered a cleanup error, then stopped successfully. No new usable test image was confirmed. The test has released its camera reservation.",
            ["Retry the test once. If cleanup errors repeat, ask support to check camera-reader shutdown in this installation."]),
        "image_encoding_failed": ("Frame received, but test image could not be prepared", "Preparing the image",
            "The reader received a frame, but CSD FARS could not convert it into the test image. The test did not pass.",
            [*video_checks, "If retrying fails, ask support to check image processing in this installation."]),
        "saved_connection_invalid": ("Saved camera connection needs correction", "Checking saved settings",
            "The saved connection exceeds the reader's configuration limit or is invalid. No camera connection was attempted and the saved test status was left unchanged.",
            ["An authorized operator should edit the connection details. Use a shorter stream address or ask your installer for one, then save and test again.",
             "Do not share private stream addresses or camera passwords with other users."]),
        "saved_connection_unreadable": ("Saved camera connection could not be read", "Reading saved settings",
            "CSD FARS could not read the protected saved connection. No camera login or video check was completed.",
            ["Ask support to check that this installation has its original camera encryption key. Do not replace or share the key.",
             "After the installation is checked, an authorized operator can re-enter the camera connection details if needed."]),
        "test_timeout": ("No fresh frame before the test ended", "Waiting for video",
            "The bounded test ended without a fresh image. The available evidence does not identify the cause.",
            [*connection_checks, *video_checks]),
        "camera_busy": ("Camera test could not start", "Reserving the camera",
            "The camera or the allowed camera-check capacity is currently in use. No new connection test was completed.",
            ["Finish the owning attendance session, registration preview, tuner or other camera check before retrying. Check saved camera limits if no owner is shown."]),
        "result_save_failed": ("Test result could not be saved", "Saving the result",
            "CSD FARS could not confirm that the test result was saved. This is an application/storage error, not proof that the camera connection failed.",
            ["Refresh saved camera status before retrying. If it repeats, ask support to check database access and available disk space."]),
        "test_error": ("Camera test could not complete", "Running the test",
            "An unexpected application error prevented a confirmed test result. The camera's condition is unknown.",
            ["Refresh saved camera status and retry once. If it repeats, give support the reference code shown here; do not send passwords or private camera addresses."]),
    }
    if not isinstance(code, str) or code not in catalog:
        code = "test_error"
    title, stage, summary, checks = catalog[code]
    return {"code": code, "title": title, "stage": stage, "summary": summary, "checks": list(checks)}


def classify_camera_test(snapshot, *, started, frame_received, cleanup_ok, encoded):
    """Return a fixed code; do not trust or interpolate native/plugin strings."""
    if not cleanup_ok:
        return "cleanup_pending"
    if frame_received:
        return None if encoded else "image_encoding_failed"
    state = snapshot if isinstance(snapshot, dict) else {}
    reason = state.get("last_failure")
    if reason == "cleanup_failed":
        return "cleanup_recovered"
    if reason in ("helper_ready_timeout", "helper_initialization_failed", "open_timeout", "open_failed",
                  "first_read_timeout", "read_timeout", "reader_start_failed"):
        return reason
    if reason in ("read_failed", "read_exception"):
        return "read_failed"
    if reason == "stale_frame" or state.get("frame_stale") is True:
        return "stale_frame"
    if not started:
        return "reader_start_failed"
    return "test_timeout"
