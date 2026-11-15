"""Camera inventory mutations. Call under lifecycle guard + BEGIN IMMEDIATE.

Never open a device here. Availability and deletion preserve referenced history.
"""
import json
import sqlite3


class CameraManagementError(ValueError):
    def __init__(self, message, code='camera_unavailable', status=409, *, detail_code=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.detail_code = detail_code


def record_change(conn, action, actor, details):
    """Require a durable receipt in the same transaction as a lifecycle change."""
    try:
        conn.execute('INSERT INTO system_audit_logs(actor_user_id,action,details) VALUES(?,?,?)',
                     (actor, action, json.dumps(details)))
    except sqlite3.Error as exc:
        raise CameraManagementError('The camera change could not be recorded in the audit history. No change was saved. Retry or contact IT Support.',
                                    'camera_audit_failed', 503) from exc


def room_available(conn, room_id):
    return room_id is not None and conn.execute('''SELECT 1 FROM rooms r
        JOIN floors f ON f.id=r.floor_id JOIN buildings b ON b.id=f.building_id
        WHERE r.id=? AND r.is_active=1 AND f.is_active=1 AND b.is_active=1''',
        (room_id,)).fetchone() is not None


def require_idle(conn, camera, manager):
    if manager.get_camera_owner(camera['id']) is not None:
        raise CameraManagementError('Camera is busy. Stop its live session, tuner or preview and wait for confirmed cleanup before changing availability.', 'camera_busy', detail_code='camera_in_use')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attendance_process_owners'").fetchone():
        if conn.execute('''SELECT 1 FROM attendance_process_owners p
            JOIN attendance_sessions s ON s.id=p.session_id
            WHERE s.camera_id=? AND p.confirmed=0 LIMIT 1''', (camera['id'],)).fetchone():
            raise CameraManagementError('Camera cleanup is unconfirmed. Resolve its session in Classes before changing availability.', 'camera_busy', detail_code='cleanup_unconfirmed')
    if conn.execute('''SELECT 1 FROM classes c WHERE (c.camera_id=? OR c.room_id=?)
        AND (c.is_active=1 OR EXISTS (SELECT 1 FROM attendance_sessions a
            WHERE a.class_id=c.id AND a.status='Open')) LIMIT 1''',
        (camera['id'], camera['room_id'])).fetchone():
        raise CameraManagementError('End attendance using this camera or room from Classes before changing availability.', 'camera_busy', detail_code='attendance_in_progress')
    if conn.execute("SELECT 1 FROM attendance_sessions WHERE camera_id=? AND status='Open' LIMIT 1", (camera['id'],)).fetchone():
        raise CameraManagementError('End the open attendance session from Classes and wait for confirmed cleanup.', 'camera_busy', detail_code='attendance_in_progress')


def require_available(conn, camera, active, lifecycle):
    if camera['is_default'] and (not active or lifecycle != 'Ready'):
        raise CameraManagementError("Replace the default camera with another available camera first. Open the replacement camera's Availability section and choose Make default.", 'camera_default')
    if active and not room_available(conn, camera['room_id']) and not camera['is_default']:
        raise CameraManagementError('Assign an active room before making this camera available. Check Locations with the Dean.', 'camera_location')


def removal_blocker(code):
    """Fixed operator guidance. No record names, source strings or native errors."""
    retire_steps = [
        "First resolve any default-camera, active-use or unfinished-cleanup blockers listed here.",
        "Open this camera's Availability section and choose Retire. This prevents new use while keeping the camera record and its references.",
        "Find retired devices under Inactive / Retired or All cameras. Do not delete class or attendance records just to remove a camera.",
    ]
    guidance = {
        'default_camera': ('This is the default camera',
            'The default camera cannot be deleted or retired until another eligible camera replaces it.', [
                'Prepare another Ready camera in an active room. Ask the Dean to manage rooms if needed.',
                "After both cameras are no longer in use, open the replacement camera's Availability section and choose Make default.",
                'Review this camera again. Replacing the default does not erase either camera or its attendance history.',
            ]),
        'camera_in_use': ('A camera operation is still using this device',
            'An attendance worker, registration preview, recognition tuner, camera test or preview still owns the camera.', [
                'Ask the operator to stop the preview or tuner, wait for any camera test to finish, or ask the owning teacher to finish the attendance session.',
                'Wait for the stop to be confirmed, then check removal again. Retirement also waits for active use to finish.',
            ]),
        'cleanup_unconfirmed': ('A previous camera operation has not finished stopping',
            'A saved camera-process record still has unconfirmed cleanup, even if no picture is visible.', [
                'Ask the owning teacher to review the saved attendance session and retry its closure if that option is available.',
                'If cleanup remains unconfirmed, contact the server operator or IT Support for a safe shutdown check. Do not force-delete the camera or its records.',
                'Check removal again only after cleanup is confirmed.',
            ]),
        'attendance_in_progress': ('Attendance for this camera or room is still open',
            'The camera or its room is associated with active or open attendance. Changing the device now could interrupt that session.', [
                'Ask the owning teacher to finish the relevant attendance session and wait for confirmed camera cleanup.',
                'Check removal again afterward. Saved session history may still require retiring the camera instead of deleting it.',
            ]),
        'class_reference': ('A class still refers to this camera',
            'At least one saved class references this camera. The reference is protected even when attendance is not running.', retire_steps),
        'attendance_history': ('Attendance history uses this camera record',
            'A saved attendance session refers to this camera. Closed sessions also keep this reference for their history.', retire_steps),
        'start_history': ('An attendance start record uses this camera',
            'A saved attendance start attempt refers to this camera, including an unsuccessful or interrupted attempt.', retire_steps),
        'related_record': ('Another saved record still uses this camera',
            'A database reference prevents permanent deletion. The camera has been kept.', retire_steps),
    }
    if code not in guidance:
        code = 'related_record'
    title, reason, steps = guidance[code]
    return {'code': code, 'title': title, 'reason': reason, 'steps': list(steps)}


def deletion_blocker_details(conn, camera, manager):
    """Explain the existing delete guards without changing their predicates."""
    blockers = []
    if camera['is_default']:
        blockers.append(removal_blocker('default_camera'))
    try:
        require_idle(conn, camera, manager)
    except CameraManagementError as exc:
        blockers.append(removal_blocker(exc.detail_code or 'camera_in_use'))
    if conn.execute('SELECT 1 FROM classes WHERE camera_id=? LIMIT 1', (camera['id'],)).fetchone():
        blockers.append(removal_blocker('class_reference'))
    if conn.execute('SELECT 1 FROM attendance_sessions WHERE camera_id=? LIMIT 1', (camera['id'],)).fetchone():
        blockers.append(removal_blocker('attendance_history'))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attendance_start_attempts'").fetchone():
        if conn.execute('SELECT 1 FROM attendance_start_attempts WHERE camera_id=? LIMIT 1', (camera['id'],)).fetchone():
            blockers.append(removal_blocker('start_history'))
    return blockers


def deletion_blockers(conn, camera, manager):
    """Keep the existing string-list helper available to callers."""
    return [detail['reason'] for detail in deletion_blocker_details(conn, camera, manager)]
