"""Central role and permission definitions."""

import functools

from flask import abort, session


ROLE_DEAN = "Dean"
ROLE_TEACHER = "Teacher"
ROLE_IT_SUPPORT = "IT_Support"
ROLE_OFFICE_ADMIN = "Office_Admin"

ROLES = (ROLE_DEAN, ROLE_TEACHER, ROLE_IT_SUPPORT, ROLE_OFFICE_ADMIN)

PERMISSIONS_BY_ROLE = {
    ROLE_DEAN: {
        "manage_attendance_settings",
        "approve_student_registrations",
        "edit_student_metadata",
        "export_reports",
        "manage_archives",
        "manage_backups",
        "manage_cameras",
        "manage_classes",
        "manage_deployment",
        "manage_locations",
        "manage_programs",
        "manage_recognition_settings",
        "manage_rosters",
        "manage_student_biometrics",
        "manage_student_status",
        "permanently_remove_students",
        "manage_users",
        "register_students_direct",
        "run_purge",
        "run_recognition_diagnostics",
        "search_students_for_roster",
        "submit_student_registration",
        "use_registration_camera",
        "view_attendance_reports",
        "view_audit_logs",
        "view_diagnostics",
        "view_student_directory",
        "view_student_profiles",
        "view_system_health",
        "view_technical_history",
    },
    ROLE_TEACHER: {
        "correct_attendance",
        "export_reports",
        "manage_classes",
        "manage_rosters",
        "run_attendance_session",
        "search_students_for_roster",
        "submit_student_registration",
        "use_registration_camera",
        "view_attendance_reports",
        "view_own_student_profiles",
    },
    ROLE_OFFICE_ADMIN: {
        "approve_student_registrations",
        "manage_student_biometrics",
        "register_students_direct",
        "view_student_directory",
        "view_student_profiles",
        "use_registration_camera",
    },
    ROLE_IT_SUPPORT: {
        "manage_cameras",
        "manage_recognition_settings",
        "run_recognition_diagnostics",
        "view_diagnostics",
        "view_system_health",
        "view_technical_history",
    },
}


def permissions_for_role(role):
    """Return the immutable permission set for a role."""
    return frozenset(PERMISSIONS_BY_ROLE.get(role, set()))


def has_permission(role, permission):
    """Return whether a role grants a single permission."""
    return permission in permissions_for_role(role)


def current_user_has_permission(permission):
    """Return whether the current session role grants a permission."""
    return has_permission(session.get("role"), permission)


def permission_required(*permissions):
    """Decorator that requires the current user to hold any listed permission."""
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapped(*args, **kwargs):
            role = session.get("role")
            if not any(has_permission(role, permission) for permission in permissions):
                abort(403)
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


def role_required(*roles):
    """Compatibility decorator for legacy role-based checks."""
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                abort(403)
            return view_func(*args, **kwargs)
        return wrapped
    return decorator
