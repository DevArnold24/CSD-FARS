from flask import current_app, render_template, request, session, redirect, url_for, flash
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta, timezone
import sqlite3

from core import get_logger
from core.utils import login_required
from database.db_manager import get_db_connection, managed_transaction
from routes.blueprints import auth_bp
from security.passwords import validate_password_complexity, browser_password_policy
from security.session_auth import authenticated_session_user, establish_authenticated_session
from security import login_notice

auth_bp.add_app_template_global(browser_password_policy)

logger = get_logger("auth")


@auth_bp.app_context_processor
def login_notice_context():
    return {"login_notice_version": login_notice.VERSION}


def login_form(*, notice_error=None):
    return render_template("login.html", notice_error=notice_error,
                           login_username=request.form.get("username", "").strip(),
                           notice_collapsed=not notice_error and
                           request.cookies.get("csd_notice_view") == login_notice.VERSION)


def is_user_locked(user):
    try:
        locked_until_str = user["locked_until"]
    except (KeyError, IndexError, TypeError):
        locked_until_str = None

    if not locked_until_str:
        return False
    try:
        if "T" in locked_until_str:
            locked_until = datetime.fromisoformat(locked_until_str)
        else:
            locked_until = datetime.strptime(locked_until_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        
        now = datetime.now(timezone.utc)
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
            
        if now < locked_until:
            return True
    except Exception as e:
        logger.error(f"Error parsing locked_until: {e}")
    return False


@auth_bp.route("/")
def index():
    """Root route. Redirect to dashboard if authenticated, else to login."""
    if "user_id" in session:
        return redirect(url_for("dashboard_routes.dashboard"))
    return redirect(url_for("auth.login"))


@auth_bp.route("/about")
def about():
    """Render the public capstone/about page."""
    return render_template("about.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render the login form and handle credential verification."""
    if "user_id" in session:
        return redirect(url_for("dashboard_routes.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return login_form()

        with managed_transaction(commit=False) as conn:
            user = conn.execute(
                "SELECT id, username, password_hash, role, requires_password_change, is_active, failed_login_attempts, locked_until FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if user:
            if is_user_locked(user):
                flash("Account temporarily locked. Please try again later.", "error")
                return login_form()

            if check_password_hash(user["password_hash"], password):
                if not user["is_active"]:
                    logger.warning("Attempted login by inactive user '%s'.", username)
                    flash("Your account has been deactivated. Contact the Dean to check your access. Resetting your password does not reactivate the account.", "error")
                    return login_form()

                try:
                    with managed_transaction(commit=True) as conn:
                        updated = conn.execute(
                            """UPDATE users SET failed_login_attempts = 0, locked_until = NULL
                           WHERE id = ? AND password_hash = ? AND is_active = 1
                             AND COALESCE(locked_until, '') = ?""",
                            (user["id"], user["password_hash"], user["locked_until"] or ""),
                        )
                        if not updated.rowcount:
                            session.clear()
                            flash("Your account changed. Please log in again.", "error")
                            return redirect(url_for("auth.login"))
                        # The writer lock binds the refreshed role and acknowledgment
                        # to the credential we verified, even during account changes.
                        user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
                        remember_notice = login_notice.accept_or_recall(conn, user["id"], request.form)
                        if remember_notice is None:
                            return login_form(notice_error=(
                                "Please read the current notice and acknowledge these responsibilities "
                                "for your account. Re-enter your password to sign in."))
                except sqlite3.Error:
                    logger.exception("Login acknowledgment could not be confirmed.")
                    return login_form(notice_error=(
                        "We could not confirm your acknowledgment. You are not signed in. "
                        "Please try again; if this continues, contact technical support.")), 503

                establish_authenticated_session(session, user)
                logger.info(
                    "User '%s' (role=%s) logged in.", user["username"], user["role"]
                )
                response = redirect(url_for("dashboard_routes.dashboard"))
                if remember_notice:
                    # No identity or credential: this untrusted cookie controls only
                    # presentation. Account/version memory is always checked in SQLite.
                    response.set_cookie("csd_notice_view", login_notice.VERSION,
                                        max_age=365 * 24 * 60 * 60, httponly=True,
                                        secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
                                        samesite="Lax")
                else:
                    response.delete_cookie("csd_notice_view", httponly=True, samesite="Lax")
                return response
            else:
                with managed_transaction(commit=True) as conn:
                    # Password verification can overlap other requests. Count
                    # against the current row under the writer lock so attempts
                    # cannot overwrite each other or remove a newly set lock.
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(
                        """SELECT failed_login_attempts, locked_until FROM users
                           WHERE id = ? AND password_hash = ?""",
                        (user["id"], user["password_hash"]),
                    ).fetchone()
                    if current and not is_user_locked(current):
                        # An expired lock starts a fresh failure window.
                        failed_attempts = 0 if current["locked_until"] else (current["failed_login_attempts"] or 0)
                        new_attempts = failed_attempts + 1
                        locked_until = None
                        if new_attempts >= 5:
                            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                        conn.execute(
                            "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                            (new_attempts, locked_until, user["id"]),
                        )

                logger.warning("Failed login attempt for username '%s'.", username)
                flash("Invalid credentials.", "error")
        else:
            logger.warning("Failed login attempt for nonexistent username '%s'.", username)
            flash("Invalid credentials.", "error")

    return login_form()


@auth_bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Clear the session and redirect to the login page."""
    username = session.get("username", "unknown")
    session.clear()
    logger.info("User '%s' logged out.", username)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/password_reset", methods=["GET", "POST"])
@login_required
def password_reset():
    """Forces the user to reset their default password."""
    user_id = session.get("user_id")
    with managed_transaction(commit=False) as conn:
        user = authenticated_session_user(conn, session)

    if not user:
        session.clear()
        flash("Your account changed. Please log in again.", "error")
        return redirect(url_for("auth.login"))

    if user and is_user_locked(user):
        # A forced-reset session can outlive a lock applied in another tab or
        # by an administrator.  Clear it before returning to login so the
        # login/dashboard/password-reset redirects cannot form a loop.
        session.clear()
        flash("Account temporarily locked. Please try again later.", "error")
        return redirect(url_for("auth.login"))

    if not session.get("requires_password_change"):
        return redirect(url_for("dashboard_routes.dashboard"))
        
    if request.method == "POST":
        new_password = request.form.get("password")
        confirm = request.form.get("confirm_password")
        
        if not new_password or new_password != confirm:
            flash("Passwords do not match or are empty.", "error")
            return render_template("password_reset.html")
            
        is_valid, err_msg = validate_password_complexity(new_password)
        if not is_valid:
            flash(err_msg, "error")
            return render_template("password_reset.html")
            
        password_hash = generate_password_hash(new_password)
        
        try:
            with managed_transaction(commit=True) as conn:
                updated = conn.execute(
                    """UPDATE users SET password_hash = ?, requires_password_change = 0
                       WHERE id = ? AND password_hash = ? AND requires_password_change = 1
                         AND is_active = 1 AND COALESCE(locked_until, '') = ?""",
                    (password_hash, user_id, user["password_hash"], user["locked_until"] or ""),
                )
                if not updated.rowcount:
                    session.clear()
                    flash("Your account changed. Please log in again.", "error")
                    return redirect(url_for("auth.login"))
                user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            establish_authenticated_session(session, user)
            flash("Password updated successfully.", "success")
            return redirect(url_for("dashboard_routes.dashboard"))
        except Exception as e:
            logger.exception("Failed to update password.")
            flash("Your password change could not be confirmed. Sign out and try signing in with your new password. If you still cannot sign in, contact the Dean. If you are the only Dean, contact the technician who set up this installation.", "error")
    return render_template("password_reset.html")
