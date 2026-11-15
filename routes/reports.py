import datetime
import io
import math
from urllib.parse import urlencode
import config
import openpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter
from flask import jsonify, render_template, request, session, send_file
from core import get_logger
from core.utils import login_required
from security.rbac import permission_required
from database.db_manager import managed_transaction
from routes.blueprints import reports_bp
from services.export_security import spreadsheet_safe_text
from services.reporting import STATUSES, ORDER_BY, DETAILS_NOTE, report_query, display_row, review_sessions, context_summary

logger = get_logger('reports')
PAGE_SIZE = 25
FILTER_KEYS = ('start_date', 'end_date', 'class_id', 'student_id', 'q', 'status',
               'teacher_id', 'section', 'session_id', 'program_id', 'year_level', 'archive', 'review', 'date_scope')


def institution_today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))).date()


def _validated_date_range(start_date, end_date, *, required):
    start_date, end_date = str(start_date or '').strip(), str(end_date or '').strip()
    if not start_date and not end_date and not required:
        return '', ''
    if not start_date or not end_date:
        raise ValueError('Start date and end date are required.')
    try:
        first, last = datetime.date.fromisoformat(start_date), datetime.date.fromisoformat(end_date)
        if first.isoformat() != start_date or last.isoformat() != end_date:
            raise ValueError()
    except ValueError as exc:
        raise ValueError('Dates must use YYYY-MM-DD and represent real calendar dates.') from exc
    if first > last:
        raise ValueError('Start date must be on or before end date.')
    return start_date, end_date


def _filters(export=False):
    values = {key: request.args.get(key, '').strip() for key in FILTER_KEYS}
    scope = values['date_scope'] or ('all' if request.args.get('all_dates') == '1' else 'range' if values['start_date'] or values['end_date'] else 'today')
    if scope not in ('today', 'range', 'all'):
        raise ValueError('Choose Today, a date range, or all dates.')
    values['date_scope'] = scope
    if scope == 'today':
        values['start_date'] = values['end_date'] = str(institution_today())
    elif scope == 'all':
        values['start_date'] = values['end_date'] = ''
    values['start_date'], values['end_date'] = _validated_date_range(
        values['start_date'], values['end_date'], required=scope != 'all')
    if values['status'] and values['status'] not in STATUSES:
        raise ValueError('Choose a valid attendance status.')
    for key in ('class_id', 'teacher_id', 'session_id', 'program_id'):
        if values[key] and not (values[key].isascii() and values[key].isdigit() and 0 < int(values[key]) <= 2**63-1):
            raise ValueError('Choose a valid class, teacher, occurrence or program.')
    if values['teacher_id'] and session['role'] != 'Dean':
        raise ValueError('Teacher filtering is available only to the Dean.')
    if values['archive'] not in ('', 'active', 'archived') or values['review'] not in ('', 'needed', 'clear'):
        raise ValueError('Choose a valid archive or review filter.')
    if any(len(values[key]) > 120 for key in ('q', 'student_id', 'section', 'year_level')):
        raise ValueError('Search and category values must be 120 characters or fewer.')
    return values


def _report_url(filters, **changes):
    return '/reports?' + urlencode({k: v for k, v in {**filters, **changes}.items() if v != '' and not k.startswith('_')})


def _filter_options(conn, classes, filters):
    """Keep submitted choices on validation errors without widening visibility."""
    options = {
        'date_scope': [('today', 'Today'), ('range', 'Date range'), ('all', 'All dates')],
        'class_id': [(str(c['id']), f"{c['course_name']} ({c['class_code']})"
                      + (f" · {c['section']}" if c['section'] else '')
                      + (' · Archived' if c['is_archived'] else '')) for c in classes],
        'status': [(value, value) for value in STATUSES],
        'archive': [('active', 'Current'), ('archived', 'Archived')],
        'review': [('needed', 'Attendance, camera or record review'), ('clear', 'No recorded issues')],
        'teacher_id': [],
    }
    sql, params = report_query({}, session['user_id'], session['role'])
    for key in ('section', 'year_level', 'program_id'):
        rows = conn.execute(sql + f'SELECT DISTINCT {key}' + (', program' if key == 'program_id' else '')
                            + f" FROM filtered WHERE {key} IS NOT NULL AND {key} != '' ORDER BY {key}", params)
        options[key] = [(str(row[key]), row['program'] if key == 'program_id' else str(row[key])) for row in rows]
    if session['role'] == 'Dean':
        options['teacher_id'] = [(str(row['id']), row['username']) for row in conn.execute(
            'SELECT DISTINCT u.id,u.username FROM users u JOIN classes c ON c.teacher_id=u.id ORDER BY u.username')]
    for key, values in options.items():
        selected = filters.get(key)
        if selected and selected not in {value for value, _ in values}:
            # Echo only the supplied value, never look up another teacher's data.
            values.append((selected, f'Unavailable selection ({selected})'))
    return options


@reports_bp.route('/api/reports/export', methods=['GET'])
@login_required
@permission_required('export_reports')
def api_reports_export():
    try:
        filters = _filters(export=True)
    except ValueError as exc:
        return jsonify(error=str(exc), status=400), 400
    try:
        with managed_transaction(commit=False) as conn:
            # SELECT alone does not start a Python SQLite read transaction.
            conn.execute('BEGIN')
            reviews = review_sessions(conn, filters, session['user_id'], session['role'])
            filters['_review_ids'] = [item['session_id'] for item in reviews]
            sql, params = report_query(filters, session['user_id'], session['role'])
            rows = [display_row(row) for row in conn.execute(sql + 'SELECT * FROM filtered ORDER BY ' + ORDER_BY, params)]
            context = context_summary(conn, filters, session['user_id'], session['role'])
            selected_class = conn.execute('SELECT course_name,class_code FROM classes WHERE id=?' +
                (' AND teacher_id=?' if session['role'] == 'Teacher' else ''),
                (filters['class_id'], session['user_id']) if session['role'] == 'Teacher' else (filters['class_id'],)).fetchone() if filters['class_id'] else None
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Attendance Export'
        ws.append(['Session Date', 'Class Code', 'Course Name', 'Section', 'Room', 'Teacher',
                   'Student ID', 'Student Name', 'Program', 'Year Level', 'Status', 'Check-in Time',
                   'Reason', 'Recorded By', 'Session State', 'Record Created At', 'Last Correction At',
                   'Occurrence ID', 'Scheduled Start', 'Occurrence Key', 'Collection State', 'Camera Cleanup'])
        keys = ('session_date', 'class_code', 'course_name', 'section', 'room', 'teacher_name',
                'student_public_id', 'student_name', 'program', 'year_level', 'status', 'checkin_time',
                'reason', 'recorded_by', 'session_state', 'recorded_at', 'corrected_at',
                'session_id', 'scheduled_start_time', 'occurrence_key', 'collection_state', 'cleanup')
        for row in rows:
            readable = dict(row)
            readable['cleanup'] = {'not_applicable': 'No camera used', 'confirmed': 'Stop confirmed', 'pending': 'Stop not confirmed'}.get(row.get('cleanup'), row.get('cleanup') or 'Not recorded')
            readable['collection_state'] = {'collecting': 'Attendance open', 'starting': 'Preparing camera', 'stopped': 'Collection stopped', 'interrupted': 'Interrupted'}.get(row.get('collection_state'), row.get('collection_state') or 'Not recorded')
            readable['checkin_time'] = row.get('checkin_time') or 'Not recorded'
            ws.append([spreadsheet_safe_text(readable.get(key) or '') for key in keys])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for column in ws.columns:
            ws.column_dimensions[get_column_letter(column[0].column)].width = min(42, max(14, max(len(str(c.value or '')) for c in column) + 2))
            for cell in column:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        ws.print_title_rows = '1:1'
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        notes = wb.create_sheet('Report Details')
        for label, value in [
            ('Generated at', datetime.datetime.now(datetime.timezone(datetime.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))).isoformat(timespec='seconds')),
            ('Timezone', f'Institutional time UTC{config.INSTITUTION_UTC_OFFSET_MINUTES / 60:+g}'),
            ('Scope', 'All matching entries, including entries on other report pages.'),
            ('Dates', (filters['start_date'] + ' to ' + filters['end_date']) if filters['start_date'] else 'All recorded dates'),
            ('Class', (selected_class['course_name'] + ' (' + selected_class['class_code'] + ') · ID ' + filters['class_id']) if selected_class else ('Class unavailable' if filters['class_id'] else 'All classes')), ('Student ID', filters['student_id'] or 'All students'),
            ('Student search', filters['q']), ('Status', filters['status'] or 'All statuses'),
            ('Entries', len(rows)), ('Current details', DETAILS_NOTE),
            ('Context completeness', f"{context['explicit']} explicit / {context['expected']} verified expected entries; {context['unknown']} occurrences with unverifiable membership; {context['legacy_entries']} unlinked legacy entries."),
            ('Final attendance rate', str(context['rate']) + '%' if context['rate'] is not None else 'N/A — incomplete, unverifiable or zero denominator'),
            ('Rate definition', '(Present + Late) / (Present + Late + Absent). Excused separate. Pending never Absent. Whole context ignores student, status, current program/year and review detail filters.'),
            ('Occurrence population', f"{context['held']} held; {context['missed']} saved missed; {context['cancelled']} cancelled. Virtual missed dates are shown in class history, not fabricated attendance."),
            ('Pending', 'No explicit attendance result is recorded. Closed new-policy sessions retain Pending students who require individual review; Pending is not Absent.'),
            ('Check-in time', 'Shown when supported by the original attendance record or its correction history. A manual correction does not invent a check-in time.'),
            ('Other times', 'Institutional local time. Record creation can be an automatic absence or manual entry, not a check-in.')]:
            notes.append([label, spreadsheet_safe_text(value)])
        for key in ('teacher_id', 'section', 'session_id', 'program_id', 'year_level', 'archive', 'review'):
            notes.append([key.replace('_', ' ').title(), spreadsheet_safe_text(filters[key] or 'All')])
        notes.append(['Snapshot', 'Generated from current saved results. Corrections made after viewing the report may change this download.'])
        for item in reviews:
            notes.append(['Whole-session review', f"Session {item['session_id']}: {item['pending']} Pending; state {item['session_state']}; cleanup {item['cleanup'] or 'legacy'}. Counts ignore student/status/page filters. {item.get('data_issue', '')}"])
        notes.column_dimensions['A'].width, notes.column_dimensions['B'].width = 23, 100
        for row in notes:
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        suffix = filters['start_date'] + '_to_' + filters['end_date'] if filters['start_date'] else 'all_dates'
        response = send_file(output, as_attachment=True, download_name=f'attendance_export_{suffix}.xlsx',
                             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['X-Attendance-Record-Count'] = str(len(rows))
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception:
        logger.exception('Report export failed')
        return jsonify(error='The report could not be prepared. Please try again.', status=500), 500


@reports_bp.route('/reports')
@login_required
@permission_required('export_reports')
def reports_index():
    error = None
    loaded_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(
        minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))).strftime('%d %b %Y, %I:%M:%S %p')
    filters = {key: request.args.get(key, '').strip() for key in FILTER_KEYS}
    filters['date_scope'] = filters['date_scope'] or ('all' if request.args.get('all_dates') == '1'
        else 'range' if filters['start_date'] or filters['end_date'] else 'today')
    page = 1
    try:
        filters = _filters()
        page = int(request.args.get('page', '1'))
        if page < 1:
            raise ValueError('Choose a valid report page.')
    except ValueError as exc:
        error = str(exc)
    summary = dict.fromkeys(('Present', 'Late', 'Absent', 'Excused', 'Total'), 0)
    stats = dict(students=0, sessions=0, pending=0, open_entries=0)
    rows, pages = [], 1
    reviews, context, review_page = [], None, 1
    with managed_transaction(commit=False) as conn:
        conn.execute('BEGIN')
        classes = conn.execute('SELECT id, course_name, class_code, section, is_archived FROM classes ' +
                               ('WHERE teacher_id = ? ' if session['role'] == 'Teacher' else '') +
                               'ORDER BY course_name, class_code, section',
                               (session['user_id'],) if session['role'] == 'Teacher' else ()).fetchall()
        if not error:
            reviews = review_sessions(conn, filters, session['user_id'], session['role'])
            context = context_summary(conn, filters, session['user_id'], session['role'])
            filters['_review_ids'] = [item['session_id'] for item in reviews]
            sql, params = report_query(filters, session['user_id'], session['role'])
            counts = conn.execute(sql + "SELECT COUNT(*) AS total, COUNT(DISTINCT student_pk) AS students, COUNT(DISTINCT session_id) AS sessions, COALESCE(SUM(status='Pending'),0) AS pending, COALESCE(SUM(session_state='Open'),0) AS open_entries FROM filtered", params).fetchone()
            summary['Total'] = counts['total']
            stats = {key: counts[key] for key in stats}
            for row in conn.execute(sql + 'SELECT status, COUNT(*) AS total FROM filtered GROUP BY status', params):
                if row['status'] in summary:
                    summary[row['status']] = row['total']
            pages = max(1, math.ceil(summary['Total'] / PAGE_SIZE))
            page = min(page, pages)
            rows = [display_row(row) for row in conn.execute(sql + 'SELECT * FROM filtered ORDER BY ' + ORDER_BY + ' LIMIT :limit OFFSET :offset',
                    {**params, 'limit': PAGE_SIZE, 'offset': (page - 1) * PAGE_SIZE})]
        options = _filter_options(conn, classes, filters)
    filters.pop('_review_ids', None)
    today = institution_today()
    shortcuts = [(label, _report_url(filters, date_scope='all' if first is None else 'range', start_date=str(first) if first else '', end_date=str(today) if first else '')) for label, first in
                 [('Today', today), ('This week', today - datetime.timedelta(days=today.weekday())), ('This month', today.replace(day=1)), ('All dates', None)]]
    review_count = len(reviews)
    try:
        review_page = max(1, min(int(request.args.get('review_page', '1')), max(1, math.ceil(review_count/PAGE_SIZE))))
    except ValueError:
        error = 'Choose a valid review page.'
    reviews = reviews[(review_page-1)*PAGE_SIZE:review_page*PAGE_SIZE]
    class_names = {str(c['id']): f"{c['course_name']} ({c['class_code']})" for c in classes}
    clear_filters = [('Class' if key == 'class_id' else key.replace('_', ' ').title(),
                      class_names.get(value, 'Class unavailable') if key == 'class_id' else value,
                      _report_url(filters, **{key: ''})) for key, value in filters.items()
                     if value and key not in ('start_date', 'end_date', 'date_scope')]
    # A page viewed before midnight must download the dates it actually showed.
    export_filters = {**filters, 'date_scope': 'range' if filters['start_date'] else 'all'}
    return render_template('reports/index.html', filters=filters, start_date=filters['start_date'], end_date=filters['end_date'],
        summary=summary, stats=stats, rows=rows, classes=classes, validation_error=error, statuses=STATUSES,
        details_note=DETAILS_NOTE, shortcuts=shortcuts, page=page, pages=pages, reviews=reviews,
        context=context, options=options, export_filters=export_filters, clear_filters=clear_filters, review_count=review_count,
        loaded_at=loaded_at, school_timezone=f'UTC{config.INSTITUTION_UTC_OFFSET_MINUTES / 60:+g}',
        all_dates_url=_report_url(filters, date_scope='all', start_date='', end_date=''),
        current_report_url=_report_url(filters, page=page, review_page=review_page),
        review_previous=_report_url(filters, review_page=review_page-1) if review_page > 1 else None,
        review_next=_report_url(filters, review_page=review_page+1) if review_page*PAGE_SIZE < review_count else None,
        first=(page-1)*PAGE_SIZE+1 if rows else 0, last=(page-1)*PAGE_SIZE+len(rows),
        previous_url=_report_url(filters, page=page-1), next_url=_report_url(filters, page=page+1)), 400 if error else 200, {'Cache-Control': 'no-store'}
