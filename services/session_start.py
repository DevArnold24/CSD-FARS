"""Reviewed Start commands and startup attempts attached to the attendance lifecycle.

Readiness never creates records or opens a device. The existing coordinator owns
startup supervision and cleanup; this module does not start background threads
at import time. Public snapshots contain no camera sources or biometric values.
"""
import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid

from itsdangerous import BadSignature, URLSafeSerializer

from services import session_lifecycle as life

START_BOUND = 30.0
REVIEW_BOUND = 30.0
BOOT = str(uuid.uuid4())
SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_start_attempts (
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE,
 session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 mode TEXT NOT NULL CHECK(mode IN ('camera','manual')),
 phase TEXT NOT NULL, camera_id INTEGER, accepted_at REAL NOT NULL,
 deadline_mono REAL NOT NULL, server_boot TEXT NOT NULL,
 ready_at REAL, failure TEXT NOT NULL DEFAULT '', dispatch TEXT NOT NULL,
 config_digest TEXT NOT NULL, coverage_digest TEXT NOT NULL, camera_digest TEXT NOT NULL,
 overrides TEXT NOT NULL DEFAULT '{}', dispatch_owner TEXT
);
CREATE INDEX IF NOT EXISTS idx_start_session ON attendance_start_attempts(session_id,id);
CREATE TABLE IF NOT EXISTS attendance_start_operations (
 operation_id TEXT PRIMARY KEY, class_id INTEGER NOT NULL, actor INTEGER NOT NULL,
 session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 attempt_id INTEGER NOT NULL REFERENCES attendance_start_attempts(id) ON DELETE CASCADE,
 intent TEXT NOT NULL, accepted_at REAL NOT NULL
);
"""


class StartError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def install_schema(conn):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_start_attempts' AND type='table'").fetchone() is not None


def current(conn, sid):
    if not sid or not available(conn):
        return None
    row = conn.execute('SELECT * FROM attendance_start_attempts WHERE session_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
    return dict(row) if row else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def class_row(conn, class_id, actor):
    row = conn.execute('SELECT * FROM classes WHERE id=?', (class_id,)).fetchone()
    if not row:
        raise StartError('Class not found.', 404)
    if row['teacher_id'] != actor:
        raise StartError('Forbidden', 403)
    if row['is_archived']:
        raise StartError('Archived classes are non-operational.')
    return dict(row)


def camera_for(conn, room):
    row = conn.execute("""SELECT * FROM camera_devices WHERE room_id=?
        AND is_active=1 AND lifecycle_status='Ready'""", (room,)).fetchone()
    return dict(row) if row else None


def camera_digest(camera):
    # Health observations do not change reviewed configuration or prove readiness.
    keys = ('id', 'room_id', 'is_active', 'lifecycle_status', 'camera_type', 'source', 'resolution_width',
            'resolution_height', 'target_fps', 'match_threshold')
    return digest({key: camera.get(key) for key in keys}) if camera else digest(None)


def roster(conn, class_id, occurrence=None):
    if occurrence:
        from services.attendance_sessions import occurrence_roster_rows
        ids = [row['id'] for row in occurrence_roster_rows(conn, occurrence['id'], class_id)]
        rows = [conn.execute('SELECT id,student_id,name,is_active,consent_granted,permanently_removed_at FROM students WHERE id=?', (sid,)).fetchone() for sid in ids]
    else:
        rows = conn.execute('''SELECT s.id,s.student_id,s.name,s.is_active,s.consent_granted,s.permanently_removed_at
            FROM class_enrollments ce JOIN students s ON s.id=ce.student_id
            WHERE ce.class_id=? AND s.is_active=1 AND s.permanently_removed_at IS NULL ORDER BY s.id''', (class_id,)).fetchall()
    return [dict(row) for row in rows if row and row['is_active'] and row['permanently_removed_at'] is None]


def coverage(conn, students):
    from core.ai_pipeline import _decode_stored_embedding
    ready, records = set(), []
    for student in students:
        rows = conn.execute('SELECT face_vector FROM embeddings WHERE student_id=?', (student['id'],)).fetchall()
        fingerprints = []
        for row in rows:
            raw = row['face_vector']
            fingerprints.append(hashlib.sha256(bytes(raw)).hexdigest() if isinstance(raw, (bytes, memoryview)) else digest(str(raw)))
            if student['consent_granted'] and _decode_stored_embedding(raw) is not None:
                ready.add(student['id'])
        records.append((student['id'], student['consent_granted'], sorted(fingerprints)))
    # Live roster queries use ID order; historical occurrence queries use name
    # order. Membership/data identity must not depend on presentation ordering.
    return len(ready), digest(sorted(records))


def lookup_occurrence(conn, info):
    from services.attendance_sessions import get_open_attendance_session, get_attendance_occurrence, session_window_for_class
    window = session_window_for_class(info, conn=conn)
    occurrence = get_open_attendance_session(conn, info['id'])
    if not occurrence:
        occurrence = get_attendance_occurrence(conn, info['id'], window['scheduled_date'], window['effective_class']['start_time'])
    return occurrence, window


def fallback_allowed(conn, sid):
    attempt, state = current(conn, sid), life.lifecycle(conn, sid)
    from services.session_reopening import collection_window as reopening
    if reopening(conn, sid):
        return False
    return bool(attempt and attempt['mode'] == 'camera' and attempt['phase'] == 'interrupted'
                and state and state['collection'] == 'interrupted' and not state['end_operation']
                and state['cleanup'] in ('confirmed', 'not_applicable')
                and not conn.execute('SELECT 1 FROM attendance_process_owners WHERE session_id=? AND confirmed=0', (sid,)).fetchone()
                and not life.clock_unreliable(conn) and life.time.time() < state['cutoff'])


def readiness(conn, class_id, actor, mode, action='start', secret=None, occurrence_id=None, expected_date=None):
    from services.attendance_sessions import session_window_for_class
    from services.recognition_settings import load_settings, normal_match_threshold
    if mode not in ('camera', 'manual') or action not in ('start', 'resume', 'manual_fallback', 'reopen', 'resume_ended'):
        raise StartError('Choose camera or manual attendance and review the current action.', 400)
    info = class_row(conn, class_id, actor)
    occurrence, window = lookup_occurrence(conn, info)
    info = window['effective_class']
    if action in ('reopen', 'resume_ended') and occurrence_id is not None:
        occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=? AND class_id=?', (occurrence_id, class_id)).fetchone()
        if not occurrence:
            raise StartError('The intended attendance session is unavailable.', 409)
        occurrence = dict(occurrence)
    state = life.lifecycle(conn, occurrence['id']) if occurrence else None
    attempt = current(conn, occurrence['id']) if occurrence else None
    camera = camera_for(conn, info['room_id'])
    blockers, warnings = [], []
    if not available(conn):
        blockers.append('Session-start migration 027 is required. Ask the server operator to check this installation.')
    try:
        life.activation_check(conn)
    except ValueError as exc:
        blockers.append(str(exc))
    if action == 'start':
        if occurrence:
            blockers.append('Attendance already exists for this occurrence. View its current status; Start does not reopen or resume it.')
        elif not window['allowed']:
            blockers.append(window['message'])
    elif action in ('reopen', 'resume_ended'):
        from services.session_reopening import eligibility
        try:
            if mode != 'camera':
                raise ValueError('Reopening is available only for camera attendance.')
            eligibility(conn, occurrence, state, early=action == 'resume_ended')
        except ValueError as exc:
            blockers.append(str(exc))
    else:
        if not state or not life.snapshot(conn, occurrence['id'])['resume_available']:
            blockers.append('Continuation is unavailable. Check the original cutoff, End receipt, clock review and confirmed cleanup.')
        if occurrence and conn.execute('SELECT 1 FROM attendance_process_owners WHERE session_id=? AND confirmed=0', (occurrence['id'],)).fetchone():
            blockers.append('Camera cleanup is unconfirmed. Its owner reservations must be resolved before continuation.')
        if action == 'manual_fallback':
            if mode != 'manual' or not state or not fallback_allowed(conn, occurrence['id']):
                blockers.append('Manual continuation is available only after failed camera startup and confirmed cleanup, before cutoff.')
        elif occurrence:
            previous_mode = attempt['mode'] if attempt else ('camera' if occurrence['camera_id'] else 'manual')
            if previous_mode != mode:
                blockers.append('Resume preserves the current mode. General mode switching is unavailable.')
    students = roster(conn, class_id, occurrence if action != 'start' else None)
    if not students:
        blockers.append('No eligible students. Add eligible students to the class before starting attendance.')
    coverage_count, coverage_hash, coverage_state, overrides = None, '', 'not_required', {}
    if mode == 'camera':
        from services.camera_capacity import recognition_refusal
        refusal = recognition_refusal(conn, occurrence['id'] if occurrence else None)
        if refusal:
            blockers.append(refusal)
        if not camera:
            blockers.append('No active Ready camera is assigned to this room. Select manual attendance or ask an authorized operator to check the assignment.')
        else:
            reserved = conn.execute('''SELECT s.id FROM attendance_sessions s JOIN attendance_lifecycle l ON l.session_id=s.id
                WHERE s.camera_id=? AND l.cleanup='pending' AND s.id<>? LIMIT 1''', (camera['id'], occurrence['id'] if occurrence else -1)).fetchone()
            if reserved:
                blockers.append('The assigned camera is reserved by another occurrence. Its cleanup must be confirmed before camera attendance.')
            try:
                overrides = load_settings(conn)
                overrides['MATCH_THRESHOLD'] = normal_match_threshold(overrides['MATCH_THRESHOLD'], camera['match_threshold'])
            except (ValueError, KeyError):
                blockers.append('Camera recognition settings need attention from an authorized operator before camera attendance.')
        try:
            coverage_count, coverage_hash = coverage(conn, students)
            coverage_state = 'none' if coverage_count == 0 else ('partial' if coverage_count < len(students) else 'complete')
            if not coverage_count and students:
                blockers.append('No eligible students have usable recognition data. Select manual attendance or use the authorized enrollment workflow.')
            elif coverage_count < len(students):
                warnings.append(f'{coverage_count} of {len(students)} eligible students have usable recognition data. Others need individual attendance decisions and remain Pending if unrecorded.')
        except Exception:
            coverage_state = 'error'
            blockers.append('Recognition readiness could not be verified. Retry the check or deliberately select manual attendance.')
    pending_count = None
    if action in ('reopen', 'resume_ended') and not blockers:
        pending_count = eligibility(conn, occurrence, state, early=action == 'resume_ended')['pending']
    scheduled_date = occurrence['scheduled_date'] if occurrence and action != 'start' else window['scheduled_date']
    if expected_date is not None and expected_date != scheduled_date:
        raise StartError('This page refers to a different session date. Return to Sessions and select the intended meeting. No attendance was started.')
    cutoff = state['cutoff'] if state and action != 'start' else life.epoch(window['window_end'])
    if action in ('reopen', 'resume_ended') and not blockers:
        cutoff = eligibility(conn, occurrence, state, early=action == 'resume_ended')['limit']
    if state and action != 'start':
        late_at = state['late_at']
    else:
        from services.attendance_sessions import late_marking_time
        try:
            late_at = life.epoch(late_marking_time(window['scheduled_start'], info['late_threshold_minutes']))
        except ValueError as exc:
            raise StartError(str(exc), 409) from exc
    from services.attendance_settings import snapshot
    opening = snapshot(conn, occurrence['id']) if occurrence and action != 'start' else {
        'opens_at': life.epoch(window['window_start']) if not window['opening_error'] else None,
        'minutes': window['opening_minutes'], 'policy_version': window['opening_policy_version']}
    scheduled_start = (str(occurrence['scheduled_date']) + 'T' + str(occurrence['scheduled_start_time'])) if occurrence and action != 'start' else window['scheduled_start'].isoformat()
    facts = {'opening': opening, 'class': info, 'date': scheduled_date, 'mode': mode, 'action': action,
             'roster': [(s['id'], s['student_id'], s['name'], s['consent_granted']) for s in students],
             'camera': camera_digest(camera), 'coverage': coverage_hash, 'settings': overrides,
             'sid': occurrence['id'] if occurrence else None, 'version': state['version'] if state else None,
             'cutoff': cutoff, 'late_at': late_at, 'pending_count': pending_count}
    # Runtime is not a reviewed class edit; the occurrence/version is bound separately.
    facts['class'].pop('is_active', None)
    room = conn.execute('SELECT room_code,room_name FROM rooms WHERE id=?', (info['room_id'],)).fetchone()
    result = {'class_id': class_id, 'class_name': info['course_name'], 'date': scheduled_date,
              'room': room['room_code'] if room else 'No room assigned', 'mode': mode, 'action': action,
              'eligible_count': len(students), 'pending_count': pending_count,
              'reopening_end_label': life.local_sql(cutoff if action == 'resume_ended' else min(life.time.time() + 600, cutoff)) if action in ('reopen', 'resume_ended') and not blockers else None, 'coverage_count': coverage_count, 'coverage_state': coverage_state,
              'requires_acknowledgment': coverage_state == 'partial', 'camera_name': camera['name'] if camera else None,
              'camera_assigned': bool(camera), 'camera_readiness': 'Processing not yet verified' if mode == 'camera' else 'Not used in manual mode',
              'scheduled_start': scheduled_start, 'cutoff': cutoff, 'late_at': late_at,
              'early_start': action == 'start' and window['allowed'] and life.time.time() < life.epoch(window['scheduled_start']),
              'opening_at': opening['opens_at'] if opening else None,
              'opening_label': life.local_sql(opening['opens_at']) + ' (institutional time)' if opening and opening['opens_at'] is not None else 'Unavailable / not recorded for this session',
              'opening_minutes': opening['minutes'] if opening else None,
              'cutoff_label': life.local_sql(cutoff) + ' (institutional time)', 'late_label': life.local_sql(late_at) + ' (institutional time)',
              'blockers': blockers, 'warnings': warnings, 'can_confirm': not blockers,
              'session_id': facts['sid'], 'version': facts['version'], 'valid_for_seconds': REVIEW_BOUND,
              'fingerprint': digest(facts)}
    if secret and not blockers:
        result['review_token'] = URLSafeSerializer(secret, salt='attendance-start-review').dumps({
            'actor': actor, 'class_id': class_id, 'mode': mode, 'action': action,
            'sid': result['session_id'], 'date': scheduled_date, 'fingerprint': result['fingerprint'], 'issued': life.time.time(), 'mono': life.time.monotonic(), 'boot': BOOT})
    return result, {'info': info, 'occurrence': occurrence, 'state': state, 'camera': camera,
                    'coverage_digest': coverage_hash, 'camera_digest': camera_digest(camera), 'overrides': overrides,
                    'opening_policy_version': window['opening_policy_version']}


def decode_review(token, secret, actor, class_id):
    try:
        value = URLSafeSerializer(secret, salt='attendance-start-review').loads(token)
        if not isinstance(value, dict) or value['actor'] != actor or value['class_id'] != class_id:
            raise ValueError()
        return value
    except (BadSignature, KeyError, TypeError, ValueError):
        raise StartError('Review the current readiness details before confirming attendance.') from None


def expired(review):
    return (review.get('boot') != BOOT or not 0 <= life.time.time() - review['issued'] < REVIEW_BOUND
            or not 0 <= life.time.monotonic() - review['mono'] < REVIEW_BOUND)


def operation(conn, operation_id, actor, class_id):
    row = conn.execute('SELECT * FROM attendance_start_operations WHERE operation_id=?', (operation_id,)).fetchone()
    if row and (row['actor'] != actor or row['class_id'] != class_id):
        raise StartError('This Start operation does not belong to the intended class.')
    return dict(row) if row else None


def accept(conn, class_id, actor, payload, secret):
    """Caller holds BEGIN IMMEDIATE. No worker/device calls in this transaction."""
    from services.attendance_sessions import create_attendance_session
    class_row(conn, class_id, actor)
    if not available(conn):
        raise StartError('Session-start migration 027 is required. Ask the server operator to check this installation.')
    op = payload.get('operation_id')
    if not isinstance(op, str) or not 8 <= len(op) <= 80:
        raise StartError('A valid Start operation ID and reviewed details are required.', 400)
    intent = digest({key: payload.get(key) for key in ('mode', 'action', 'review_token', 'acknowledge_partial', 'reason')})
    previous = operation(conn, op, actor, class_id)
    if previous:
        if previous['intent'] != intent:
            raise StartError('The operation ID was reused with different details. Reconcile the original Start.')
        return previous['session_id']
    mode, action = payload.get('mode'), payload.get('action', 'start')
    review = decode_review(payload.get('review_token'), secret, actor, class_id)
    if review.get('mode') != mode or review.get('action') != action:
        raise StartError('The selected mode or action changed. Review it again before confirming.')
    if expired(review):
        raise StartError('Readiness details expired. Review the refreshed details and confirm again; your selected mode is unchanged.')
    fresh, private = readiness(conn, class_id, actor, mode, action, occurrence_id=review.get('sid'), expected_date=review.get('date'))
    existing = private['occurrence']
    if action == 'start' and existing:
        # Another tab may have accepted the same intended occurrence. Never turn
        # a repeated Start into Resume or overwrite its selected mode.
        attempt = current(conn, existing['id'])
        if attempt and attempt['mode'] == mode and attempt['config_digest'] == review['fingerprint']:
            conn.execute('INSERT INTO attendance_start_operations VALUES(?,?,?,?,?,?,?)',
                         (op, class_id, actor, existing['id'], attempt['id'], intent, life.time.time()))
            return existing['id']
        raise StartError('Attendance already exists or its reviewed details differ. View its current status; no new occurrence was created.')
    if fresh['fingerprint'] != review['fingerprint']:
        raise StartError('Attendance opening, roster, schedule, permission, camera or recognition details changed. Review again before confirming; your selected mode is unchanged.')
    if fresh['blockers']:
        raise StartError(fresh['blockers'][0])
    if fresh['requires_acknowledgment'] and payload.get('acknowledge_partial') is not True:
        raise StartError('Acknowledge the partial recognition-data coverage before starting camera attendance.')
    reason = payload.get('reason', '')
    if action in ('manual_fallback', 'reopen', 'resume_ended') and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
        raise StartError('A reason of 1 to 500 characters is required for this continuation.')
    # Coverage decoding can take time. Sample the strict deadline again after
    # validation while still holding the same SQLite writer reservation.
    if life.clock_unreliable(conn) or life.time.time() >= fresh['cutoff'] or expired(review):
        raise StartError('The confirmation expired or attendance time is unavailable. Review current details again.')
    info = private['info']
    camera_id = private['camera']['id'] if mode == 'camera' else None
    if action == 'start':
        info['camera_id'] = camera_id
        info['startup_requested'] = mode == 'camera'
        sid = create_attendance_session(conn, info, actor)
        conn.execute('INSERT INTO attendance_opening_snapshots(session_id,opens_at,minutes,policy_version) VALUES(?,?,?,?)',
                     (sid, fresh['opening_at'], fresh['opening_minutes'], private['opening_policy_version']))
    elif action in ('reopen', 'resume_ended'):
        from services.session_reopening import accept as accept_reopening
        sid = accept_reopening(conn, existing, private['state'], actor, reason, early=action == 'resume_ended')
    else:
        sid = existing['id']
        life.resume(conn, sid, fresh['version'], actor, starting=mode == 'camera')
        conn.execute('UPDATE attendance_sessions SET camera_id=? WHERE id=?', (camera_id, sid))
    now = life.time.time()
    token = str(uuid.uuid4())
    cursor = conn.execute('''INSERT INTO attendance_start_attempts
        (token,session_id,mode,phase,camera_id,accepted_at,deadline_mono,server_boot,ready_at,dispatch,config_digest,coverage_digest,camera_digest,overrides)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (token, sid, mode, 'starting' if mode == 'camera' else 'running', camera_id, now,
         life.time.monotonic() + START_BOUND, BOOT, now if mode == 'manual' else None,
         'queued' if mode == 'camera' else 'none', fresh['fingerprint'], private['coverage_digest'], private['camera_digest'], json.dumps(private['overrides'])))
    conn.execute('INSERT INTO attendance_start_operations VALUES(?,?,?,?,?,?,?)', (op, class_id, actor, sid, cursor.lastrowid, intent, now))
    conn.execute('''UPDATE attendance_lifecycle SET collection=?,cleanup=?,version=version+1,interruption_reason='' WHERE session_id=?''',
                 ('starting' if mode == 'camera' else 'collecting', 'pending' if mode == 'camera' else 'not_applicable', sid))
    conn.execute('UPDATE classes SET is_active=? WHERE id=?', (int(mode == 'manual'), class_id))
    life.event(conn, sid, 'start_accepted', actor, f'{action}: {mode}')
    if action == 'manual_fallback':
        # Literal prefix also keeps spreadsheet audit cells out of formula mode.
        life.event(conn, sid, 'manual_continuation', actor, 'Manual continuation reason: ' + reason.strip())
    return sid


def public(conn, sid):
    attempt = current(conn, sid)
    if not attempt:
        return None
    state = life.lifecycle(conn, sid)
    from services.attendance_settings import snapshot
    opening = snapshot(conn, sid)
    return {'opening_label': life.local_sql(opening['opens_at']) + ' (institutional time)' if opening else 'Not recorded for this session',
            'attempt_id': attempt['token'], 'mode': attempt['mode'], 'phase': attempt['phase'],
            'accepted_at': attempt['accepted_at'], 'ready_at': attempt['ready_at'],
            'failure': attempt['failure'], 'startup_bound_seconds': START_BOUND,
            'remaining_seconds': max(0, min(attempt['deadline_mono'] - life.time.monotonic(), state['cutoff'] - life.time.time())) if attempt['phase'] == 'starting' else 0,
            'manual_fallback_available': fallback_allowed(conn, sid)}


def interrupt_attempt(conn, sid, reason):
    if available(conn):
        conn.execute("UPDATE attendance_start_attempts SET phase='interrupted',failure=? WHERE session_id=? AND phase='starting'", (reason, sid))


def may_start(conn, sid, token):
    state, attempt = life.lifecycle(conn, sid), current(conn, sid)
    from services.session_reopening import collection_window as reopening, room_limit
    extension = reopening(conn, sid)
    if extension:
        occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (sid,)).fetchone()
        try:
            limit = min(extension['deadline'], room_limit(conn, occurrence, life.time.time()))
            if life.time.time() >= limit:
                return False
        except ValueError:
            return False
    return bool(state and attempt and attempt['token'] == token and attempt['phase'] == 'starting'
                and state['collection'] == 'starting' and not state['end_operation']
                and not life.clock_unreliable(conn) and life.time.time() < state['cutoff']
                and life.time.monotonic() < attempt['deadline_mono'])


def confirm_ready(conn, sid, token, camera_id, captured_mono, worker_token):
    """Called after an actual successful detection pass and encoder handshake."""
    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (sid,))
    if not may_start(conn, sid, token):
        return False
    attempt = current(conn, sid)
    if not isinstance(captured_mono, (float, int)) or not math.isfinite(captured_mono) or not 0 <= life.time.monotonic() - captured_mono <= 2.0:
        return False
    owner = conn.execute('SELECT * FROM attendance_process_owners WHERE token=? AND session_id=? AND confirmed=0', (worker_token, sid)).fetchone()
    if not owner or owner['kind'] != 'worker' or not owner_alive(owner):
        return False
    helpers = conn.execute("SELECT * FROM attendance_process_owners WHERE session_id=? AND confirmed=0 AND kind IN ('capture','encoding')", (sid,)).fetchall()
    if any(not any(row['kind'] == kind and owner_alive(row) for row in helpers) for kind in ('capture', 'encoding')):
        return False
    info = conn.execute('SELECT c.* FROM classes c JOIN attendance_sessions s ON s.class_id=c.id WHERE s.id=?', (sid,)).fetchone()
    camera = camera_for(conn, info['room_id'])
    if not camera or camera['id'] != camera_id or camera_id != attempt['camera_id']:
        life.interrupt(conn, sid, 'camera_assignment_changed')
        return False
    if camera_digest(camera) != attempt['camera_digest'] or info['is_archived']:
        life.interrupt(conn, sid, 'startup_details_changed')
        return False
    from services.recognition_settings import load_settings, normal_match_threshold
    settings = load_settings(conn)
    settings['MATCH_THRESHOLD'] = normal_match_threshold(settings['MATCH_THRESHOLD'], camera['match_threshold'])
    if settings != json.loads(attempt['overrides']):
        life.interrupt(conn, sid, 'startup_details_changed')
        return False
    occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (sid,)).fetchone()
    count, fingerprint = coverage(conn, roster(conn, info['id'], occurrence))
    if not count or fingerprint != attempt['coverage_digest']:
        life.interrupt(conn, sid, 'startup_details_changed')
        return False
    # Sample again after read/validation work and the writer lock wait.
    if not may_start(conn, sid, token):
        return False
    conn.execute("UPDATE attendance_start_attempts SET phase='running',ready_at=? WHERE token=?", (life.time.time(), token))
    conn.execute("UPDATE attendance_lifecycle SET collection='collecting',version=version+1 WHERE session_id=?", (sid,))
    conn.execute('UPDATE classes SET is_active=1 WHERE id=?', (info['id'],))
    life.event(conn, sid, 'camera_processing_ready', detail='Fresh detection pass and initialized encoder; no student match required')
    return True


def owner_alive(owner):
    """Read-only identity proof; absence/denied inspection never means ready."""
    import psutil
    if not owner['pid'] or owner['created'] is None:
        return False
    try:
        process = psutil.Process(owner['pid'])
        return abs(process.create_time() - owner['created']) < 0.01 and process.is_running()
    except psutil.Error:
        return False


def prepare_dispatch(coordinator, conn):
    """Existing coordinator claims attempts; dispatch outside its transaction."""
    if not available(conn):
        return []
    launches = []
    for row in conn.execute("SELECT a.*,s.class_id FROM attendance_start_attempts a JOIN attendance_sessions s ON s.id=a.session_id WHERE a.phase='starting'").fetchall():
        if not may_start(conn, row['session_id'], row['token']):
            life.interrupt(conn, row['session_id'], 'startup_timeout')
            continue
        if row['dispatch'] == 'queued':
            owner = str(uuid.uuid4())
            import psutil
            conn.execute('INSERT INTO attendance_process_owners(token,session_id,kind,pid,created) VALUES(?,?,?,?,?)',
                         (owner, row['session_id'], 'startup_dispatch', os.getpid(), psutil.Process().create_time()))
            conn.execute("UPDATE attendance_start_attempts SET dispatch='launched',dispatch_owner=? WHERE id=?", (owner, row['id']))
            launches.append({**dict(row), 'dispatch_owner': owner})
    return launches


def dispatch(coordinator, planned):
    sid = planned['session_id']
    try:
        with life.connection() as conn:
            if not may_start(conn, sid, planned['token']):
                return
        coordinator.manager.start(planned['class_id'], sid, camera_id=planned['camera_id'],
                                  overrides=json.loads(planned['overrides']), startup_attempt=planned['token'])
    except Exception:
        with life.connection() as conn:
            life.interrupt(conn, sid, 'startup_failed')
    finally:
        # The dispatcher cannot subsequently spawn anything once it has exited.
        life.confirm_process(planned['dispatch_owner'])


def launch(coordinator, planned):
    for row in planned:
        thread = threading.Thread(target=dispatch, args=(coordinator, row), daemon=True, name='attendance-startup')
        thread.start()
