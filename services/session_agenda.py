"""Read-only dated class meetings, using saved attendance and schedule periods."""
import datetime as dt
import json
import math
import config

from services import session_lifecycle as life
from services.attendance_sessions import late_marking_time, normalized_day_tokens
from services.attendance_settings import load, SettingsUnavailable
from services.reporting import report_query

VIEWS = {'today': 'Today', 'upcoming': 'Upcoming', 'past': 'Past', 'review': 'Needs review'}


def filters(args, today, default='today'):
    from routes.reports import _validated_date_range
    view = args.get('view', default)
    if view not in VIEWS:
        raise ValueError('Choose Today, Upcoming, Past or Needs review.')
    start, end = _validated_date_range(args.get('start_date', '').strip(), args.get('end_date', '').strip(), required=False)
    state = args.get('state', '').strip()
    if state not in ('', 'Open', 'Closed', 'Missed', 'Cancelled', 'Unrecorded'):
        raise ValueError('Choose a valid session state.')
    page = int(args.get('page', '1'))
    if page < 1:
        raise ValueError('Choose a valid session page.')
    if start and (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 365:
        raise ValueError('Choose a date range of up to one year. Use another range for older meetings.')
    if view == 'today':
        start = end = today.isoformat()
    elif not start and view == 'upcoming':
        start, end = today.isoformat(), (today + dt.timedelta(days=30)).isoformat()
    # Blank Past dates retain access to every saved record; missing dates are
    # derived for 30 days by default or the entire explicit selected range.
    return dict(view=view, start_date=start, end_date=end, state=state), page


def scoped_classes(conn, actor, role, class_id=None):
    if role not in ('Teacher', 'Dean'):
        return []
    clauses, values = ['1=1'], []
    if role == 'Teacher':
        clauses.append('c.teacher_id=?')
        values.append(actor)
    if class_id is not None:
        clauses.append('c.id=?')
        values.append(class_id)
    return [dict(row) for row in conn.execute('''SELECT c.*, u.username AS teacher_name,
        r.room_code FROM classes c JOIN users u ON u.id=c.teacher_id
        LEFT JOIN rooms r ON r.id=c.room_id WHERE ''' + ' AND '.join(clauses) +
        ' ORDER BY c.course_name COLLATE NOCASE,c.id', values)]


def _timing(day, start, end):
    begins = dt.datetime.fromisoformat(day + ' ' + start)
    ends = dt.datetime.fromisoformat(day + ' ' + end) if end else begins + dt.timedelta(minutes=config.SESSION_DURATION_MINUTES)
    if ends and ends <= begins:
        ends += dt.timedelta(days=1)
    return begins, ends


def saved_presentation(conn, item, now=None):
    """Read-only labels, never authority to collect or release a camera."""
    state = life.lifecycle(conn, item['id'])
    stamp = life.epoch(now) if now else life.time.time()
    unfinished = item['status'] == 'Open'
    label = {'Closed': 'Ended', 'Missed': 'No attendance recorded',
             'Cancelled': 'Cancelled'}.get(item['status'], 'Unfinished')
    if state:
        unfinished = state['closed_at'] is None
        if not unfinished:
            label = 'Ended'
        elif life.clock_unreliable(conn):
            label = 'Interrupted'
        elif state['end_operation'] or stamp >= state['cutoff']:
            label = 'Attendance stopped'
        elif state['collection'] == 'starting':
            label = 'Starting'
        elif life.collection_allowed(state, stamp):
            label = 'In progress'
        elif state['collection'] == 'interrupted':
            label = 'Interrupted'
        else:
            label = 'Attendance stopped'
    return dict(label=label, unfinished=unfinished,
                tone='success' if label == 'In progress' else 'warning' if unfinished else 'secondary')


def agenda(conn, classes, selected, page, actor, role, now=None, page_size=25):
    now = now or life.institutional_now()
    today = now.date()
    by_id = {c['id']: c for c in classes}
    ids = json.dumps(list(by_id))
    view, start, end = (selected[k] for k in ('view', 'start_date', 'end_date'))
    parameters = {'ids': ids, 'start': start, 'end': end}
    date_clause = ' AND s.scheduled_date BETWEEN :start AND :end' if start else ''
    if view == 'today':
        # Ongoing work belongs here even across midnight, including unresolved
        # closure. Do not conceal an older camera reservation in Past.
        date_clause = ''' AND (s.scheduled_date BETWEEN :start AND :end OR s.status='Open'
            OR EXISTS (SELECT 1 FROM attendance_lifecycle l WHERE l.session_id=s.id AND l.closed_at IS NULL))'''
        parameters.update(start=(today - dt.timedelta(days=1)).isoformat(),
                          end=(today + dt.timedelta(days=1)).isoformat())
    saved = [dict(row) for row in conn.execute('''SELECT s.* FROM attendance_sessions s
        WHERE s.class_id IN (SELECT value FROM json_each(:ids))''' + date_clause + '''
        AND (s.occurrence_key IS NOT NULL OR NOT EXISTS (
          SELECT 1 FROM attendance_sessions canonical WHERE canonical.class_id=s.class_id
          AND canonical.scheduled_date=s.scheduled_date AND canonical.occurrence_key IS NOT NULL))''', parameters)]
    report_dates = {k: selected[k] for k in ('start_date', 'end_date')} if view != 'today' else {}
    sql, params = report_query(report_dates, actor, role)
    counts = {}
    for row in conn.execute(sql + '''SELECT session_id,status,COUNT(*) AS n FROM filtered
            WHERE session_id IN (SELECT value FROM json_each(:session_ids)) GROUP BY session_id,status''',
            {**params, 'session_ids': json.dumps([s['id'] for s in saved])}):
        counts.setdefault(row['session_id'], {})[row['status']] = row['n']
    recorded_dates = {(s['class_id'], s['scheduled_date']) for s in saved}
    rows = []
    for item in saved:
        item.update({key: by_id[item['class_id']].get(key) for key in
                     ('course_name', 'class_code', 'teacher_name', 'room_code', 'is_archived')})
        item['counts'] = counts.get(item['id'], {})
        item['pending_count'] = item['counts'].get('Pending', 0)
        item['recorded_count'] = sum(item['counts'].get(s, 0) for s in ('Present', 'Late', 'Absent', 'Excused'))
        item['total_count'] = item['recorded_count'] + item['pending_count']
        item['preview'] = False
        item.update(saved_presentation(conn, item, now))
        if view == 'review' and not item['pending_count']:
            continue
        if view == 'today' and not item['unfinished'] and item['scheduled_date'] != today.isoformat():
            continue
        if view == 'past' and item['unfinished']:
            continue
        if view == 'upcoming':  # Accepted occurrences are current work or history.
            continue
        rows.append(item)
    opening_error, policy = '', None
    try:
        policy = load(conn)
    except SettingsUnavailable as exc:
        opening_error = str(exc)
    if view != 'review':
        first = dt.date.fromisoformat(start) if start else today - dt.timedelta(days=30)
        last = dt.date.fromisoformat(end) if end else today
        if view == 'past':
            last = min(last, today)
        elif view == 'upcoming':
            first = max(first, today)
        elif view == 'today':
            first = max(dt.date.min, today - dt.timedelta(days=1))
            last = min(dt.date.max, today + dt.timedelta(days=1))
        periods = conn.execute('''SELECT * FROM class_schedule_periods WHERE class_id IN
            (SELECT value FROM json_each(?)) AND effective_from<=?
            AND (effective_until IS NULL OR effective_until>?) ORDER BY effective_from,id''',
            (ids, last.isoformat(), first.isoformat())).fetchall()
        for period in periods:
            info = by_id[period['class_id']]
            if info['is_archived'] and (view in ('today', 'upcoming') or (view == 'date' and first >= today)):
                continue
            low = max(first, dt.date.fromisoformat(period['effective_from']))
            high = min(last, dt.date.fromisoformat(period['effective_until']) - dt.timedelta(days=1)) if period['effective_until'] else last
            day = low
            while day <= high:
                date_text = day.isoformat()
                if (day.strftime('%a') in normalized_day_tokens(period['days_of_week'])
                        and (info['id'], date_text) not in recorded_dates):
                    begins, ends = _timing(date_text, period['start_time'], period['end_time'])
                    past = ends is not None and now >= ends
                    opening = begins - dt.timedelta(minutes=policy['minutes']) if policy else None
                    timing_error = ''
                    try:
                        late_at = late_marking_time(begins, info['late_threshold_minutes'])
                    except ValueError as exc:
                        late_at, timing_error = None, str(exc)
                    window_open = bool(not timing_error and opening and ends and opening <= now < ends)
                    include = (view != 'past' or past)
                    if view == 'today':
                        include = day == today or window_open
                    elif view == 'upcoming':
                        include = not past and (opening is None or now < opening)
                    if include:
                        label = 'No attendance recorded' if past else 'Timing unavailable' if timing_error else 'Not started' if window_open else 'Not open yet' if opening else 'Opening unavailable'
                        rows.append(dict(id=None, class_id=info['id'], course_name=info['course_name'],
                            class_code=info['class_code'], teacher_name=info['teacher_name'], room_code=info['room_code'],
                            is_archived=info['is_archived'], schedule_period_id=period['id'], scheduled_date=date_text,
                            scheduled_start_time=period['start_time'], scheduled_end_time=period['end_time'],
                            status='Unrecorded' if past else 'Scheduled', label=label, tone='secondary',
                            window_open=window_open, early=window_open and now < begins, unfinished=False,
                            preview=True, opening=opening, opening_error=opening_error,
                            late_at=late_at, timing_error=timing_error, ends=ends,
                            close_reason='', pending_count=0, total_count=0, recorded_count=0,
                            can_explain=past and day < today))
                if day == high:
                    break
                day += dt.timedelta(days=1)
    if selected['state']:
        rows = [r for r in rows if r['status'] == selected['state'] or
                (selected['state'] == 'Unrecorded' and r['status'] == 'Missed')]
    rows.sort(key=lambda r: (r['scheduled_date'], r['scheduled_start_time'] or '', r['class_id']), reverse=view in ('past', 'review'))
    total = len(rows)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    rows = rows[(page-1)*page_size:page*page_size]
    for item in rows:
        if item['id'] is None:
            continue
        state = life.snapshot(conn, item['id'])
        if state:
            item['lifecycle_message'] = state['message']
            item['history_consistent'] = state['roster_consistent']
        else:
            item['legacy_results'] = True
        if item['status'] == 'Missed':
            item['explanation_history'] = explanation_history(conn, item['id'])
    return dict(sessions=rows, history_total=total, history_page=page, history_pages=pages)


def explanation_history(conn, session_id):
    return [dict(row, **json.loads(row['details'])) for row in conn.execute('''
        SELECT a.details,a.created_at,COALESCE(u.username,'Former account') AS actor
        FROM system_audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id
        WHERE a.action='session_explanation_changed' AND json_extract(a.details,'$.session_id')=?
        ORDER BY a.id DESC''', (session_id,))]


def record_explanation(conn, sid, actor, previous, new):
    conn.execute('INSERT INTO system_audit_logs(actor_user_id,action,details) VALUES(?,?,?)',
                 (actor, 'session_explanation_changed', json.dumps(dict(session_id=sid, previous=previous, new=new))))
