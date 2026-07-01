"""
src/api/user_store.py
──────────────────────
User model, roles, and persistent JSON-based user store.

No external dependencies — uses hashlib (PBKDF2-HMAC-SHA256) for
password hashing, secrets for token generation.

Default admin account created on first startup:
  login    : Gos_Cloud
  password : DomSys#gos26
  role     : super_admin
  must_change_password: False (admin sets his own first)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

# ─── File path for persistent user store ───────────────────────
_STORE_PATH = os.environ.get(
    "USER_STORE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "users.json"),
)


# ════════════════════════════════════════════════════════════════
# ENUMS
# ════════════════════════════════════════════════════════════════

class Role(Enum):
    SUPER_ADMIN  = "super_admin"   # Full access — manage users, platforms, everything
    ADMIN        = "admin"         # Launch migrations, manage jobs, assign roles
    OPERATOR     = "operator"      # Launch migrations on assigned platforms only
    READ_ONLY    = "read_only"     # View only — no actions


# Permission map: role → set of allowed permissions
ROLE_PERMISSIONS: Dict[str, set] = {
    Role.SUPER_ADMIN.value: {
        "manage_users",
        "manage_platforms",
        "launch_migration_all",
        "launch_migration_own",
        "post_migration_actions",
        "view_all_jobs",
        "view_own_jobs",
        "view_reports",
        "view_logs",
        "assign_roles",
        "modify_passwords",
    },
    Role.ADMIN.value: {
        "launch_migration_all",
        "launch_migration_own",
        "post_migration_actions",
        "view_all_jobs",
        "view_own_jobs",
        "view_reports",
        "view_logs",
    },
    Role.OPERATOR.value: {
        "launch_migration_own",
        "post_migration_actions_limited",
        "view_own_jobs",
        "view_reports",
        "view_logs",
    },
    Role.READ_ONLY.value: {
        "view_own_jobs",
        "view_reports",
        "view_logs",
    },
}


# ════════════════════════════════════════════════════════════════
# USER MODEL
# ════════════════════════════════════════════════════════════════

class User:
    """
    Represents a platform user.

    Fields:
      user_id             : UUID
      login               : unique login identifier
      cuid                : Orange Group professional ID (e.g. OR123456)
      first_name          : prénom
      last_name           : nom
      function            : job title / function
      role                : Role enum value
      password_hash       : PBKDF2-HMAC-SHA256 hash
      salt                : random salt for this user
      is_active           : account enabled/disabled
      must_change_password: force password change on next login
      allowed_targets     : list of OpenStack target IDs this user can use
                            (empty = all targets)
      created_at          : ISO timestamp
      created_by          : login of creator
      last_login          : ISO timestamp or None
    """

    def __init__(
        self,
        login: str,
        cuid: str,
        first_name: str,
        last_name: str,
        function: str,
        role: str = Role.OPERATOR.value,
        is_active: bool = True,
        must_change_password: bool = True,
        allowed_targets: Optional[List[str]] = None,
        user_id: Optional[str] = None,
        created_by: str = "system",
        created_at: Optional[str] = None,
        last_login: Optional[str] = None,
        password_hash: str = "",
        salt: str = "",
    ):
        self.user_id              = user_id or str(uuid.uuid4())
        self.login                = login.strip()
        self.cuid                 = cuid.strip().upper()
        self.first_name           = first_name.strip()
        self.last_name            = last_name.strip()
        self.function             = function.strip()
        self.role                 = role
        self.is_active            = is_active
        self.must_change_password = must_change_password
        self.allowed_targets      = allowed_targets or []
        self.created_by           = created_by
        self.created_at           = created_at or datetime.utcnow().isoformat()
        self.last_login           = last_login
        self.password_hash        = password_hash
        self.salt                 = salt

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def has_permission(self, permission: str) -> bool:
        perms = ROLE_PERMISSIONS.get(self.role, set())
        return permission in perms

    def can_use_target(self, target_id: str) -> bool:
        """Empty allowed_targets means all targets are allowed."""
        if not self.allowed_targets:
            return True
        return target_id in self.allowed_targets

    def to_dict(self, include_sensitive: bool = False) -> dict:
        d = {
            "user_id":               self.user_id,
            "login":                 self.login,
            "cuid":                  self.cuid,
            "first_name":            self.first_name,
            "last_name":             self.last_name,
            "full_name":             self.full_name,
            "function":              self.function,
            "role":                  self.role,
            "is_active":             self.is_active,
            "must_change_password":  self.must_change_password,
            "allowed_targets":       self.allowed_targets,
            "created_by":            self.created_by,
            "created_at":            self.created_at,
            "last_login":            self.last_login,
        }
        if include_sensitive:
            d["password_hash"] = self.password_hash
            d["salt"]          = self.salt
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        return cls(**{k: v for k, v in data.items() if k in cls.__init__.__code__.co_varnames})


# ════════════════════════════════════════════════════════════════
# PASSWORD UTILITIES
# ════════════════════════════════════════════════════════════════

def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    Hash a password using PBKDF2-HMAC-SHA256 with 260 000 iterations.
    Returns (hash_hex, salt_hex).
    """
    if salt is None:
        salt = secrets.token_hex(32)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=260_000,
    )
    return dk.hex(), salt


def verify_password(password: str, hash_hex: str, salt: str) -> bool:
    """Constant-time password verification."""
    computed, _ = _hash_password(password, salt)
    return hmac.compare_digest(computed, hash_hex)


def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    Enforce password policy:
      - Min 8 characters
      - At least 1 uppercase
      - At least 1 digit
      - At least 1 special character
    """
    if len(password) < 8:
        return False, "Le mot de passe doit contenir au moins 8 caractères"
    if not any(c.isupper() for c in password):
        return False, "Le mot de passe doit contenir au moins une majuscule"
    if not any(c.isdigit() for c in password):
        return False, "Le mot de passe doit contenir au moins un chiffre"
    if not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password):
        return False, "Le mot de passe doit contenir au moins un caractère spécial"
    return True, "OK"


# ════════════════════════════════════════════════════════════════
# SESSION TOKENS (simple secure token store)
# ════════════════════════════════════════════════════════════════

class TokenStore:
    """
    In-memory token store.
    Maps token → {user_id, expires_at, user_agent}.
    In production, replace with Redis.
    """
    def __init__(self, ttl_seconds: int = 28800):  # 8 hours
        self._tokens: Dict[str, dict] = {}
        self._ttl = ttl_seconds

    def create(self, user_id: str, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(48)
        from datetime import timezone, timedelta
        expires = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        self._tokens[token] = {
            "user_id":    user_id,
            "expires_at": expires.isoformat(),
            "user_agent": user_agent,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return token

    def validate(self, token: str) -> Optional[str]:
        """Returns user_id if token is valid and not expired, else None."""
        entry = self._tokens.get(token)
        if not entry:
            return None
        from datetime import timezone
        expires = datetime.fromisoformat(entry["expires_at"])
        if datetime.now(timezone.utc) > expires:
            del self._tokens[token]
            return None
        return entry["user_id"]

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def revoke_all_for_user(self, user_id: str) -> int:
        to_delete = [t for t, e in self._tokens.items() if e["user_id"] == user_id]
        for t in to_delete:
            del self._tokens[t]
        return len(to_delete)


# ════════════════════════════════════════════════════════════════
# USER STORE
# ════════════════════════════════════════════════════════════════

class UserStore:
    """
    Persistent user store backed by a JSON file.
    Thread-safe for single-process deployments.

    Default admin account seeded on first run:
      login    : Gos_Cloud
      password : DomSys#gos26
      role     : super_admin
    """

    DEFAULT_ADMIN_LOGIN    = "Gos_Cloud"
    DEFAULT_ADMIN_PASSWORD = "DomSys#gos26"

    def __init__(self, path: str = _STORE_PATH):
        self._path = os.path.abspath(path)
        self._users: Dict[str, User] = {}   # login → User
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for u in data.get("users", []):
                    user = User.from_dict(u)
                    self._users[user.login] = user
            except Exception as exc:
                import logging
                logging.getLogger("migration.auth").warning(f"Could not load user store: {exc}")

        # Seed default admin if no users exist
        if not self._users:
            self._seed_default_admin()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {
            "version":    "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "users":      [u.to_dict(include_sensitive=True) for u in self._users.values()],
        }
        # Atomic write
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    def _seed_default_admin(self) -> None:
        h, s = _hash_password(self.DEFAULT_ADMIN_PASSWORD)
        admin = User(
            login                = self.DEFAULT_ADMIN_LOGIN,
            cuid                 = "ADMIN001",
            first_name           = "Platform",
            last_name            = "Administrator",
            function             = "Super Administrateur Plateforme",
            role                 = Role.SUPER_ADMIN.value,
            is_active            = True,
            must_change_password = False,
            allowed_targets      = [],
            created_by           = "system",
            password_hash        = h,
            salt                 = s,
        )
        self._users[admin.login] = admin
        self._save()

    # ── CRUD ──────────────────────────────────────────────────

    def create_user(
        self,
        login: str,
        cuid: str,
        first_name: str,
        last_name: str,
        function: str,
        role: str,
        password: str,
        allowed_targets: Optional[List[str]] = None,
        created_by: str = "system",
        must_change_password: bool = True,
    ) -> tuple[Optional[User], str]:
        """
        Create a new user. Returns (user, error_message).
        error_message is empty string on success.
        """
        if login in self._users:
            return None, f"Le login '{login}' existe déjà"

        if cuid and any(u.cuid == cuid.upper() for u in self._users.values()):
            return None, f"Le CUID '{cuid}' est déjà utilisé"

        if role not in [r.value for r in Role]:
            return None, f"Rôle invalide: {role}"

        ok, msg = validate_password_strength(password)
        if not ok:
            return None, msg

        h, s = _hash_password(password)
        user = User(
            login                = login,
            cuid                 = cuid,
            first_name           = first_name,
            last_name            = last_name,
            function             = function,
            role                 = role,
            is_active            = True,
            must_change_password = must_change_password,
            allowed_targets      = allowed_targets or [],
            created_by           = created_by,
            password_hash        = h,
            salt                 = s,
        )
        self._users[login] = user
        self._save()
        return user, ""

    def get_by_login(self, login: str) -> Optional[User]:
        return self._users.get(login)

    def get_by_id(self, user_id: str) -> Optional[User]:
        return next((u for u in self._users.values() if u.user_id == user_id), None)

    def list_users(self) -> List[User]:
        return list(self._users.values())

    def update_user(
        self,
        login: str,
        updater_login: str,
        **kwargs,
    ) -> tuple[Optional[User], str]:
        """
        Update user fields. Only admins can call this.
        kwargs: first_name, last_name, function, role, is_active,
                allowed_targets, must_change_password
        """
        user = self._users.get(login)
        if not user:
            return None, f"Utilisateur '{login}' introuvable"

        allowed_fields = {
            "first_name", "last_name", "function", "role",
            "is_active", "allowed_targets", "must_change_password",
        }
        for k, v in kwargs.items():
            if k in allowed_fields:
                if k == "role" and v not in [r.value for r in Role]:
                    return None, f"Rôle invalide: {v}"
                setattr(user, k, v)

        self._save()
        return user, ""

    def change_password(
        self,
        login: str,
        new_password: str,
        force_change: bool = False,
    ) -> tuple[bool, str]:
        """
        Change user password.
        force_change=True sets must_change_password=False after change.
        """
        user = self._users.get(login)
        if not user:
            return False, "Utilisateur introuvable"

        ok, msg = validate_password_strength(new_password)
        if not ok:
            return False, msg

        h, s = _hash_password(new_password)
        user.password_hash        = h
        user.salt                 = s
        user.must_change_password = False if force_change else user.must_change_password
        self._save()
        return True, "Mot de passe modifié avec succès"

    def set_password_by_admin(
        self,
        login: str,
        new_password: str,
        must_change: bool = True,
    ) -> tuple[bool, str]:
        """Admin sets a password for a user. User must change it on next login."""
        user = self._users.get(login)
        if not user:
            return False, "Utilisateur introuvable"

        ok, msg = validate_password_strength(new_password)
        if not ok:
            return False, msg

        h, s = _hash_password(new_password)
        user.password_hash        = h
        user.salt                 = s
        user.must_change_password = must_change
        self._save()
        return True, "Mot de passe défini — l'utilisateur devra le changer à la prochaine connexion"

    def authenticate(self, login: str, password: str) -> tuple[Optional[User], str]:
        """
        Authenticate a user. Returns (user, error_message).
        Updates last_login on success.
        """
        user = self._users.get(login)
        if not user:
            return None, "Identifiant ou mot de passe incorrect"
        if not user.is_active:
            return None, "Compte désactivé — contactez votre administrateur"
        if not verify_password(password, user.password_hash, user.salt):
            return None, "Identifiant ou mot de passe incorrect"

        user.last_login = datetime.utcnow().isoformat()
        self._save()
        return user, ""

    def delete_user(self, login: str) -> tuple[bool, str]:
        if login not in self._users:
            return False, f"Utilisateur '{login}' introuvable"
        if login == self.DEFAULT_ADMIN_LOGIN:
            return False, "Le compte administrateur par défaut ne peut pas être supprimé"
        del self._users[login]
        self._save()
        return True, "Utilisateur supprimé"


# ── Singleton instances ────────────────────────────────────────
_user_store:  Optional[UserStore]  = None
_token_store: Optional[TokenStore] = None


def get_user_store() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def get_token_store() -> TokenStore:
    global _token_store
    if _token_store is None:
        _token_store = TokenStore()
    return _token_store
