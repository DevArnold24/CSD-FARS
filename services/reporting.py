"""One read-only report dataset for the browser and spreadsheet export."""
import json
from urllib.parse import urlsplit
import config
from core.utils import (
    _current_attendance_status_expr, _institution_local_date_expr,
    _institution_local_time_expr, _institution_local_datetime_expr,
)
from services.attendance_sessions import enrollment_period_contains_sql, attendance_audit_kind_sql

STATUSES = ('Present', 'Late', 'Absent', 'Excused', 'Pending')
ORDER_BY = 'session_date DESC, course_name COLLATE NOCASE, class_id, student_name COLLATE NOCASE, student_pk, COALESCE(record_id, -session_id)'
DETAILS_NOTE = 'Attendance dates, statuses and recorded times belong to the saved session. Student, program, year level and class details reflect current records.'
DETAIL_FILTERS = ('student_id', 'q', 'status', 'program_id', 'year_level', 'review')


def context_filters(filters):
    """A detail selection must not manufacture a whole-class attendance rate."""
    return {**filters, **dict.fromkeys(DETAIL_FILTERS, '')}


def safe_report_return(value):
    """Only return to the internal report; never turn filter navigation into a redirect."""
    value = str(value or '')
    try:
        parsed = urlsplit(value)
        if (len(value) <= 4000 and not any(ord(char) < 32 for char in value)
                and not parsed.scheme and not parsed.netloc and parsed.path == '/reports'):
            return value
    except ValueError:
        pass
    return None


def report_query(filters, user_id, role):
    """Return a scoped SQL dataset. Pending rows are derived, never written."""
    scope = 'c.teacher_id = :viewer' if role == 'Teacher' else '1 = 1'
    params = {'viewer': user_id}
    clauses = []
    for key, expression in [('class_id', 'class_id'), ('student_id', 'student_public_id'), ('status', 'status'),
                            ('teacher_id', 'teacher_id'), ('section', 'section'), ('session_id', 'session_id'),
                            ('program_id', 'program_id'), ('year_level', 'year_level')]:
        if filters.get(key):
            clauses.append(f'{expression} = :{key}')
            params[key] = filters[key]
    if filters.get('archive') in ('active', 'archived'):
        clauses.append('is_archived = :archived')
        params['archived'] = int(filters['archive'] == 'archived')
    if filters.get('review'):
        # One bind avoids SQLite's variable limit for large all-date histories.
        params['review_ids'] = json.dumps(list(filters.get('_review_ids', ())))
        has_review = 'session_id IN (SELECT value FROM json_each(:review_ids))'
        clauses.append(has_review if filters['review'] == 'needed' else '(session_id IS NULL OR NOT (' + has_review + '))')
    if filters.get('start_date'):
        clauses.append('session_date BETWEEN :start_date AND :end_date')
        params.update(start_date=filters['start_date'], end_date=filters['end_date'])
    if filters.get('q'):
        clauses.append("INSTR(LOWER(student_name || ' ' || student_public_id), LOWER(:q)) > 0")
        params['q'] = filters['q']
    where = ' AND '.join(clauses) or '1 = 1'
    offset = -int(config.INSTITUTION_UTC_OFFSET_MINUTES)
    anchor = f"COALESCE(ats.roster_anchor_at, DATETIME(ats.scheduled_date || ' ' || ats.scheduled_start_time, '{offset:+d} minutes'))"
    membership = enrollment_period_contains_sql(anchor, 'ats.roster_anchor_order')
    sql = f"""WITH scoped_classes AS (
        SELECT c.* FROM classes c WHERE {scope}
    ), attendance AS (
        SELECT a.id AS record_id, a.attendance_session_id AS session_id, a.class_id,
               a.student_id, {_current_attendance_status_expr('a')} AS status,
               a.timestamp, a.source, a.reason, a.updated_at
        FROM attendance_records a JOIN scoped_classes c ON c.id = a.class_id
        UNION ALL
        SELECT NULL, ats.id, ats.class_id, cep.student_id, 'Pending', NULL, '', '', NULL
        FROM attendance_sessions ats
        JOIN scoped_classes c ON c.id = ats.class_id
        JOIN class_enrollment_periods cep ON cep.class_id = ats.class_id
        WHERE ats.status IN ('Open', 'Closed') AND {membership}
          AND NOT EXISTS (SELECT 1 FROM attendance_records a
                          WHERE a.attendance_session_id = ats.id AND a.student_id = cep.student_id)
        GROUP BY ats.id, cep.student_id
    ), report_rows AS (
        SELECT a.record_id, a.session_id, a.class_id, s.id AS student_pk,
               COALESCE(ats.scheduled_date, {_institution_local_date_expr('a.timestamp')}) AS session_date,
               COALESCE(ats.status, 'Legacy') AS session_state,
               COALESCE(ats.scheduled_start_time, '') AS scheduled_start_time,
               COALESCE(ats.occurrence_key, '') AS occurrence_key,
               COALESCE(l.collection, 'legacy') AS collection_state,
               COALESCE(l.cleanup, 'legacy') AS cleanup,
               c.teacher_id, c.is_archived, s.program_id,
               c.class_code, c.course_name, COALESCE(c.section, '') AS section,
               COALESCE(r.room_code, '') AS room, COALESCE(ut.username, '') AS teacher_name,
               s.student_id AS student_public_id, s.name AS student_name,
               COALESCE(ap.name, '') AS program, s.year_level,
               CASE WHEN ats.status = 'Open' AND l.session_id IS NULL AND a.status = 'Absent' AND a.source = 'system'
                    THEN 'Pending' ELSE a.status END AS status,
               COALESCE(CASE WHEN ats.id IS NULL THEN {_institution_local_time_expr('a.timestamp')}
                             ELSE TIME(a.timestamp) END, '') AS recorded_time,
               COALESCE(CASE WHEN ats.id IS NULL THEN {_institution_local_datetime_expr('a.timestamp')}
                             ELSE a.timestamp END, '') AS recorded_at,
               COALESCE(a.reason, '') AS reason, COALESCE(a.source, '') AS source,
               COALESCE(a.updated_at, '') AS corrected_at,
               {attendance_audit_kind_sql('a.record_id')} AS manual_kind,
               (SELECT aca.previous_status FROM attendance_change_audit aca
                WHERE aca.attendance_record_id = a.record_id ORDER BY aca.id LIMIT 1) AS initial_status,
               COALESCE((SELECT u.username FROM attendance_change_audit aca JOIN users u ON aca.actor_user_id = u.id
                         WHERE aca.attendance_record_id = a.record_id
                         ORDER BY aca.changed_at DESC, aca.id DESC LIMIT 1), '') AS latest_editor
        FROM attendance a
        JOIN scoped_classes c ON c.id = a.class_id
        JOIN students s ON s.id = a.student_id
        LEFT JOIN academic_programs ap ON ap.id = s.program_id
        LEFT JOIN rooms r ON r.id = c.room_id
        LEFT JOIN users ut ON ut.id = c.teacher_id
        LEFT JOIN attendance_sessions ats ON ats.id = a.session_id
        LEFT JOIN attendance_lifecycle l ON l.session_id = ats.id
    ), filtered AS (SELECT * FROM report_rows WHERE {where})
    """
    return sql, params


def session_report_rows(conn, class_id, session_id):
    """The same saved/derived rows as Reports, after the caller authorizes access.

    Missing historical marks are Pending, including legacy closed sessions.
    Missed/cancelled dates never manufacture student results from a roster.
    """
    sql, params = report_query({'class_id': class_id, 'session_id': session_id}, None, 'Dean')
    return [dict(row) for row in conn.execute(sql + 'SELECT * FROM filtered ORDER BY '
            'student_name COLLATE NOCASE, student_pk', params)]


def review_sessions(conn, filters, user_id, role):
    """Whole-session review state, unaffected by student/status/page filters."""
    scoped = context_filters(filters)
    sql, params = report_query(scoped, user_id, role)
    results = [dict(row) for row in conn.execute(sql + """SELECT session_id, class_id,
        session_date, session_state, SUM(status='Pending') AS pending,
        (SELECT cutoff FROM attendance_lifecycle WHERE session_id=filtered.session_id) AS cutoff,
        (SELECT closed_at FROM attendance_lifecycle WHERE session_id=filtered.session_id) AS closed_at,
        (SELECT end_reason FROM attendance_lifecycle WHERE session_id=filtered.session_id) AS end_reason,
        (SELECT cleanup FROM attendance_lifecycle WHERE session_id=filtered.session_id) AS cleanup
        FROM filtered WHERE session_id IS NOT NULL GROUP BY session_id
        HAVING pending > 0 OR cleanup='pending'""", params)]
    # A corrupted historical membership period must not masquerade as an empty,
    # complete session merely because it contributes no rows to the dataset.
    from services.session_lifecycle import snapshot
    conditions, values = [], []
    if role == 'Teacher':
        conditions.append('c.teacher_id=?')
        values.append(user_id)
    if filters.get('class_id'):
        conditions.append('s.class_id=?')
        values.append(filters['class_id'])
    for key, expression in [('teacher_id', 'c.teacher_id'), ('section', 'c.section'), ('session_id', 's.id')]:
        if filters.get(key):
            conditions.append(expression + '=?')
            values.append(filters[key])
    if filters.get('archive') in ('active', 'archived'):
        conditions.append('c.is_archived=?')
        values.append(int(filters['archive'] == 'archived'))
    if filters.get('start_date'):
        conditions.append('s.scheduled_date BETWEEN ? AND ?')
        values.extend((filters['start_date'], filters['end_date']))
    rows = conn.execute('SELECT s.id,s.class_id,s.scheduled_date,s.status FROM attendance_sessions s JOIN classes c ON c.id=s.class_id JOIN attendance_lifecycle l ON l.session_id=s.id WHERE ' + (' AND '.join(conditions) or '1=1'), values).fetchall()
    for row in rows:
        state = snapshot(conn, row['id'])
        if not state['roster_consistent'] or state['cleanup'] == 'pending':
            existing = next((item for item in results if item['session_id'] == row['id']), None)
            if existing is None:
                existing = dict(session_id=row['id'], class_id=row['class_id'], session_date=row['scheduled_date'], session_state=row['status'], pending=state['counts']['Pending'], cleanup=state['cleanup'])
                results.append(existing)
            if not state['roster_consistent']:
                existing['data_issue'] = 'Historical roster completeness cannot be verified.'
    return sorted(results, key=lambda item: (item['session_date'], item['session_id']), reverse=True)


def context_summary(conn, filters, user_id, role):
    """Read-only, whole-occurrence totals; never infer missing legacy membership."""
    scoped = context_filters(filters)
    sql, params = report_query(scoped, user_id, role)
    counts = dict.fromkeys(STATUSES, 0)
    for row in conn.execute(sql + "SELECT status, COUNT(*) AS n FROM filtered WHERE session_state NOT IN ('Missed','Cancelled') GROUP BY status", params):
        if row['status'] in counts:
            counts[row['status']] = row['n']
    where, values = [], []
    if role == 'Teacher':
        where.append('c.teacher_id=?')
        values.append(user_id)
    for key, expression in [('class_id', 's.class_id'), ('teacher_id', 'c.teacher_id'),
                            ('section', 'c.section'), ('session_id', 's.id')]:
        if scoped.get(key):
            where.append(expression + '=?')
            values.append(scoped[key])
    if scoped.get('archive') in ('active', 'archived'):
        where.append('c.is_archived=?')
        values.append(int(scoped['archive'] == 'archived'))
    if scoped.get('start_date'):
        where.append('s.scheduled_date BETWEEN ? AND ?')
        values.extend((scoped['start_date'], scoped['end_date']))
    sessions = conn.execute('SELECT s.* FROM attendance_sessions s JOIN classes c ON c.id=s.class_id WHERE '
                            + (' AND '.join(where) or '1=1'), values).fetchall()
    from services.session_lifecycle import snapshot
    result = dict(counts=counts, expected=0, explicit=0, held=0, missed=0, cancelled=0,
                  unknown=0, unresolved=0, unclosed=0, cleanup_pending=0)
    for row in sessions:
        if row['status'] in ('Missed', 'Cancelled'):
            result['missed' if row['status'] == 'Missed' else 'cancelled'] += 1
            continue
        result['held'] += 1
        state = snapshot(conn, row['id'])
        if state is None:
            # Migration 023 backfilled timestamps from the then-current roster.
            # Native sequence anchors (025) were not backfilled. A closed legacy
            # occurrence with that anchor and complete explicit historical marks
            # can be counted without inventing missing membership or absences.
            roster = conn.execute('SELECT DISTINCT cep.student_id AS id FROM class_enrollment_periods cep WHERE cep.class_id=:legacy_class AND '
                                  + enrollment_period_contains_sql(':legacy_anchor', ':legacy_order'),
                                  {'legacy_class': row['class_id'], 'legacy_anchor': row['roster_anchor_at'],
                                   'legacy_order': row['roster_anchor_order']}).fetchall() if row['roster_anchor_at'] and row['roster_anchor_order'] is not None else []
            marks = conn.execute(sql + 'SELECT student_pk,status FROM filtered WHERE session_id=:legacy_session', {**params, 'legacy_session': row['id']}).fetchall()
            if (row['status'] == 'Closed' and roster
                    and {student['id'] for student in roster} == {mark['student_pk'] for mark in marks}
                    and all(mark['status'] in STATUSES[:-1] for mark in marks)):
                result['expected'] += len(roster)
                result['explicit'] += len(roster)
            else:
                result['unknown'] += 1
            continue
        # Ending and camera cleanup remain meaningful even if roster data needs review.
        result['unclosed'] += int(not state['closure_confirmed'])
        result['cleanup_pending'] += int(state['cleanup'] == 'pending')
        if not state['roster_consistent']:
            result['unknown'] += 1
            continue
        result['expected'] += state['total']
        result['explicit'] += state['total'] - state['counts']['Pending']
        result['unresolved'] += state['counts']['Pending']
    result['legacy_entries'] = conn.execute(sql + 'SELECT COUNT(*) FROM filtered WHERE session_id IS NULL', params).fetchone()[0]
    result['verifiable'] = not (result['unknown'] or result['legacy_entries'])
    result['completeness'] = round(result['explicit'] / result['expected'] * 100, 1) if result['verifiable'] and result['expected'] else None
    result['complete'] = result['verifiable'] and result['unresolved'] == 0 and result['unclosed'] == 0
    denominator = counts['Present'] + counts['Late'] + counts['Absent']
    result['rate'] = round((counts['Present'] + counts['Late']) / denominator * 100, 1) if result['complete'] and denominator else None
    result['rate_label'] = ('Final rate unavailable: attendance is incomplete or cannot be verified'
                            if not result['complete'] else 'Final attendance rate' if denominator
                            else 'No Present, Late or Absent results in this selection')
    return result


def display_row(row):
    result = dict(row)
    # A correction to an automatically absent record does not create a check-in.
    has_checkin = result['status'] in ('Present', 'Late') and (
        result['source'] == 'face_recognition'
        or result['initial_status'] in ('Present', 'Late')
        or (result['session_id'] is None and result['initial_status'] is None
            and result['source'] in ('system', 'migration'))
    )
    result['checkin_time'] = result['recorded_time'] if has_checkin else ''
    result['method'] = {'face_recognition': 'Camera recognition', 'manual_edit': 'Manual correction',
                        'system': 'System'}.get(result['source'], result['source'] or 'Not recorded')
    if result['source'] == 'manual_edit' and result.get('manual_kind') == 'initial_manual':
        result['method'] = 'Manual attendance'
        result['corrected_at'] = ''
    elif result['source'] == 'manual_edit' and result.get('manual_kind') == 'legacy_manual':
        result['method'] = 'Manual entry (type not recorded)'
        if result['corrected_at']:
            result['corrected_at'] = 'Type not recorded; updated ' + result['corrected_at']
    result['recorded_by'] = result['latest_editor'] or ('System (Camera)' if result['source'] == 'face_recognition'
                                                     else result['method'])
    return result
