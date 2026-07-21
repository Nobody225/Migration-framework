"""
src/api/connection_store.py
────────────────────────────
Dynamic connection store for vCenters and OpenStack environments.

Instead of hardcoding credentials in config.yaml, operators discover
and register connections through the dashboard UI.

Connections are persisted in config/connections.json with passwords
obfuscated (XOR + base64 — not cryptographic, but avoids plaintext).
For production, set CONNECTION_STORE_KEY env var to a 32-char secret.

Connection lifecycle:
  1. Operator enters IP/URL  → ping/port check
  2. Enters credentials      → actual auth test
  3. If success              → saved + available in migration form
  4. Dashboard shows status  → green/red badge per connection
"""

from __future__ import annotations

import base64
import json
import os
import socket
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

_STORE_PATH = os.environ.get(
    "CONNECTION_STORE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "connections.json"),
)

# Simple XOR obfuscation key (not encryption — use vault for real secrets)
_KEY = os.environ.get("CONNECTION_STORE_KEY", "OrangeMigration2025SecretKey!!!!").encode()


def _obfuscate(plaintext: str) -> str:
    """XOR + base64 — keeps passwords out of plaintext JSON."""
    if not plaintext:
        return ""
    data = plaintext.encode("utf-8")
    key  = (_KEY * (len(data) // len(_KEY) + 1))[:len(data)]
    return base64.b64encode(bytes(a ^ b for a, b in zip(data, key))).decode()


def _deobfuscate(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        data = base64.b64decode(encoded.encode())
        key  = (_KEY * (len(data) // len(_KEY) + 1))[:len(data)]
        return bytes(a ^ b for a, b in zip(data, key)).decode("utf-8")
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════════════════════

class VCenterConnection:
    def __init__(
        self,
        name: str,
        host: str,
        port: int = 443,
        username: str = "",
        datacenter: str = "",
        ssl_verify: bool = False,
        conn_id: Optional[str] = None,
        status: str = "unknown",        # unknown | reachable | connected | error
        last_tested: Optional[str] = None,
        last_error: str = "",
        vm_count: int = 0,
        host_count: int = 0,
        added_by: str = "system",
        added_at: Optional[str] = None,
        _password_enc: str = "",
        **kwargs,
    ):
        self.conn_id       = conn_id or str(uuid.uuid4())
        self.name          = name.strip()
        self.host          = host.strip()
        self.port          = port
        self.username      = username.strip()
        self.datacenter    = datacenter.strip()
        self.ssl_verify    = ssl_verify
        self.status        = status
        self.last_tested   = last_tested
        self.last_error    = last_error
        self.vm_count      = vm_count
        self.host_count    = host_count
        self.added_by      = added_by
        self.added_at      = added_at or datetime.utcnow().isoformat()
        self._password_enc = _password_enc

    @property
    def password(self) -> str:
        return _deobfuscate(self._password_enc)

    @password.setter
    def password(self, value: str):
        self._password_enc = _obfuscate(value)

    def to_dict(self, include_password: bool = False) -> dict:
        d = {
            "conn_id":     self.conn_id,
            "type":        "vcenter",
            "name":        self.name,
            "host":        self.host,
            "port":        self.port,
            "username":    self.username,
            "datacenter":  self.datacenter,
            "ssl_verify":  self.ssl_verify,
            "status":      self.status,
            "last_tested": self.last_tested,
            "last_error":  self.last_error,
            "vm_count":    self.vm_count,
            "host_count":  self.host_count,
            "added_by":    self.added_by,
            "added_at":    self.added_at,
        }
        if include_password:
            d["_password_enc"] = self._password_enc
        return d

    def to_vmware_config(self) -> dict:
        """Return a dict compatible with VMwareExtractor config."""
        return {
            "id":           self.conn_id,
            "host":         self.host,
            "port":         self.port,
            "username":     self.username,
            "password":     self.password,
            "datacenter":   self.datacenter,
            "ssl_verify":   self.ssl_verify,
            "metrics_interval_s": 300,
        }


class OpenStackConnection:
    def __init__(
        self,
        name: str,
        auth_url: str,
        project_name: str = "",
        username: str = "",
        user_domain_name: str = "Default",
        project_domain_name: str = "Default",
        region_name: str = "RegionOne",
        ssl_verify: bool = True,
        os_type: str = "custom",        # custom | redhat | huawei
        # Huawei specific
        enterprise_project_id: str = "",
        volume_type: str = "",
        availability_zone: str = "nova",
        external_network: str = "public",
        conn_id: Optional[str] = None,
        status: str = "unknown",
        last_tested: Optional[str] = None,
        last_error: str = "",
        added_by: str = "system",
        added_at: Optional[str] = None,
        _password_enc: str = "",
        **kwargs,
    ):
        self.conn_id               = conn_id or str(uuid.uuid4())
        self.name                  = name.strip()
        self.auth_url              = auth_url.strip().rstrip("/")
        self.project_name          = project_name.strip()
        self.username              = username.strip()
        self.user_domain_name      = user_domain_name
        self.project_domain_name   = project_domain_name
        self.region_name           = region_name
        self.ssl_verify            = ssl_verify
        self.os_type               = os_type
        self.enterprise_project_id = enterprise_project_id
        self.volume_type           = volume_type
        self.availability_zone     = availability_zone
        self.external_network      = external_network
        self.status                = status
        self.last_tested           = last_tested
        self.last_error            = last_error
        self.added_by              = added_by
        self.added_at              = added_at or datetime.utcnow().isoformat()
        self._password_enc         = _password_enc

    @property
    def password(self) -> str:
        return _deobfuscate(self._password_enc)

    @password.setter
    def password(self, value: str):
        self._password_enc = _obfuscate(value)

    def to_dict(self, include_password: bool = False) -> dict:
        d = {
            "conn_id":               self.conn_id,
            "type":                  "openstack",
            "name":                  self.name,
            "auth_url":              self.auth_url,
            "project_name":          self.project_name,
            "username":              self.username,
            "user_domain_name":      self.user_domain_name,
            "project_domain_name":   self.project_domain_name,
            "region_name":           self.region_name,
            "ssl_verify":            self.ssl_verify,
            "os_type":               self.os_type,
            "enterprise_project_id": self.enterprise_project_id,
            "volume_type":           self.volume_type,
            "availability_zone":     self.availability_zone,
            "external_network":      self.external_network,
            "status":                self.status,
            "last_tested":           self.last_tested,
            "last_error":            self.last_error,
            "added_by":              self.added_by,
            "added_at":              self.added_at,
        }
        if include_password:
            d["_password_enc"] = self._password_enc
        return d

    def to_openstack_config(self) -> dict:
        """Return a dict compatible with AdapterFactory config."""
        base = {
            "auth_url":              self.auth_url,
            "project_name":          self.project_name,
            "username":              self.username,
            "password":              self.password,
            "user_domain_name":      self.user_domain_name,
            "project_domain_name":   self.project_domain_name,
            "region_name":           self.region_name,
            "ssl_verify":            self.ssl_verify,
            "type":                  self.os_type,
        }
        if self.os_type == "huawei":
            base["huawei"] = {
                "enterprise_project_id": self.enterprise_project_id,
                "volume_type":           self.volume_type or "SAS",
                "availability_zone":     self.availability_zone,
            }
        elif self.os_type == "redhat":
            base["redhat"] = {
                "availability_zone": self.availability_zone,
                "volume_type":       self.volume_type or "",
            }
        else:
            base["custom"] = {
                "volume_type":       self.volume_type,
                "availability_zone": self.availability_zone,
                "external_network":  self.external_network,
            }
        return base


# ════════════════════════════════════════════════════════════════
# CONNECTIVITY TESTS (no auth — just reachability)
# ════════════════════════════════════════════════════════════════

def ping_host(host: str, port: int = 443, timeout: float = 5.0) -> tuple[bool, str]:
    """
    TCP port check — no auth required.
    Returns (reachable, latency_ms or error_message).
    """
    import time
    try:
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            ms = round((time.monotonic() - start) * 1000)
            return True, f"{ms}ms"
    except socket.timeout:
        return False, f"Timeout ({timeout}s) — hôte injoignable"
    except ConnectionRefusedError:
        return False, f"Port {port} refusé — vérifiez le port et le pare-feu"
    except OSError as e:
        return False, str(e)


def test_vcenter_auth(host: str, port: int, username: str, password: str,
                      ssl_verify: bool = False) -> tuple[bool, str, dict]:
    """
    Try a real vCenter connection.
    Returns (success, message, info_dict).
    """
    try:
        from pyVim.connect import SmartConnect, Disconnect
        if ssl_verify:
            si = SmartConnect(host=host, port=port, user=username, pwd=password)
        else:
            import ssl; context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.check_hostname = False; context.verify_mode = ssl.CERT_NONE; si = SmartConnect(host=host, port=port, user=username, pwd=password, sslContext=context)

        content = si.RetrieveContent()

        # Collect basic info
        datacenters = []
        from pyVmomi import vim
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datacenter], True
        )
        for dc in container.view:
            datacenters.append(dc.name)
        container.Destroy()

        about = content.about
        info  = {
            "version":     about.version,
            "build":       about.build,
            "fullName":    about.fullName,
            "datacenters": datacenters,
        }
        Disconnect(si)
        return True, f"Connexion réussie — vCenter {about.version}", info

    except Exception as exc:
        return False, str(exc), {}


def test_openstack_auth(auth_url: str, project_name: str, username: str,
                        password: str, user_domain: str = "Default",
                        project_domain: str = "Default", region: str = "RegionOne",
                        ssl_verify: bool = True) -> tuple[bool, str, dict]:
    """
    Try a real Keystone authentication.
    Returns (success, message, info_dict).
    """
    try:
        import openstack
        conn = openstack.connect(
            auth_url=auth_url,
            project_name=project_name,
            username=username,
            password=password,
            user_domain_name=user_domain,
            project_domain_name=project_domain,
            region_name=region,
            verify=ssl_verify,
        )
        # Trigger auth
        token = conn.authorize()

        # Collect info
        networks = list(conn.network.networks())
        flavors  = list(conn.compute.flavors())
        limits   = conn.compute.get_limits()
        abs_l    = limits.get("absolute", {})

        info = {
            "networks": [n.name for n in networks[:10]],
            "flavors":  [{"name": f.name, "vcpus": f.vcpus, "ram": f.ram} for f in flavors[:10]],
            "quota": {
                "max_instances": abs_l.get("maxTotalInstances", 0),
                "used_instances": abs_l.get("totalInstancesUsed", 0),
                "max_vcpus": abs_l.get("maxTotalCores", 0),
                "used_vcpus": abs_l.get("totalCoresUsed", 0),
                "max_ram_mb": abs_l.get("maxTotalRAMSize", 0),
                "used_ram_mb": abs_l.get("totalRAMUsed", 0),
            }
        }
        conn.close()
        return True, "Authentification Keystone réussie", info

    except Exception as exc:
        return False, str(exc), {}


# ════════════════════════════════════════════════════════════════
# CONNECTION STORE
# ════════════════════════════════════════════════════════════════

class ConnectionStore:
    """
    Persistent store for dynamically discovered vCenter and OpenStack connections.
    Replaces the static vcenters/openstack sections in config.yaml.
    """

    def __init__(self, path: str = _STORE_PATH):
        self._path = os.path.abspath(path)
        self._vcenters:   Dict[str, VCenterConnection]   = {}
        self._openstacks: Dict[str, OpenStackConnection] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for vc in data.get("vcenters", []):
                c = VCenterConnection(**vc)
                self._vcenters[c.conn_id] = c
            for os_ in data.get("openstacks", []):
                c = OpenStackConnection(**os_)
                self._openstacks[c.conn_id] = c
        except Exception as e:
            import logging
            logging.getLogger("migration.connections").warning(f"Load error: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {
            "version":    "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "vcenters":   [c.to_dict(include_password=True) for c in self._vcenters.values()],
            "openstacks": [c.to_dict(include_password=True) for c in self._openstacks.values()],
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ── vCenter CRUD ──────────────────────────────────────────

    def add_vcenter(self, name: str, host: str, port: int = 443,
                    username: str = "", password: str = "",
                    datacenter: str = "", ssl_verify: bool = False,
                    added_by: str = "system") -> VCenterConnection:
        conn = VCenterConnection(
            name=name, host=host, port=port,
            username=username, datacenter=datacenter,
            ssl_verify=ssl_verify, added_by=added_by,
        )
        conn.password = password
        self._vcenters[conn.conn_id] = conn
        self._save()
        return conn

    def update_vcenter(self, conn_id: str, **kwargs) -> Optional[VCenterConnection]:
        conn = self._vcenters.get(conn_id)
        if not conn:
            return None
        password = kwargs.pop("password", None)
        for k, v in kwargs.items():
            if hasattr(conn, k):
                setattr(conn, k, v)
        if password:
            conn.password = password
        self._save()
        return conn

    def delete_vcenter(self, conn_id: str) -> bool:
        if conn_id not in self._vcenters:
            return False
        del self._vcenters[conn_id]
        self._save()
        return True

    def get_vcenter(self, conn_id: str) -> Optional[VCenterConnection]:
        return self._vcenters.get(conn_id)

    def list_vcenters(self) -> List[VCenterConnection]:
        return list(self._vcenters.values())

    # ── OpenStack CRUD ────────────────────────────────────────

    def add_openstack(self, name: str, auth_url: str, project_name: str = "",
                      username: str = "", password: str = "",
                      user_domain_name: str = "Default",
                      project_domain_name: str = "Default",
                      region_name: str = "RegionOne",
                      ssl_verify: bool = True, os_type: str = "custom",
                      added_by: str = "system", **kwargs) -> OpenStackConnection:
        conn = OpenStackConnection(
            name=name, auth_url=auth_url, project_name=project_name,
            username=username, user_domain_name=user_domain_name,
            project_domain_name=project_domain_name, region_name=region_name,
            ssl_verify=ssl_verify, os_type=os_type, added_by=added_by, **kwargs,
        )
        conn.password = password
        self._openstacks[conn.conn_id] = conn
        self._save()
        return conn

    def update_openstack(self, conn_id: str, **kwargs) -> Optional[OpenStackConnection]:
        conn = self._openstacks.get(conn_id)
        if not conn:
            return None
        password = kwargs.pop("password", None)
        for k, v in kwargs.items():
            if hasattr(conn, k):
                setattr(conn, k, v)
        if password:
            conn.password = password
        self._save()
        return conn

    def delete_openstack(self, conn_id: str) -> bool:
        if conn_id not in self._openstacks:
            return False
        del self._openstacks[conn_id]
        self._save()
        return True

    def get_openstack(self, conn_id: str) -> Optional[OpenStackConnection]:
        return self._openstacks.get(conn_id)

    def list_openstacks(self) -> List[OpenStackConnection]:
        return list(self._openstacks.values())

    # ── Status update ──────────────────────────────────────────

    def set_vcenter_status(self, conn_id: str, status: str, error: str = "",
                            vm_count: int = 0, host_count: int = 0):
        conn = self._vcenters.get(conn_id)
        if conn:
            conn.status      = status
            conn.last_error  = error
            conn.last_tested = datetime.utcnow().isoformat()
            conn.vm_count    = vm_count
            conn.host_count  = host_count
            self._save()

    def set_openstack_status(self, conn_id: str, status: str, error: str = ""):
        conn = self._openstacks.get(conn_id)
        if conn:
            conn.status      = status
            conn.last_error  = error
            conn.last_tested = datetime.utcnow().isoformat()
            self._save()


# ── Singleton ─────────────────────────────────────────────────
_conn_store: Optional[ConnectionStore] = None

def get_connection_store() -> ConnectionStore:
    global _conn_store
    if _conn_store is None:
        _conn_store = ConnectionStore()
    return _conn_store
