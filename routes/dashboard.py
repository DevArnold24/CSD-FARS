import datetime
import config
from flask import render_template, session, redirect, url_for, flash
from database.db_manager import get_db_connection, managed_transaction
from core.utils import login_required, _current_attendance_status_expr
from routes.blueprints import dashboard_bp
from services.camera_health import camera_health_summary
from services.reporting import context_summary
from services import session_lifecycle


def _institution_now():
    return session_lifecycle.institutional_now()


@dashboard_bp.route("/dashboard")
@login_required
def dashboard():
    """Role-branched dashboard acting as a traffic controller."""
    role = session.get("role")
    user_id = session.get("user_id")
    institution_now = _institution_now()
    institution_today = institution_now.date()
    institution_date = institution_today.isoformat()
    institution_offset = f"{config.INSTITUTION_UTC_OFFSET_MINUTES:+d} minutes"
    camera_health = camera_health_summary() if role == "Dean" else None
    
    with managed_transaction(commit=False) as conn:
        if role == "Teacher":
            from services.session_agenda import agenda, filters, scoped_classes
            selected, page = filters({}, institution_today)
            meetings = agenda(conn, scoped_classes(conn, user_id, role), selected, page, user_id, role, now=institution_now)
            
            attendance = context_summary(conn, {'start_date': institution_date, 'end_date': institution_date}, user_id, role)
            return render_template("dashboard/teacher.html", classes=meetings['sessions'],
                                   meeting_total=meetings['history_total'], summary_date=institution_date,
                                   stats=attendance['counts'], attendance=attendance)
            
        elif role == "Dean":
            classes = conn.execute(
                "SELECT c.*, u.username as teacher_name FROM classes c JOIN users u ON c.teacher_id = u.id WHERE u.role = 'Teacher' AND c.is_archived = 0 ORDER BY c.id DESC"
            ).fetchall()
            active_count = sum(1 for c in classes if c["is_active"])
            
            teachers = conn.execute("SELECT id, username FROM users WHERE role = 'Teacher' ORDER BY username").fetchall()
            
            attendance = context_summary(conn, {'start_date': institution_date, 'end_date': institution_date}, user_id, role)
            return render_template("dashboard/dean.html", classes=classes, active_count=active_count,
                                   teachers=teachers, attendance_rate=attendance['rate'],
                                   attendance=attendance, camera_health=camera_health)
            
        elif role == "IT_Support":
            return render_template("dashboard/it_support.html")
            
        elif role == "Office_Admin":
            return render_template("dashboard/office_admin.html")
            
        else:
            flash("Invalid role.", "error")
            return redirect(url_for("auth.login"))
