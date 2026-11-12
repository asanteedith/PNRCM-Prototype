"""Security & Access Governance for PNRCM Revenue OS.

Provides:
- Role-based access control (RBAC) decorators for routes
- Facility-level data isolation
- Session access logging
- Sensitive action authorization
- Audit trail integration

No regulatory compliance claims are made. This is a demo prototype.
"""
from __future__ import annotations

import functools, uuid
from typing import Any, Callable, Optional
from flask import request, jsonify, g
from db import get_db, now, audit

# Role hierarchy: higher level can access lower level resources
ROLE_HIERARCHY = {
    "administrator": 5,
    "revenue_cycle_manager": 4,
    "finance_officer": 4,
    "billing_officer": 3,
    "claims_officer": 3,
    "clinical_documentation_reviewer": 2,
    "department_staff": 1,
}

# Roles permitted for sensitive financial operations
SENSITIVE_FINANCIAL_ROLES = {
    "administrator", "revenue_cycle_manager", "finance_officer", "billing_officer",
}

# Roles permitted for write-off approval
WRITE_OFF_APPROVAL_ROLES = {"administrator", "revenue_cycle_manager", "finance_officer"}


def _get_user_role(c, username: str) -> Optional[dict]:
    """Look up user role from user_roles table."""
    row = c.execute(
        "SELECT * FROM user_roles WHERE username=? AND is_active=1", (username,)
    ).fetchone()
    return dict(row) if row else None


def _get_user_facility(c, username: str) -> Optional[str]:
    """Get the facility_id assigned to a user."""
    row = c.execute(
        "SELECT facility_id FROM user_roles WHERE username=? AND is_active=1",
        (username,),
    ).fetchone()
    return row["facility_id"] if row else None


def require_role(allowed_roles):
    """Decorator: require the current user to have one of the allowed roles.

    Usage:
        @app.get("/api/admin/...")
        @require_role(["administrator", "revenue_cycle_manager"])
        def admin_route(): ...
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            username = _get_current_username()
            if not username:
                return jsonify(error="Authentication required"), 401
            c = get_db()
            try:
                user = _get_user_role(c, username)
                if not user:
                    c.close()
                    return jsonify(error="User not found or inactive"), 403
                user_role = user.get("role", "")
                if user_role not in allowed_roles:
                    c.close()
                    return jsonify(
                        error="Insufficient role",
                        required=allowed_roles,
                        current=user_role,
                    ), 403
                g.current_user = username
                g.current_user_role = user_role
                g.current_user_facility = user.get("facility_id")
            finally:
                c.close()
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_financial_authorization(f: Callable) -> Callable:
    """Decorator: require a role permitted for sensitive financial operations."""
    return require_role(SENSITIVE_FINANCIAL_ROLES)(f)


def require_write_off_approval(f: Callable) -> Callable:
    """Decorator: require a role permitted for write-off approval."""
    return require_role(WRITE_OFF_APPROVAL_ROLES)(f)


def _get_current_username() -> Optional[str]:
    """Extract username from request. Supports header X-User or query param ?user=."""
    # Header-based (preferred)
    username = request.headers.get("X-User")
    if username:
        return username
    # Query param fallback
    username = request.args.get("user")
    if username:
        return username
    # JSON body
    try:
        data = request.get_json(silent=True) or {}
        if data.get("actor"):
            return data.get("actor")
    except Exception:
        pass
    return None


def log_access(action: str, resource: str = "", resource_id: str = "",
               status: str = "ok", detail: str = "") -> None:
    """Log an access event to session_access_log and audit_trail."""
    try:
        c = get_db()
        username = _get_current_username() or "system"
        user = _get_user_role(c, username)
        user_role = user.get("role", "") if user else ""
        facility_id = user.get("facility_id") if user else None
        sid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO session_access_log(id,session_id,user_id,username,role,facility_id,action,resource,resource_id,ip_address,user_agent,status,timestamp,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, request.headers.get("X-Session-Id", ""), username,
                username, user_role, facility_id, action, resource, resource_id,
                request.remote_addr or "", request.headers.get("User-Agent", ""),
                status, now(), now(),
            ),
        )
        audit(
            c, username, "user", "Security", action, resource, resource_id,
            input_summary=detail, output_summary={"status": status},
        )
        c.commit()
        c.close()
    except Exception:
        pass


def apply_facility_isolation(query: str, params: list, facility_column: str = "facility_id") -> tuple:
    """Append facility-level WHERE clause to a query if the current user has a facility restriction.

    Returns (modified_query, modified_params).
    """
    username = _get_current_username()
    if not username:
        return query, params
    c = get_db()
    try:
        user = _get_user_role(c, username)
        if not user:
            return query, params
        facility_id = user.get("facility_id")
        if facility_id:
            # Avoid duplicate WHERE
            if "WHERE" in query.upper():
                query += f" AND {facility_column}=?"
            else:
                query += f" WHERE {facility_column}=?"
            params.append(facility_id)
    finally:
        c.close()
    return query, params


def is_demo_data() -> bool:
    """Return True if the current database is the demo database (not a real facility)."""
    from db import DB
    return "pnrcm_demo" in DB.lower()