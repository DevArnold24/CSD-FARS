"""Audited early camera continuations and one exceptional camera extension.

No worker or coordinator of its own. The original End receipt is immutable;
the existing lifecycle supervises the explicitly accepted extension.
"""
import datetime as dt
import json

from services import session_lifecycle as life


def install_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS attendance_reopening (
        session_id INTEGER PRIMARY KEY REFERENCES attendance_sessions(id) ON DELETE CASCADE,
        actor INTEGER NOT NULL, reason TEXT NOT NULL, accepted_at REAL NOT NULL,
        deadline REAL NOT NULL, original_cutoff REAL NOT NULL,
        original_end_operation TEXT NOT NULL UNIQUE, original_receipt TEXT NOT NULL)''')


def get(conn, sid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_reopening'").fetchone():
        return None
    row = conn.execute('SELECT * FROM attendance_reopening WHERE session_id=?', (sid,)).fetchone()
    return dict(row) if row else None


def install_resume_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS attendance_early_resumes (
        id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
        actor INTEGER NOT NULL, reason TEXT NOT NULL, accepted_at REAL NOT NULL,
        deadline REAL NOT NULL, original_cutoff REAL NOT NULL,
        original_end_operation TEXT NOT NULL UNIQUE, original_receipt TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_early_resumes_session ON attendance_early_resumes(session_id,id)')


def early_resume(conn, sid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_early_resumes'").fetchone():
        return None
    row = conn.execute('SELECT * FROM attendance_early_resumes WHERE session_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
    return dict(row) if row else None


def collection_window(conn, sid):
    """The exceptional extension, if used, supersedes earlier on-time resumes."""
    return get(conn, sid) or early_resume(conn, sid)


def end_receipt(conn, sid, operation):
    if not operation:
        return None
    for table in ('attendance_reopening', 'attendance_early_resumes'):
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            continue
        row = conn.execute(f'SELECT session_id,original_receipt FROM {table} WHERE original_end_operation=?', (operation,)).fetchone()
        if row:
            if row['session_id'] != sid:
                raise ValueError('That End operation belongs to a different occurrence.')
            return json.loads(row['original_receipt'])
    return None


def room_limit(conn, occurrence, now):
    """Stop before another class's opening, including an already occupied slot.

    Effective-dated schedules and frozen accepted opening times take precedence.
    Invalid policy or schedule fails closed; no guessed free-room interval.
    """
    from services.attendance_settings import load
    from services.class_scheduling import schedule_period_for_date
    from services.attendance_sessions import normalized_day_tokens
    minutes = load(conn)['minutes']
    info = conn.execute('SELECT room_id FROM classes WHERE id=?', (occurrence['class_id'],)).fetchone()
    if not info or not info['room_id']:
        raise ValueError('The room assignment must be confirmed before reopening.')
    earliest = float('inf')
    local = dt.datetime.fromisoformat(life.local_sql(now))
    for other in conn.execute('SELECT * FROM classes WHERE room_id=? AND id<>? AND is_archived=0', (info['room_id'], occurrence['class_id'])):
        for delta in (-1, 0, 1):
            date = local.date() + dt.timedelta(days=delta)
            period = schedule_period_for_date(conn, other['id'], date)
            schedule = period or other
            if date.strftime('%a') not in normalized_day_tokens(schedule['days_of_week']):
                continue
            start = dt.datetime.fromisoformat(f"{date}T{schedule['start_time']}")
            end = dt.datetime.fromisoformat(f"{date}T{schedule['end_time']}")
            if end <= start:
                end += dt.timedelta(days=1)
            opening = life.epoch(start) - minutes * 60
            frozen = conn.execute('''SELECT o.opens_at FROM attendance_sessions s
                JOIN attendance_opening_snapshots o ON o.session_id=s.id
                WHERE s.class_id=? AND s.scheduled_date=? AND s.scheduled_start_time=?''',
                (other['id'], str(date), schedule['start_time'])).fetchone()
            if frozen:
                opening = frozen[0]
            if life.epoch(end) > now:
                earliest = min(earliest, opening)
    return earliest


def eligibility(conn, occurrence, state, *, early=False):
    if not occurrence or not state:
        raise ValueError('No saved attendance session is available to reopen.')
    sid, now = occurrence['id'], life.time.time()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_reopening'").fetchone():
        raise ValueError('Reopening is unavailable until the installation supports it.')
    if get(conn, sid):
        raise ValueError('This session has already used its one reopening. Use individual corrections for remaining results.')
    if early and not conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_early_resumes'").fetchone():
        raise ValueError('Resume until scheduled end requires installation migration 032.')
    from services.session_start import current, camera_for
    if conn.execute("SELECT 1 FROM attendance_sessions WHERE class_id=? AND id<>? AND status='Open'", (occurrence['class_id'], sid)).fetchone():
        raise ValueError('Another attendance session is open for this class. It must not be interrupted.')
    attempt = current(conn, sid)
    if not attempt or attempt['mode'] != 'camera' or not occurrence['camera_id']:
        raise ValueError('Only a camera attendance session can be reopened.')
    info = conn.execute('SELECT * FROM classes WHERE id=?', (occurrence['class_id'],)).fetchone()
    camera = camera_for(conn, info['room_id'])
    if not camera or camera['id'] != occurrence['camera_id']:
        raise ValueError('The original camera assignment has changed. Use individual corrections.')
    if life.clock_unreliable(conn):
        raise ValueError('The server clock needs technical review before reopening.')
    if not state['end_operation'] or state['closed_at'] is None or state['cleanup'] != 'confirmed':
        raise ValueError('Session ending and the camera stop must be confirmed before reopening.')
    if conn.execute('SELECT 1 FROM attendance_process_owners WHERE session_id=? AND confirmed=0', (sid,)).fetchone():
        raise ValueError('Camera ownership cleanup is not confirmed.')
    start = dt.datetime.fromisoformat(f"{occurrence['scheduled_date']}T{occurrence['scheduled_start_time']}")
    end = dt.datetime.fromisoformat(f"{occurrence['scheduled_date']}T{occurrence['scheduled_end_time']}")
    if end <= start:
        end += dt.timedelta(days=1)
    cutoff = life.epoch(end)
    if early and (now >= cutoff or now < state['closed_at']):
        raise ValueError('The scheduled end has been reached. Resume cannot add attendance time.')
    if not early and (not 0 <= now - state['closed_at'] <= 900 or now > cutoff + 900):
        raise ValueError('The reopening window has passed. Use individual attendance corrections.')
    from services.attendance_sessions import occurrence_roster_rows
    roster = occurrence_roster_rows(conn, sid)
    if life.roster_signature(roster) != state['roster_signature']:
        raise ValueError('The original student list needs review before reopening.')
    marked = {r[0] for r in conn.execute('SELECT student_id FROM attendance_records WHERE attendance_session_id=?', (sid,))}
    pending = sum(s['id'] not in marked for s in roster)
    if not pending:
        raise ValueError('Every student has a recorded result. Use corrections to change existing results.')
    limit = min(cutoff if early else cutoff + 900, room_limit(conn, occurrence, now))
    if early and now >= limit:
        raise ValueError('The original attendance time or room availability has ended.')
    if not early and limit - now < 60:
        raise ValueError('Less than one minute is available before the room or attendance limit. Use individual corrections.')
    return {'limit': limit, 'original_cutoff': cutoff, 'pending': pending,
            'original_end': state['ended_at'], 'confirmed_end': state['closed_at']}


def accept(conn, occurrence, state, actor, reason, *, early=False):
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError('Explain why camera attendance must reopen (1 to 500 characters).')
    owner = conn.execute('SELECT teacher_id,is_archived FROM classes WHERE id=?', (occurrence['class_id'],)).fetchone()
    user = conn.execute('SELECT role,is_active FROM users WHERE id=?', (actor,)).fetchone()
    if not owner or owner['teacher_id'] != actor or owner['is_archived'] or not user or user['role'] != 'Teacher' or not user['is_active']:
        raise ValueError('Only the current owning teacher may continue this attendance session.')
    facts = eligibility(conn, occurrence, state, early=early)
    now, sid = life.time.time(), occurrence['id']
    if (early and now >= facts['limit']) or (not early and (facts['limit'] - now < 60 or now - state['closed_at'] > 900)):
        raise ValueError('The reopening window is no longer available. Review the saved attendance instead.')
    deadline = facts['limit'] if early else min(now + 600, facts['limit'])
    table = 'attendance_early_resumes' if early else 'attendance_reopening'
    conn.execute(f'''INSERT INTO {table}
        (session_id,actor,reason,accepted_at,deadline,original_cutoff,original_end_operation,original_receipt)
        VALUES(?,?,?,?,?,?,?,?)''',
                 (sid, actor, reason.strip(), now, deadline, facts['original_cutoff'],
                  state['end_operation'], json.dumps(state, sort_keys=True)))
    conn.execute('''UPDATE attendance_lifecycle SET cutoff=?,collection='starting',
        end_operation=NULL,end_actor=NULL,end_reason=NULL,ended_at=NULL,closed_at=NULL,
        episode=NULL,episode_started=NULL,attempts=0,next_attempt=NULL,exhausted=0,
        in_flight=0,last_error='',interruption_reason='',version=version+1 WHERE session_id=?''', (deadline, sid))
    conn.execute("UPDATE attendance_sessions SET status='Open',actual_ended_at=NULL,close_reason='' WHERE id=?", (sid,))
    life.event(conn, sid, 'early_camera_resume_accepted' if early else 'camera_reopening_accepted', actor,
               ('Resume until scheduled end: ' if early else 'Reopening reason: ') + reason.strip() + '; ends by ' + life.local_sql(deadline))
    return sid
