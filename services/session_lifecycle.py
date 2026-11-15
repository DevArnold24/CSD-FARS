"""Durable, terminal attendance collection and bounded closure recovery.

Importing this module never starts a thread. Serving entry points explicitly
start the coordinator. All timestamps here are UTC epoch seconds; presentation
converts them to institutional time. No camera I/O occurs in a DB transaction.
"""
import contextlib
import datetime as dt
import os
import hashlib
import sqlite3
import threading
import time
import uuid

import config

BACKOFFS = (2, 5, 10, 20)
ATTEMPT_BOUND = 3.0
EPISODE_BOUND = 60.0
# An in-process fence covers a failed transaction while the coordinator is
# trying to persist an anomaly. Durable occurrence holds survive restart.
_pending_clock_holds = set()
SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_lifecycle (
 session_id INTEGER PRIMARY KEY REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 policy_version INTEGER NOT NULL DEFAULT 2 CHECK(policy_version=2),
 cutoff REAL NOT NULL, late_at REAL NOT NULL, roster_signature TEXT NOT NULL,
 collection TEXT NOT NULL DEFAULT 'collecting', version INTEGER NOT NULL DEFAULT 1,
 end_operation TEXT UNIQUE, end_actor INTEGER, end_reason TEXT, ended_at REAL,
 closed_at REAL, cleanup TEXT NOT NULL,
 episode TEXT, episode_started REAL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL, exhausted INTEGER NOT NULL DEFAULT 0, in_flight INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', interruption_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance_lifecycle_events (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 event TEXT NOT NULL, at REAL NOT NULL, actor INTEGER, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance_process_owners (
 token TEXT PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, pid INTEGER, created REAL, confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attendance_server_owner (
 id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_unfinished ON attendance_lifecycle(cutoff)
 WHERE end_operation IS NULL OR closed_at IS NULL OR cleanup='pending';
"""


def install_schema(conn):
    # Do not executescript: it commits an enclosing migration transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attendance_lifecycle'").fetchone() is not None


def lifecycle(conn, session_id):
    if not session_id or not available(conn):
        return None
    row = conn.execute('SELECT * FROM attendance_lifecycle WHERE session_id=?', (session_id,)).fetchone()
    return dict(row) if row else None


def enrich(conn, row):
    if not row:
        return row
    state = lifecycle(conn, row['id'])
    return {**dict(row), **(state or {})}


def institutional_now():
    return dt.datetime.fromtimestamp(time.time(), dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES)


def epoch(local):
    return (local - dt.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES)).replace(tzinfo=dt.timezone.utc).timestamp()


def local_sql(instant):
    return (dt.datetime.fromtimestamp(instant, dt.timezone.utc) + dt.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')


def event(conn, sid, name, actor=None, detail='', now=None):
    conn.execute('INSERT INTO attendance_lifecycle_events(session_id,event,at,actor,detail) VALUES(?,?,?,?,?)',
                 (sid, name, time.time() if now is None else now, actor, detail))


def activation_check(conn):
    if clock_unreliable(conn):
        raise ValueError('The server clock needs technical review. Resolve the affected occurrences through their owning teachers before starting new attendance.')
    if available(conn) and conn.execute("SELECT 1 FROM attendance_sessions s LEFT JOIN attendance_lifecycle l ON l.session_id=s.id WHERE s.status='Open' AND l.session_id IS NULL LIMIT 1").fetchone():
        raise ValueError('New attendance policy is awaiting activation. Finish existing legacy Open occurrences through their current workflow first.')


def clock_unreliable(conn):
    return (config.DATABASE_PATH in _pending_clock_holds or
            (available(conn) and conn.execute("SELECT 1 FROM attendance_lifecycle WHERE interruption_reason='clock_anomaly' LIMIT 1").fetchone() is not None))


def create(conn, sid, class_info, scheduled_date):
    if not available(conn):  # Small legacy test schemas only; supported servers require migration 026.
        return
    start = dt.datetime.fromisoformat(str(scheduled_date) + 'T' + str(class_info['start_time']))
    end = dt.datetime.fromisoformat(str(scheduled_date) + 'T' + str(class_info['end_time']))
    if end <= start:
        end += dt.timedelta(days=1)
    from services.attendance_sessions import late_marking_time, occurrence_roster_rows
    late = late_marking_time(start, class_info['late_threshold_minutes'])
    camera = class_info.get('camera_id') if isinstance(class_info, dict) else class_info['camera_id']
    signature = roster_signature(occurrence_roster_rows(conn, sid))
    conn.execute('INSERT INTO attendance_lifecycle(session_id,cutoff,late_at,cleanup,roster_signature) VALUES(?,?,?,?,?)',
                 (sid, epoch(end), epoch(late), 'pending' if camera else 'not_applicable', signature))
    event(conn, sid, 'startup_requested' if isinstance(class_info, dict) and class_info.get('startup_requested') else 'collection_started', class_info['teacher_id'])


def collection_allowed(state, now=None):
    return bool(state and state['collection'] == 'collecting' and not state['end_operation']
                and state.get('interruption_reason') != 'clock_anomaly'
                and config.DATABASE_PATH not in _pending_clock_holds
                and (time.time() if now is None else now) < state['cutoff'])


def guard_live_write(conn, sid, version=None):
    state = lifecycle(conn, sid)
    if not state:
        return None
    # Acquire SQLite's writer reservation BEFORE sampling the decision time.
    # A frame timestamp or a request sent before cutoff does not confer eligibility.
    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (sid,))
    state = lifecycle(conn, sid)
    now = time.time()
    if clock_unreliable(conn):
        raise ValueError('The server clock needs technical review. Live attendance is suspended.')
    if version is not None and state['version'] != version:
        raise ValueError('This attendance view is stale. Refresh before making a live attendance decision.')
    from services.session_reopening import collection_window as reopening, room_limit
    extension = reopening(conn, sid)
    if extension:
        occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (sid,)).fetchone()
        limit = min(extension['deadline'], room_limit(conn, occurrence, now))
        now = time.time()
        if now >= limit:
            raise ValueError('The reopened attendance time or room availability has ended. Use individual corrections.')
    if not collection_allowed(state, now):
        raise ValueError('Attendance collection has stopped. Use the saved report to make a reason-required correction.')
    event(conn, sid, 'live_decision_accepted', now=now)
    return state, now


def accept_end(conn, sid, *, operation=None, actor=None, version=None, reason='scheduled_cutoff', now=None):
    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (sid,))
    state = lifecycle(conn, sid)
    if not state:
        raise ValueError('Attendance lifecycle is unavailable.')
    from services.session_reopening import collection_window as reopening, end_receipt
    extension = reopening(conn, sid)
    receipt = end_receipt(conn, sid, operation)
    if receipt:
        return receipt
    # Read the existing receipt before testing a stale version. Duplicates and
    # simultaneous automatic/manual End converge on the same terminal result.
    if state['end_operation']:
        return state
    if clock_unreliable(conn):
        raise ValueError('The server clock needs technical review. Use Retry after the server operator checks the clock; End is not yet accepted.')
    if state['exhausted'] and state['last_error'] == 'intent_save_unconfirmed':
        raise ValueError('Automatic End could not be saved within its recovery budget. Use explicit Retry closure before another End attempt.')
    if version is not None and state['version'] != version:
        raise ValueError('This attendance view is stale. Refresh the current session before ending it.')
    now = time.time() if now is None else now
    cutoff = min(now, state['cutoff'])
    reason = 'scheduled_cutoff' if now >= state['cutoff'] else reason
    suffix = ('-early-resume-' + str(extension['id']) if extension and 'id' in extension else '-reopened') if extension else ''
    operation = operation or 'automatic-' + str(sid) + suffix
    if conn.execute('SELECT 1 FROM attendance_lifecycle WHERE end_operation=? AND session_id<>?', (operation, sid)).fetchone():
        raise ValueError('That End operation belongs to a different occurrence. Refresh the intended session.')
    retain_episode = state['cleanup'] == 'pending' and state['episode'] is not None
    conn.execute("""UPDATE attendance_lifecycle SET collection='stopped', version=version+1,
        cutoff=?, ended_at=?, end_operation=?, end_actor=?, end_reason=?,
        episode=?, episode_started=?, next_attempt=?, attempts=?, exhausted=?, in_flight=? WHERE session_id=?""",
        (cutoff, cutoff, operation, actor, reason,
         state['episode'] if retain_episode else str(uuid.uuid4()),
         state['episode_started'] if retain_episode else now,
         state['next_attempt'] if retain_episode else now,
         state['attempts'] if retain_episode else 0,
         state['exhausted'] if retain_episode else 0,
         state['in_flight'] if retain_episode else 0, sid))
    conn.execute('UPDATE classes SET is_active=0 WHERE id=(SELECT class_id FROM attendance_sessions WHERE id=?)', (sid,))
    # PB-004: End retires capture, while independently authorized results remain.
    from services.session_start import interrupt_attempt
    interrupt_attempt(conn, sid, 'end_accepted')
    event(conn, sid, 'end_accepted', actor, reason, now)
    return lifecycle(conn, sid)


def interrupt(conn, sid, reason='collection_interrupted'):
    state = lifecycle(conn, sid)
    if state and state['collection'] in ('collecting', 'starting'):
        allowed = {'collection_interrupted', 'camera_missing', 'camera_assignment_changed', 'camera_open_failed', 'camera_frame_failed', 'unexpected_error', 'worker_unavailable', 'clock_anomaly', 'startup_failed', 'startup_timeout', 'startup_details_changed'}
        reason = reason if reason in allowed else 'worker_unavailable'
        conn.execute("UPDATE attendance_lifecycle SET collection='interrupted',version=version+1,last_error=?,interruption_reason=? WHERE session_id=?", (reason, reason, sid))
        if state['cleanup'] == 'pending' and not state['episode']:
            now = time.time()
            conn.execute('UPDATE attendance_lifecycle SET episode=?,episode_started=?,next_attempt=? WHERE session_id=?', (str(uuid.uuid4()), now, now, sid))
        from services.session_start import interrupt_attempt
        interrupt_attempt(conn, sid, reason)
        event(conn, sid, 'collection_interrupted', detail=reason)


def resume(conn, sid, version, actor=None, *, starting=False):
    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (sid,))
    state = lifecycle(conn, sid)
    if clock_unreliable(conn):
        raise ValueError('Resume is unavailable while the server clock needs technical review.')
    if not (state and state['version'] == version and state['collection'] == 'interrupted'
            and not state['end_operation'] and time.time() < state['cutoff']
            and state['cleanup'] in ('confirmed', 'not_applicable')):
        raise ValueError('Resume is unavailable. Refresh session status; End is terminal and cleanup must be confirmed before cutoff.')
    conn.execute("UPDATE attendance_lifecycle SET collection='collecting',version=version+1,episode=NULL,episode_started=NULL,attempts=0,next_attempt=NULL,in_flight=0,exhausted=0,last_error='' WHERE session_id=?", (sid,))
    event(conn, sid, 'startup_resume_requested' if starting else 'collection_resumed', actor)


@contextlib.contextmanager
def connection():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=0.15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def reserve_process(sid, kind):
    """Commit an unknown-owner reservation BEFORE spawning, including helpers."""
    if not sid:
        return None
    with connection() as conn:
        if not lifecycle(conn, sid):
            return None
        token = str(uuid.uuid4())
        conn.execute('INSERT INTO attendance_process_owners(token,session_id,kind) VALUES(?,?,?)', (token, sid, kind))
        conn.execute("UPDATE attendance_lifecycle SET cleanup='pending' WHERE session_id=?", (sid,))
        return token


def identify_process(token, pid):
    if token:
        import psutil
        created = psutil.Process(pid).create_time()
        with connection() as conn:
            conn.execute('UPDATE attendance_process_owners SET pid=?,created=? WHERE token=?', (pid, created, token))


def confirm_process(token):
    if token:
        with connection() as conn:
            conn.execute('UPDATE attendance_process_owners SET confirmed=1 WHERE token=?', (token,))


def owners_confirmed(conn, sid):
    import psutil
    for owner in conn.execute('SELECT * FROM attendance_process_owners WHERE session_id=? AND confirmed=0', (sid,)):
        if not owner['pid'] or owner['created'] is None:
            return False  # Spawn outcome unknown: absence from memory proves nothing.
        try:
            process = psutil.Process(owner['pid'])
            if abs(process.create_time() - owner['created']) < 0.01 and process.is_running():
                return False
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            return False
        conn.execute('UPDATE attendance_process_owners SET confirmed=1 WHERE token=?', (owner['token'],))
    return True


def stop_orphan_owners(sid):
    """Request termination only for exact persisted OS identities, never wait.

    A missing PID remains unknown. Never terminate this serving process (a
    thread-mode reservation uses its PID). Readback on a later attempt must
    confirm every owner exited before the camera can be released.
    """
    import psutil
    with connection() as conn:
        owners = [dict(row) for row in conn.execute('SELECT * FROM attendance_process_owners WHERE session_id=? AND confirmed=0', (sid,))]
    for owner in owners:
        if not owner['pid'] or owner['created'] is None or owner['pid'] == os.getpid():
            continue
        try:
            process = psutil.Process(owner['pid'])
            if abs(process.create_time() - owner['created']) < 0.01:
                process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            continue  # identity/liveness still unconfirmed, reservation retained


def reserved_camera(camera_id, exclude_session=None):
    if camera_id is None:
        return None
    with connection() as conn:
        if not available(conn):
            return None
        row = conn.execute("SELECT s.class_id FROM attendance_sessions s JOIN attendance_lifecycle l ON l.session_id=s.id WHERE s.camera_id=? AND l.cleanup='pending' AND (? IS NULL OR s.id<>?) LIMIT 1", (camera_id, exclude_session, exclude_session)).fetchone()
        return row[0] if row else None


def retry(conn, sid, actor, version, *, clock_review_confirmed=False, clock_review_reason=''):
    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (sid,))
    state = lifecycle(conn, sid)
    clock_hold = bool(state and state['interruption_reason'] == 'clock_anomaly')
    if not state or not (state['exhausted'] or clock_hold) or state['version'] != version:
        raise ValueError('Retry is unavailable or this view is stale. Refresh session status.')
    if clock_hold:
        if clock_review_confirmed is not True or not isinstance(clock_review_reason, str) or not 1 <= len(clock_review_reason.strip()) <= 500:
            raise ValueError('Ask the server operator to check and correct the clock first. Confirm that review and enter a reason (1–500 characters) before Retry.')
        # This is an owning teacher's recorded confirmation of external technical
        # review, not a new role power to alter the OS clock or a trusted-time proof.
        conn.execute("UPDATE attendance_lifecycle SET interruption_reason='clock_reviewed' WHERE session_id=?", (sid,))
        # This detail is also exported to Excel. Keep the user-supplied note
        # after a literal prefix so it cannot become a spreadsheet formula.
        event(conn, sid, 'clock_review_confirmed', actor, 'Technical clock review: ' + clock_review_reason.strip())
    now = time.time()
    conn.execute("""UPDATE attendance_lifecycle SET episode=?,episode_started=?,attempts=0,
        exhausted=0,in_flight=0,next_attempt=?,last_error='',version=version+1 WHERE session_id=?""",
        (str(uuid.uuid4()), now, now, sid))
    event(conn, sid, 'recovery_retry_requested', actor)
    # Never restart collection here. A pre-cutoff occurrence still requires
    # explicit Resume; an overdue one is handled by the ordinary cutoff sweep.


def roster_signature(roster):
    return hashlib.sha256(','.join(str(student['id']) for student in sorted(roster, key=lambda item: item['id'])).encode('ascii')).hexdigest()


def snapshot(conn, sid):
    state = lifecycle(conn, sid)
    if not state:
        return None
    from services.attendance_sessions import occurrence_roster_rows
    roster = occurrence_roster_rows(conn, sid)
    consistent = roster_signature(roster) == state['roster_signature']
    marks = {r['student_id']: r['status'] for r in conn.execute('SELECT student_id,status FROM attendance_records WHERE attendance_session_id=?', (sid,))}
    counts = {status: 0 for status in ('Present', 'Late', 'Absent', 'Excused', 'Pending')}
    for student in roster:
        counts[marks.get(student['id'], 'Pending')] += 1
    active = collection_allowed(state)
    cleanup = state['cleanup']
    clock_hold = state['interruption_reason'] == 'clock_anomaly'
    unreliable = clock_unreliable(conn)
    closed = state['closed_at'] is not None
    pending = counts['Pending']
    if clock_hold:
        message = 'Attendance suspended: the server clock changed unexpectedly. Confirmed marks and existing End receipts are preserved. Ask the server operator to check and correct the clock, then record that review with Retry. No collection or Resume is permitted before review; the original cutoff is unchanged.'
    elif state['exhausted']:
        message = 'Automatic recovery needs attention. Review the saved status, then explicitly Retry closure. If camera cleanup remains unconfirmed, keep the camera reserved and ask the server operator to inspect its worker processes.'
        explanation = {'cleanup_unconfirmed':'Worker cleanup could not be confirmed.', 'save_unconfirmed':'The save or its readback could not be confirmed.', 'intent_save_unconfirmed':'Automatic End could not be saved within its recovery budget.', 'operator_required':'Storage validation failed; ask the server operator to investigate before retrying.', 'restart_requires_retry':'Restart interrupted the recovery episode; its remaining budget could not be established.', 'recovery_bound_reached':'The attempt or episode time limit was reached.'}.get(state['last_error'], '')
        if explanation:
            message += ' ' + explanation
    elif closed and cleanup == 'pending':
        message = 'Attendance saved; camera cleanup pending. The camera remains reserved.'
    elif closed:
        message = 'Ended; no enrolled students.' if not roster else ('Ended and saved. ' + (f"{pending} {'student needs' if pending == 1 else 'students need'} review." if pending else 'All students have an explicit result.'))
    elif state['collection'] == 'starting':
        message = 'Starting camera attendance. Recording is not yet enabled; the original cutoff is unchanged. You may End this occurrence.'
    elif not active and state['collection'] != 'interrupted':
        message = 'Collection stopped. Saving session closure; success is not yet confirmed.'
    elif state['collection'] == 'interrupted':
        explanation = {'camera_missing':'The assigned camera is unavailable.', 'camera_assignment_changed':'The camera assignment changed.', 'camera_open_failed':'The camera could not be opened.', 'camera_frame_failed':'The camera stopped returning frames.', 'unexpected_error':'The camera pipeline stopped after an error.', 'clock_anomaly':'The server clock changed unexpectedly; ask the server operator to check it.', 'startup_failed':'Camera startup could not be confirmed.', 'startup_timeout':'Camera startup exceeded its 30-second allowance.', 'startup_details_changed':'Camera or recognition details changed during startup.'}.get(state['interruption_reason'], '')
        message = 'Attendance interrupted. ' + explanation + ' Confirmed marks are preserved. Resume is available only before cutoff after confirmed camera cleanup.'
    else:
        message = 'Attendance collection is open until the scheduled cutoff.'
    if not consistent:
        message = 'Historical roster consistency needs review. Confirmed attendance is preserved; completeness cannot be verified. Ask the server operator to investigate the occurrence history.'
    can_resume = bool(not unreliable and state['collection'] == 'interrupted' and not state['end_operation'] and time.time() < state['cutoff'] and cleanup != 'pending')
    last_attempt = conn.execute("SELECT MAX(at) FROM attendance_lifecycle_events WHERE session_id=? AND event='recovery_attempt'", (sid,)).fetchone()[0]
    from services.session_start import public as startup_public
    startup = startup_public(conn, sid)
    from services.session_reopening import get as reopening, early_resume, eligibility
    extension = reopening(conn, sid)
    resumed = early_resume(conn, sid)
    occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (sid,)).fetchone()
    reopen_available, reopen_message, early_available, early_message = False, '', False, ''
    if not extension and state['end_operation']:
        try:
            early_facts = eligibility(conn, occurrence, state, early=True)
            early_available = True
            early_message = f"Resume camera attendance until {local_sql(early_facts['limit'])}. The original scheduled end and late threshold stay unchanged. Only Pending students can receive new camera results. An explanation is required."
        except ValueError as exc:
            early_message = str(exc)
        try:
            facts = eligibility(conn, occurrence, state)
            reopen_available = True
            reopen_message = f"One camera reopening is available for up to 10 minutes, ending no later than {local_sql(facts['limit'])}. Only Pending students can receive new camera results. An explanation is required."
        except ValueError as exc:
            reopen_message = str(exc)
    if extension:
        message = ('Reopened camera attendance. Only Pending students may receive new camera results. ' if not closed else 'This session used a camera reopening. ') + message.replace('original cutoff', 'reopening deadline').replace('scheduled cutoff', 'reopening deadline')
    elif resumed and active:
        message = 'Camera attendance resumed until the original scheduled end. Only Pending students can receive new camera results.'
    return {**state, 'startup': startup,
            'early_resume': ({'accepted_at': resumed['accepted_at'], 'deadline': resumed['deadline'],
                              'reason': resumed['reason'], 'actor': resumed['actor']} if resumed else None),
            'resume_ended_available': early_available, 'resume_ended_message': early_message,
            'reopening': ({'accepted_at': extension['accepted_at'], 'deadline': extension['deadline'],
                          'original_cutoff': extension['original_cutoff'], 'reason': extension['reason'], 'actor': extension['actor']} if extension else None),
            'reopen_available': reopen_available, 'reopen_message': reopen_message, 'counts': counts, 'total': len(roster), 'review_required': bool(pending) or not consistent, 'roster_consistent': consistent,
            'cleanup_label': {'pending':'Unconfirmed; camera remains reserved', 'confirmed':'Confirmed', 'not_applicable':'No camera used'}[cleanup],
            'reason_label': {'teacher_end':'Ended by teacher', 'scheduled_cutoff':'Scheduled cutoff reached'}.get(state['end_reason'], state['end_reason'] or 'Collection unfinished'),
            'cutoff_label': local_sql(state['cutoff']) + ' (institutional time)',
            'closed_label': local_sql(state['closed_at']) + ' (institutional time)' if closed else 'Not confirmed',
            'last_attempt_label': local_sql(last_attempt) + ' (institutional time)' if last_attempt is not None else 'No confirmed attempt record',
            'closure_confirmed': closed, 'collection_allowed': active, 'message': message,
            'resume_available': can_resume, 'end_available': not unreliable and not bool(state['end_operation']) and time.time() < state['cutoff'],
            'clock_review_required': clock_hold,
            'retry_available': bool(state['exhausted']) or clock_hold, 'report_session_id': sid}


class Coordinator:
    """One serving-process coordinator; independent per-session bounded attempts."""
    def __init__(self, manager):
        self.manager = manager
        self.stop_event = threading.Event()
        self.thread = None
        self.jobs = {}
        self.deadlines = {}
        self.budgets = {}
        self.last_clock = (time.time(), time.monotonic())
        self.clock_untrusted = False
        self.cached_rows = []
        self.intent_budgets = {}

    def startup(self):
        with connection() as conn:
            if not available(conn):
                raise RuntimeError('Attendance lifecycle migration 026 is required before serving.')
            # Persisted in-flight work/budgets cannot be reconstructed from a
            # new monotonic clock. Require an audited retry; never reset a scan.
            conn.execute("UPDATE attendance_lifecycle SET exhausted=1,in_flight=0,last_error='restart_requires_retry' WHERE episode IS NOT NULL AND ((end_operation IS NOT NULL AND closed_at IS NULL) OR cleanup='pending')")
            rows = conn.execute("SELECT l.*,s.class_id FROM attendance_lifecycle l JOIN attendance_sessions s ON s.id=l.session_id WHERE l.collection IN ('collecting','starting')").fetchall()
            for row in rows:
                if time.time() >= row['cutoff'] and not row['exhausted'] and not clock_unreliable(conn):
                    accept_end(conn, row['session_id'])
                elif time.time() < row['cutoff']:
                    interrupt(conn, row['session_id'])
                    conn.execute('UPDATE classes SET is_active=0 WHERE id=?', (row['class_id'],))
        self.sweep()

    def attempt(self, sid, planned):
        started = time.monotonic()
        episode = planned['episode']
        key = (sid, episode)
        previous, _ = self.budgets.get(key, (planned['attempts'], 0))
        attempt = max(previous, planned['attempts']) + 1
        if attempt > 5:
            return
        self.budgets[key] = (attempt, started + BACKOFFS[min(attempt-1, 3)])
        try:
            with connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT l.*,s.class_id,s.camera_id FROM attendance_lifecycle l JOIN attendance_sessions s ON s.id=l.session_id WHERE l.session_id=?', (sid,)).fetchone()
                if not row or row['episode'] != episode or row['exhausted'] or row['in_flight'] or row['attempts'] >= 5:
                    return
                conn.execute('UPDATE attendance_lifecycle SET attempts=?,in_flight=1 WHERE session_id=?', (attempt, sid))
                event(conn, sid, 'recovery_attempt', detail=str(attempt))
            # Nonblocking worker cleanup: no native joins in a DB transaction,
            # and one slow session cannot block another session's cutoff sweep.
            clean = row['cleanup'] != 'pending'
            if not clean:
                clean = self.manager.poll_stop(row['class_id'], sid)
                if clean:
                    stop_orphan_owners(sid)
            with connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = lifecycle(conn, sid)
                if current['episode'] != episode:
                    return
                if time.monotonic() - started >= ATTEMPT_BOUND or time.time() - current['episode_started'] >= EPISODE_BOUND:
                    conn.execute("UPDATE attendance_lifecycle SET exhausted=1,in_flight=0,last_error='recovery_bound_reached' WHERE session_id=?", (sid,))
                    return
                for token in tuple(getattr(self.manager, 'confirmed_lifecycle_tokens', ())):
                    conn.execute('UPDATE attendance_process_owners SET confirmed=1 WHERE token=?', (token,))
                if clean and owners_confirmed(conn, sid):
                    conn.execute("UPDATE attendance_lifecycle SET cleanup='confirmed' WHERE session_id=? AND cleanup='pending'", (sid,))
                if current['end_operation'] and not current['closed_at'] and current['interruption_reason'] != 'clock_anomaly':
                    conn.execute("UPDATE attendance_sessions SET status='Closed',actual_ended_at=?,close_reason=? WHERE id=?", (local_sql(current['cutoff']), current['end_reason'], sid))
                    conn.execute('UPDATE attendance_lifecycle SET closed_at=? WHERE session_id=?', (time.time(), sid))
                    event(conn, sid, 'closure_saved')
            # Separate committed readback; no response may assume a commit.
            with connection() as conn:
                current = lifecycle(conn, sid)
                done = (current['interruption_reason'] == 'clock_anomaly' or current['closed_at'] is not None or not current['end_operation']) and current['cleanup'] != 'pending'
                exhausted = not done and (attempt >= 5 or time.monotonic() - started >= ATTEMPT_BOUND or time.time() - current['episode_started'] >= EPISODE_BOUND)
                conn.execute('UPDATE attendance_lifecycle SET in_flight=0,exhausted=?,next_attempt=?,last_error=? WHERE session_id=? AND episode=?',
                             (int(exhausted), time.time() + BACKOFFS[min(attempt-1, 3)], '' if done else 'cleanup_unconfirmed', sid, episode))
        except Exception as error:
            # Do not expose connection details or exception text to UI/logs.
            try:
                with connection() as conn:
                    row = lifecycle(conn, sid)
                    if row and row['episode'] == episode:
                        permanent = not isinstance(error, (sqlite3.OperationalError, OSError))
                        if isinstance(error, sqlite3.OperationalError):
                            permanent = not any(term in str(error).lower() for term in ('locked', 'busy', 'disk i/o', 'disk is full'))
                        exhausted = permanent or attempt >= 5 or time.monotonic() - started >= ATTEMPT_BOUND or time.time() - row['episode_started'] >= EPISODE_BOUND
                        conn.execute("UPDATE attendance_lifecycle SET attempts=?,in_flight=0,exhausted=?,next_attempt=?,last_error=? WHERE session_id=?", (attempt, int(exhausted), time.time()+BACKOFFS[min(attempt-1,3)], 'operator_required' if permanent else 'save_unconfirmed', sid))
            except sqlite3.Error:
                pass  # sweep observes the monotonic ceiling; never blind replay

    def accept_due(self, planned):
        """Bound failed automatic intent saves before a receipt can commit.

        Read-only discovery continues during storage failure. On recovery,
        persist exhaustion before any further closure effect. Failed intent
        attempts consume the same episode budget as finalization.
        """
        sid, now, monotonic = planned['session_id'], time.time(), time.monotonic()
        key = (sid, planned['version'])
        budget = self.intent_budgets.setdefault(key, {
            'failures': planned['attempts'] if planned['episode'] else 0,
            'started': planned['episode_started'] or now,
            'deadline': monotonic + max(0, EPISODE_BOUND - (now-(planned['episode_started'] or now))),
            'next': monotonic,
        })
        if monotonic < budget['next']:
            return
        exhausted = budget['failures'] >= 5 or monotonic >= budget['deadline'] or budget.get('permanent', False)
        try:
            with connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = lifecycle(conn, sid)
                if not current or clock_unreliable(conn) or current['end_operation'] or current['exhausted'] or current['version'] != planned['version']:
                    return
                if exhausted:
                    conn.execute("UPDATE attendance_lifecycle SET collection='stopped',exhausted=1,attempts=?,episode=COALESCE(episode,?),episode_started=?,last_error='intent_save_unconfirmed' WHERE session_id=?",
                        (budget['failures'], str(uuid.uuid4()), budget['started'], sid))
                    event(conn, sid, 'automatic_intent_exhausted', detail='Explicit Retry closure required')
                    return
                state = accept_end(conn, sid)
                conn.execute('UPDATE attendance_lifecycle SET attempts=?,episode_started=? WHERE session_id=?',
                    (max(state['attempts'], budget['failures']), budget['started'], sid))
                self.deadlines[(sid, state['episode'])] = budget['deadline']
        except sqlite3.Error as error:
            if not exhausted:
                transient = isinstance(error, sqlite3.OperationalError) and any(term in str(error).lower() for term in ('locked','busy','disk i/o','disk is full'))
                budget['failures'] += 1
                budget['permanent'] = not transient
                budget['next'] = monotonic if budget['failures'] >= 5 or not transient else monotonic + BACKOFFS[min(budget['failures']-1, 3)]
            # No durable receipt exists: never advertise accepted End.

    def sweep(self):
        now, monotonic = time.time(), time.monotonic()
        anomalous = now - self.last_clock[0] < -1 or abs((now-self.last_clock[0]) - (monotonic-self.last_clock[1])) > 5
        self.clock_untrusted = self.clock_untrusted or anomalous
        self.last_clock = (now, monotonic)
        if self.clock_untrusted:
            _pending_clock_holds.add(config.DATABASE_PATH)
            # Persist the suspension BEFORE any apparently overdue End. Include
            # interrupted work and accepted-but-unfinished End receipts; never
            # rewrite attendance or the receipt's cutoff/reason/operation.
            with connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                rows = conn.execute("SELECT * FROM attendance_lifecycle WHERE end_operation IS NULL OR closed_at IS NULL OR cleanup='pending'").fetchall()
                for row in rows:
                    if row['interruption_reason'] == 'clock_anomaly':
                        continue
                    sid = row['session_id']
                    interrupt(conn, sid, 'clock_anomaly')
                    conn.execute("UPDATE attendance_lifecycle SET collection=CASE WHEN end_operation IS NULL THEN 'interrupted' ELSE 'stopped' END, interruption_reason='clock_anomaly',version=version+1 WHERE session_id=?", (sid,))
                    conn.execute('UPDATE classes SET is_active=0 WHERE id=(SELECT class_id FROM attendance_sessions WHERE id=?)', (sid,))
                    # Results authorization does not permit capture during a clock hold.
                    event(conn, sid, 'clock_anomaly', detail='Wall time diverged from monotonic elapsed time; technical review required')
            _pending_clock_holds.discard(config.DATABASE_PATH)
            self.clock_untrusted = False  # only the persistence fence; durable holds remain
        try:
            with connection() as conn:
                from services.session_reopening import collection_window as reopening, room_limit
                for active in conn.execute("SELECT l.session_id,l.cutoff FROM attendance_lifecycle l WHERE l.end_operation IS NULL").fetchall():
                    if reopening(conn, active['session_id']):
                        occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (active['session_id'],)).fetchone()
                        try:
                            limit = room_limit(conn, occurrence, now)
                        except ValueError:
                            interrupt(conn, active['session_id'], 'startup_details_changed')
                            continue
                        if limit < active['cutoff']:
                            conn.execute('UPDATE attendance_lifecycle SET cutoff=?,version=version+1 WHERE session_id=?', (limit, active['session_id']))
                self.cached_rows = [dict(row) for row in conn.execute("SELECT * FROM attendance_lifecycle WHERE end_operation IS NULL AND exhausted=0")]
        except sqlite3.Error:
            pass  # Previously observed frozen cutoffs still constrain attempts.
        for planned in self.cached_rows:
            if now >= planned['cutoff']:
                self.accept_due(planned)
        with connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT l.*,s.class_id FROM attendance_lifecycle l JOIN attendance_sessions s ON s.id=l.session_id WHERE l.end_operation IS NULL OR l.closed_at IS NULL OR l.cleanup='pending'").fetchall()
            for row in rows:
                sid = row['session_id']
                for token in tuple(getattr(self.manager, 'confirmed_lifecycle_tokens', ())):
                    conn.execute('UPDATE attendance_process_owners SET confirmed=1 WHERE token=?', (token,))
                if row['collection'] == 'interrupted' and row['cleanup'] == 'pending' and owners_confirmed(conn, sid):
                    conn.execute("UPDATE attendance_lifecycle SET cleanup='confirmed' WHERE session_id=?", (sid,))
            from services.session_start import prepare_dispatch
            start_launches = prepare_dispatch(self, conn)
            pending = conn.execute("SELECT * FROM attendance_lifecycle WHERE episode IS NOT NULL AND ((end_operation IS NOT NULL AND closed_at IS NULL AND interruption_reason<>'clock_anomaly') OR cleanup='pending') AND exhausted=0").fetchall()
            launch = []
            for row in pending:
                sid = row['session_id']
                key = (sid, row['episode'])
                used, next_due = self.budgets.get(key, (row['attempts'], 0))
                running = sid in self.jobs and self.jobs[sid][0].is_alive()
                deadline = self.deadlines.setdefault(key, monotonic + max(0, EPISODE_BOUND - (now-row['episode_started'])))
                over_attempt = sid in self.jobs and row['in_flight'] and monotonic-self.jobs[sid][1] >= ATTEMPT_BOUND
                if monotonic >= deadline or (used >= 5 and not running) or over_attempt:
                    conn.execute("UPDATE attendance_lifecycle SET exhausted=1,last_error='recovery_bound_reached' WHERE session_id=?", (sid,))
                elif not row['in_flight'] and now >= row['next_attempt'] and monotonic >= next_due and not (sid in self.jobs and self.jobs[sid][0].is_alive()):
                    launch.append(dict(row))
        from services.session_start import launch as launch_startups
        launch_startups(self, start_launches)
        for planned in launch:
            sid = planned['session_id']
            thread = threading.Thread(target=self.attempt, args=(sid, planned), daemon=True, name='attendance-closure')
            self.jobs[sid] = (thread, time.monotonic())
            thread.start()

    def run(self):
        while not self.stop_event.wait(0.5):
            try:
                self.sweep()
            except sqlite3.Error:
                continue  # independent write guards still enforce frozen cutoff


_coordinator = None
_start_lock = threading.Lock()


def stop_coordinator():
    """Stop scheduling on normal process exit; retain all durable camera claims."""
    if _coordinator is not None:
        _coordinator.stop_event.set()
        if _coordinator.thread is not None:
            _coordinator.thread.join(timeout=1.0)


def start_coordinator():
    global _coordinator
    with _start_lock:
        if _coordinator is None:
            import psutil
            pid, created = os.getpid(), psutil.Process(os.getpid()).create_time()
            with connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                owner = conn.execute('SELECT * FROM attendance_server_owner WHERE id=1').fetchone()
                if owner and owner['pid'] != pid:
                    try:
                        if abs(psutil.Process(owner['pid']).create_time()-owner['created']) < 0.01:
                            raise RuntimeError('Another attendance server owns lifecycle coordination. Stop it normally before starting this server.')
                    except psutil.NoSuchProcess:
                        pass
                conn.execute('INSERT OR REPLACE INTO attendance_server_owner(id,pid,created) VALUES(1,?,?)', (pid, created))
            from workers.session_manager import session_manager
            coordinator = Coordinator(session_manager)
            coordinator.startup()  # reconcile before accepting HTTP attendance actions
            coordinator.thread = threading.Thread(target=coordinator.run, daemon=True, name='attendance-lifecycle')
            coordinator.thread.start()
            _coordinator = coordinator
            import atexit
            atexit.register(stop_coordinator)
        return _coordinator
