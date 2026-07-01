"""
src/api/auth_middleware.py
──────────────────────────
Authentication middleware and RBAC decorators.

Every protected endpoint uses one of:
  @require_auth                    → any authenticated active user
  @require_role("admin")           → admin or super_admin
  @require_permission("manage_users") → specific permission check
  @require_target_access           → checks user.allowed_targets

Token passed as:
  Authorization: Bearer <token>
  OR cookie: session_token=<token>
"""

from __future__ import annotations

import functools
import logging
from typing import Callable

from flask import g, jsonify, request

from src.api.user_store import Role, get_token_store, get_user_store

logger = logging.getLogger("migration.auth")


# ════════════════════════════════════════════════════════════════
# TOKEN EXTRACTION
# ════════════════════════════════════════════════════════════════

def _extract_token() -> str | None:
    """Extract bearer token from header or cookie."""
    # Authorization: Bearer <token>
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Fallback: cookie
    return request.cookies.get("session_token")


def _get_current_user():
    """
    Resolve the current user from the request token.
    Returns User or None.
    """
    token = _extract_token()
    if not token:
        return None
    ts      = get_token_store()
    user_id = ts.validate(token)
    if not user_id:
        return None
    us   = get_user_store()
    user = us.get_by_id(user_id)
    if not user or not user.is_active:
        return None
    return user


# ════════════════════════════════════════════════════════════════
# DECORATORS
# ════════════════════════════════════════════════════════════════

def require_auth(f: Callable) -> Callable:
    """Require a valid session token. No role check."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            return jsonify({
                "error": "Authentification requise",
                "code":  "UNAUTHORIZED",
            }), 401
        if user.must_change_password:
            # Allow access only to the change-password endpoint
            if request.endpoint != "change_own_password":
                return jsonify({
                    "error":       "Vous devez changer votre mot de passe avant de continuer",
                    "code":        "MUST_CHANGE_PASSWORD",
                    "redirect_to": "/change-password",
                }), 403
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def require_role(*roles: str) -> Callable:
    """
    Require the user to have one of the specified roles.
    super_admin always passes.

    Usage:
      @require_role("admin", "super_admin")
      @require_role("super_admin")
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            user = g.current_user
            allowed = set(roles) | {Role.SUPER_ADMIN.value}
            if user.role not in allowed:
                logger.warning(
                    f"Access denied: {user.login} (role={user.role}) "
                    f"tried to access {request.endpoint} (requires {roles})"
                )
                return jsonify({
                    "error": "Vous n'avez pas les droits nécessaires pour cette action",
                    "code":  "FORBIDDEN",
                }), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_permission(permission: str) -> Callable:
    """
    Require a specific permission.

    Usage:
      @require_permission("manage_users")
      @require_permission("launch_migration_all")
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            user = g.current_user
            if not user.has_permission(permission):
                logger.warning(
                    f"Permission denied: {user.login} missing '{permission}' "
                    f"on {request.endpoint}"
                )
                return jsonify({
                    "error": f"Permission '{permission}' requise",
                    "code":  "FORBIDDEN",
                }), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def admin_only(f: Callable) -> Callable:
    """Shortcut: requires admin or super_admin role."""
    return require_role(Role.ADMIN.value, Role.SUPER_ADMIN.value)(f)


def super_admin_only(f: Callable) -> Callable:
    """Shortcut: requires super_admin role only."""
    return require_role(Role.SUPER_ADMIN.value)(f)


# ════════════════════════════════════════════════════════════════
# AUDIT LOGGING HELPER
# ════════════════════════════════════════════════════════════════

def audit_log(action: str, detail: str = "", success: bool = True) -> None:
    """Log a security-relevant action with the current user context."""
    user = getattr(g, "current_user", None)
    login = user.login if user else "anonymous"
    ip    = request.remote_addr or "unknown"
    level = "INFO" if success else "WARNING"
    msg   = f"[AUDIT] [{level}] user={login} ip={ip} action={action}"
    if detail:
        msg += f" detail={detail}"
    logger.info(msg)
