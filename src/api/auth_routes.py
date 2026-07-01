"""
src/api/auth_routes.py
───────────────────────
Authentication endpoints.

POST /api/v1/auth/login          → login (login + password)
POST /api/v1/auth/logout         → revoke session token
GET  /api/v1/auth/me             → current user info
POST /api/v1/auth/change-password → change own password

Admin-only user management:
GET    /api/v1/admin/users            → list all users
POST   /api/v1/admin/users            → create user
GET    /api/v1/admin/users/<login>    → get user details
PUT    /api/v1/admin/users/<login>    → update user
DELETE /api/v1/admin/users/<login>    → delete user
POST   /api/v1/admin/users/<login>/set-password    → admin sets password
POST   /api/v1/admin/users/<login>/toggle-active   → enable/disable account
POST   /api/v1/admin/users/<login>/reset-password  → force must_change_password
"""

from __future__ import annotations

import logging
from flask import Blueprint, g, jsonify, request

from src.api.auth_middleware import (
    audit_log, admin_only, require_auth, super_admin_only,
)
from src.api.user_store import (
    Role, get_token_store, get_user_store, validate_password_strength,
)

logger = logging.getLogger("migration.auth.routes")
auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1")


# ════════════════════════════════════════════════════════════════
# PUBLIC — LOGIN / LOGOUT
# ════════════════════════════════════════════════════════════════

@auth_bp.route("/auth/login", methods=["POST"])
def login():
    """
    Authenticate and receive a session token.

    Body: { "login": "...", "password": "..." }
    Returns: { "token": "...", "user": {...}, "must_change_password": bool }
    """
    data     = request.get_json(force=True) or {}
    login_id = data.get("login", "").strip()
    password = data.get("password", "")

    if not login_id or not password:
        return jsonify({"error": "Login et mot de passe requis"}), 400

    us   = get_user_store()
    user, err = us.authenticate(login_id, password)

    if not user:
        audit_log("LOGIN_FAILED", f"login={login_id}", success=False)
        return jsonify({"error": err}), 401

    # Create session token
    ts    = get_token_store()
    token = ts.create(user.user_id, request.headers.get("User-Agent", ""))

    audit_log("LOGIN_SUCCESS", f"login={login_id} role={user.role}")

    resp_data = {
        "token":               token,
        "must_change_password": user.must_change_password,
        "user": {
            "user_id":    user.user_id,
            "login":      user.login,
            "cuid":       user.cuid,
            "full_name":  user.full_name,
            "first_name": user.first_name,
            "last_name":  user.last_name,
            "function":   user.function,
            "role":       user.role,
            "allowed_targets": user.allowed_targets,
        },
    }
    return jsonify(resp_data), 200


@auth_bp.route("/auth/logout", methods=["POST"])
@require_auth
def logout():
    """Revoke the current session token."""
    ts    = get_token_store()
    token = _extract_token_from_request()
    if token:
        ts.revoke(token)
    audit_log("LOGOUT")
    return jsonify({"message": "Déconnexion réussie"}), 200


@auth_bp.route("/auth/me", methods=["GET"])
@require_auth
def get_me():
    """Return the current authenticated user's profile."""
    user = g.current_user
    return jsonify(user.to_dict()), 200


@auth_bp.route("/auth/change-password", methods=["POST"])
@require_auth
def change_own_password():
    """
    Allow a user to change their own password.
    Required when must_change_password is True.

    Body: { "current_password": "...", "new_password": "..." }
    """
    data         = request.get_json(force=True) or {}
    current_pwd  = data.get("current_password", "")
    new_pwd      = data.get("new_password", "")

    if not current_pwd or not new_pwd:
        return jsonify({"error": "Mot de passe actuel et nouveau mot de passe requis"}), 400

    user = g.current_user
    us   = get_user_store()

    # Verify current password
    _, err = us.authenticate(user.login, current_pwd)
    if err and "incorrect" in err.lower():
        audit_log("CHANGE_PASSWORD_FAILED", "wrong current password", success=False)
        return jsonify({"error": "Mot de passe actuel incorrect"}), 401

    ok, msg = validate_password_strength(new_pwd)
    if not ok:
        return jsonify({"error": msg}), 400

    if current_pwd == new_pwd:
        return jsonify({"error": "Le nouveau mot de passe doit être différent de l'ancien"}), 400

    us.change_password(user.login, new_pwd, force_change=True)
    audit_log("CHANGE_PASSWORD_SUCCESS")
    return jsonify({"message": "Mot de passe modifié avec succès"}), 200


# ════════════════════════════════════════════════════════════════
# ADMIN — USER MANAGEMENT
# ════════════════════════════════════════════════════════════════

@auth_bp.route("/admin/users", methods=["GET"])
@admin_only
def list_users():
    """List all users. Admin only."""
    us    = get_user_store()
    users = us.list_users()
    return jsonify({
        "count": len(users),
        "users": [u.to_dict() for u in users],
    }), 200


@auth_bp.route("/admin/users", methods=["POST"])
@admin_only
def create_user():
    """
    Create a new user account. Admin only.

    Body:
    {
      "login":       "j.dupont",
      "cuid":        "OR123456",
      "first_name":  "Jean",
      "last_name":   "Dupont",
      "function":    "Ingénieur Cloud",
      "role":        "operator",
      "password":    "Temp@12345",
      "allowed_targets": ["redhat"],   // optional — empty = all
      "must_change_password": true
    }
    """
    data = request.get_json(force=True) or {}

    required = ["login", "cuid", "first_name", "last_name", "function", "role", "password"]
    missing  = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Champs manquants: {', '.join(missing)}"}), 400

    # Only super_admin can create other admins
    creator = g.current_user
    new_role = data.get("role", "operator")
    if new_role in (Role.SUPER_ADMIN.value, Role.ADMIN.value):
        if creator.role != Role.SUPER_ADMIN.value:
            return jsonify({
                "error": "Seul un super_admin peut créer un compte admin"
            }), 403

    us   = get_user_store()
    user, err = us.create_user(
        login                = data["login"],
        cuid                 = data["cuid"],
        first_name           = data["first_name"],
        last_name            = data["last_name"],
        function             = data["function"],
        role                 = new_role,
        password             = data["password"],
        allowed_targets      = data.get("allowed_targets", []),
        created_by           = creator.login,
        must_change_password = data.get("must_change_password", True),
    )

    if not user:
        return jsonify({"error": err}), 400

    audit_log("USER_CREATED", f"new_login={user.login} role={user.role}")
    return jsonify({
        "message": f"Utilisateur '{user.login}' créé avec succès",
        "user":    user.to_dict(),
    }), 201


@auth_bp.route("/admin/users/<login>", methods=["GET"])
@admin_only
def get_user(login: str):
    """Get details of a specific user."""
    us   = get_user_store()
    user = us.get_by_login(login)
    if not user:
        return jsonify({"error": f"Utilisateur '{login}' introuvable"}), 404
    return jsonify(user.to_dict()), 200


@auth_bp.route("/admin/users/<login>", methods=["PUT"])
@admin_only
def update_user(login: str):
    """
    Update user profile/role/targets. Admin only.
    Only super_admin can change roles to admin/super_admin.

    Body: any subset of { first_name, last_name, function, role,
                          is_active, allowed_targets, must_change_password }
    """
    data    = request.get_json(force=True) or {}
    creator = g.current_user
    us      = get_user_store()

    # Prevent privilege escalation
    new_role = data.get("role")
    if new_role and new_role in (Role.SUPER_ADMIN.value, Role.ADMIN.value):
        if creator.role != Role.SUPER_ADMIN.value:
            return jsonify({
                "error": "Seul un super_admin peut attribuer le rôle admin"
            }), 403

    user, err = us.update_user(login, updater_login=creator.login, **data)
    if not user:
        return jsonify({"error": err}), 400

    audit_log("USER_UPDATED", f"login={login} fields={list(data.keys())}")
    return jsonify({
        "message": "Utilisateur mis à jour",
        "user":    user.to_dict(),
    }), 200


@auth_bp.route("/admin/users/<login>", methods=["DELETE"])
@admin_only
def delete_user(login: str):
    """Delete a user account. Admin only. Cannot delete default admin."""
    us      = get_user_store()
    creator = g.current_user

    # Prevent self-deletion
    if login == creator.login:
        return jsonify({"error": "Vous ne pouvez pas supprimer votre propre compte"}), 400

    ok, msg = us.delete_user(login)
    if not ok:
        return jsonify({"error": msg}), 400

    audit_log("USER_DELETED", f"deleted_login={login}")
    return jsonify({"message": msg}), 200


@auth_bp.route("/admin/users/<login>/set-password", methods=["POST"])
@admin_only
def admin_set_password(login: str):
    """
    Admin sets a password for a user.
    User will be required to change it on next login.

    Body: { "new_password": "...", "must_change": true }
    """
    data         = request.get_json(force=True) or {}
    new_password = data.get("new_password", "")
    must_change  = data.get("must_change", True)

    if not new_password:
        return jsonify({"error": "Nouveau mot de passe requis"}), 400

    us      = get_user_store()
    ok, msg = us.set_password_by_admin(login, new_password, must_change=must_change)
    if not ok:
        return jsonify({"error": msg}), 400

    audit_log("ADMIN_SET_PASSWORD", f"target_login={login} must_change={must_change}")
    return jsonify({"message": msg}), 200


@auth_bp.route("/admin/users/<login>/toggle-active", methods=["POST"])
@admin_only
def toggle_active(login: str):
    """Enable or disable a user account."""
    us   = get_user_store()
    user = us.get_by_login(login)
    if not user:
        return jsonify({"error": f"Utilisateur '{login}' introuvable"}), 404
    if login == get_user_store().DEFAULT_ADMIN_LOGIN:
        return jsonify({"error": "Le compte administrateur par défaut ne peut pas être désactivé"}), 400

    new_state = not user.is_active
    us.update_user(login, updater_login=g.current_user.login, is_active=new_state)

    # Revoke all tokens if disabling
    if not new_state:
        get_token_store().revoke_all_for_user(user.user_id)

    action = "ACTIVÉ" if new_state else "DÉSACTIVÉ"
    audit_log(f"USER_{action}", f"login={login}")
    return jsonify({
        "message":   f"Compte {login} {'activé' if new_state else 'désactivé'}",
        "is_active": new_state,
    }), 200


@auth_bp.route("/admin/users/<login>/reset-password", methods=["POST"])
@admin_only
def reset_password_flag(login: str):
    """Force a user to change their password on next login."""
    us   = get_user_store()
    user, err = us.update_user(
        login,
        updater_login=g.current_user.login,
        must_change_password=True,
    )
    if not user:
        return jsonify({"error": err}), 404

    # Revoke all existing sessions so they must re-login
    get_token_store().revoke_all_for_user(user.user_id)

    audit_log("PASSWORD_RESET_FORCED", f"login={login}")
    return jsonify({
        "message": f"{login} devra changer son mot de passe à la prochaine connexion"
    }), 200


@auth_bp.route("/admin/roles", methods=["GET"])
@admin_only
def list_roles():
    """Return available roles and their permissions."""
    from src.api.user_store import ROLE_PERMISSIONS
    return jsonify({
        "roles": [
            {
                "value":       r.value,
                "label":       {
                    "super_admin": "Super Administrateur",
                    "admin":       "Administrateur",
                    "operator":    "Opérateur",
                    "read_only":   "Lecture seule",
                }.get(r.value, r.value),
                "permissions": sorted(list(ROLE_PERMISSIONS.get(r.value, []))),
            }
            for r in Role
        ]
    }), 200


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════

def _extract_token_from_request() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("session_token")
