"""
tests/unit/test_security.py
────────────────────────────
Tests for RBAC, UserStore, password hashing, and token management.
"""
import os, sys, pytest, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# Override store path to temp file for tests
TEST_STORE = tempfile.mktemp(suffix='.json')
os.environ['USER_STORE_PATH'] = TEST_STORE

from src.api.user_store import (
    UserStore, TokenStore, Role, ROLE_PERMISSIONS,
    _hash_password, verify_password, validate_password_strength,
)


@pytest.fixture(autouse=True)
def fresh_store():
    """Each test gets a clean store."""
    import src.api.user_store as us_mod
    us_mod._user_store  = None
    us_mod._token_store = None
    if os.path.exists(TEST_STORE):
        os.remove(TEST_STORE)
    yield
    if os.path.exists(TEST_STORE):
        os.remove(TEST_STORE)


# ── Password hashing ──────────────────────────────────────────

class TestPasswordHashing:
    def test_hash_is_deterministic_with_same_salt(self):
        h1, s = _hash_password("Test@1234")
        h2, _ = _hash_password("Test@1234", s)
        assert h1 == h2

    def test_different_passwords_different_hashes(self):
        h1, s = _hash_password("Test@1234")
        h2, _ = _hash_password("Other@5678", s)
        assert h1 != h2

    def test_verify_correct_password(self):
        h, s = _hash_password("MyP@ss1!")
        assert verify_password("MyP@ss1!", h, s) is True

    def test_verify_wrong_password(self):
        h, s = _hash_password("MyP@ss1!")
        assert verify_password("WrongPass1!", h, s) is False

    def test_random_salts_produce_different_hashes(self):
        h1, s1 = _hash_password("Same@Pass1")
        h2, s2 = _hash_password("Same@Pass1")
        assert s1 != s2
        assert h1 != h2


# ── Password strength ─────────────────────────────────────────

class TestPasswordStrength:
    def test_valid_password(self):
        ok, _ = validate_password_strength("Orange@2025")
        assert ok is True

    def test_too_short(self):
        ok, msg = validate_password_strength("A1!")
        assert ok is False
        assert "8" in msg

    def test_no_uppercase(self):
        ok, _ = validate_password_strength("orange@2025")
        assert ok is False

    def test_no_digit(self):
        ok, _ = validate_password_strength("Orange@Pass!")
        assert ok is False

    def test_no_special(self):
        ok, _ = validate_password_strength("Orange2025")
        assert ok is False

    def test_default_admin_password_is_strong(self):
        ok, _ = validate_password_strength("DomSys#gos26")
        assert ok is True


# ── UserStore ─────────────────────────────────────────────────

class TestUserStore:
    def test_default_admin_created_on_init(self):
        us = UserStore(TEST_STORE)
        admin = us.get_by_login("Gos_Cloud")
        assert admin is not None
        assert admin.role == Role.SUPER_ADMIN.value

    def test_default_admin_password(self):
        us = UserStore(TEST_STORE)
        user, err = us.authenticate("Gos_Cloud", "DomSys#gos26")
        assert user is not None
        assert err == ""

    def test_create_user_success(self):
        us = UserStore(TEST_STORE)
        user, err = us.create_user(
            login="j.dupont", cuid="OR123456",
            first_name="Jean", last_name="Dupont",
            function="Ingénieur Cloud",
            role="operator", password="Test@1234",
        )
        assert user is not None
        assert err == ""
        assert user.cuid == "OR123456"
        assert user.must_change_password is True

    def test_duplicate_login_rejected(self):
        us = UserStore(TEST_STORE)
        us.create_user("user1","CU001","A","B","F","operator","Pass@1234")
        _, err = us.create_user("user1","CU002","C","D","F","operator","Pass@5678")
        assert err != ""
        assert "existe" in err.lower()

    def test_duplicate_cuid_rejected(self):
        us = UserStore(TEST_STORE)
        us.create_user("u1","OR001","A","B","F","operator","Pass@1234")
        _, err = us.create_user("u2","OR001","C","D","F","operator","Pass@5678")
        assert err != ""

    def test_weak_password_rejected(self):
        us = UserStore(TEST_STORE)
        _, err = us.create_user("u3","CU003","A","B","F","operator","weak")
        assert err != ""

    def test_invalid_role_rejected(self):
        us = UserStore(TEST_STORE)
        _, err = us.create_user("u4","CU004","A","B","F","god_mode","Pass@1234")
        assert err != ""

    def test_authenticate_wrong_password(self):
        us = UserStore(TEST_STORE)
        us.create_user("u5","CU005","A","B","F","operator","Correct@1!")
        user, err = us.authenticate("u5","Wrong@pass1!")
        assert user is None
        assert err != ""

    def test_authenticate_inactive_user(self):
        us = UserStore(TEST_STORE)
        us.create_user("u6","CU006","A","B","F","operator","Pass@1234")
        us.update_user("u6","Gos_Cloud", is_active=False)
        user, err = us.authenticate("u6","Pass@1234")
        assert user is None
        assert "désactivé" in err.lower()

    def test_admin_set_password_forces_change(self):
        us = UserStore(TEST_STORE)
        us.create_user("u7","CU007","A","B","F","operator","Pass@1234",must_change_password=False)
        ok, _ = us.set_password_by_admin("u7","NewPass@9!", must_change=True)
        assert ok is True
        user = us.get_by_login("u7")
        assert user.must_change_password is True

    def test_cannot_delete_default_admin(self):
        us = UserStore(TEST_STORE)
        ok, msg = us.delete_user("Gos_Cloud")
        assert ok is False
        assert "default" in msg.lower() or "défaut" in msg.lower() or "supprimé" not in msg.lower()

    def test_change_password_clears_must_change(self):
        us = UserStore(TEST_STORE)
        us.create_user("u8","CU008","A","B","F","operator","OldPass@1!",must_change_password=True)
        us.change_password("u8","NewPass@2!", force_change=True)
        user = us.get_by_login("u8")
        assert user.must_change_password is False


# ── RBAC permissions ──────────────────────────────────────────

class TestRBAC:
    def test_super_admin_has_all_permissions(self):
        us = UserStore(TEST_STORE)
        admin = us.get_by_login("Gos_Cloud")
        for perm in ["manage_users","manage_platforms","launch_migration_all","assign_roles"]:
            assert admin.has_permission(perm), f"super_admin missing: {perm}"

    def test_read_only_has_limited_permissions(self):
        us = UserStore(TEST_STORE)
        us.create_user("ro","CU_RO","A","B","F","read_only","Pass@1234")
        user = us.get_by_login("ro")
        assert user.has_permission("view_logs") is True
        assert user.has_permission("launch_migration_all") is False
        assert user.has_permission("manage_users") is False

    def test_operator_cannot_manage_users(self):
        us = UserStore(TEST_STORE)
        us.create_user("op","CU_OP","A","B","F","operator","Pass@1234")
        user = us.get_by_login("op")
        assert user.has_permission("manage_users") is False
        assert user.has_permission("launch_migration_own") is True

    def test_allowed_targets_empty_means_all(self):
        us = UserStore(TEST_STORE)
        us.create_user("t1","CU_T1","A","B","F","operator","Pass@1234",allowed_targets=[])
        user = us.get_by_login("t1")
        assert user.can_use_target("redhat") is True
        assert user.can_use_target("huawei") is True

    def test_allowed_targets_restriction(self):
        us = UserStore(TEST_STORE)
        us.create_user("t2","CU_T2","A","B","F","operator","Pass@1234",allowed_targets=["redhat"])
        user = us.get_by_login("t2")
        assert user.can_use_target("redhat") is True
        assert user.can_use_target("huawei") is False


# ── TokenStore ────────────────────────────────────────────────

class TestTokenStore:
    def test_create_and_validate(self):
        ts = TokenStore(ttl_seconds=3600)
        token = ts.create("user-abc")
        uid = ts.validate(token)
        assert uid == "user-abc"

    def test_invalid_token_returns_none(self):
        ts = TokenStore()
        assert ts.validate("bogus-token-xyz") is None

    def test_revoke_invalidates_token(self):
        ts = TokenStore()
        tok = ts.create("user-123")
        ts.revoke(tok)
        assert ts.validate(tok) is None

    def test_revoke_all_for_user(self):
        ts = TokenStore()
        t1 = ts.create("user-x")
        t2 = ts.create("user-x")
        t3 = ts.create("user-y")
        ts.revoke_all_for_user("user-x")
        assert ts.validate(t1) is None
        assert ts.validate(t2) is None
        assert ts.validate(t3) == "user-y"

    def test_expired_token_returns_none(self):
        ts = TokenStore(ttl_seconds=0)
        import time; time.sleep(0.01)
        tok = ts.create("user-exp")
        assert ts.validate(tok) is None
