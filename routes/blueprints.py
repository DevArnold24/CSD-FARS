"""Flask Blueprint declarations for route groups."""

from flask import Blueprint


auth_bp = Blueprint("auth", __name__)
dashboard_bp = Blueprint("dashboard_routes", __name__)
diagnostics_bp = Blueprint("diagnostics", __name__)
reports_bp = Blueprint("reports", __name__)
admin_bp = Blueprint("admin", __name__)
students_bp = Blueprint("students", __name__)
classes_bp = Blueprint("classes", __name__)
settings_bp = Blueprint("settings", __name__)
entrance_bp = Blueprint("entrance", __name__)


BLUEPRINTS = (
    auth_bp,
    dashboard_bp,
    diagnostics_bp,
    reports_bp,
    admin_bp,
    students_bp,
    classes_bp,
    settings_bp,
    entrance_bp,
)
