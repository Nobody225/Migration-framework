"""
src/api/connection_routes.py
─────────────────────────────
REST endpoints for dynamic vCenter and OpenStack discovery.

Workflow for vCenter:
  POST /api/v1/connections/vcenter/ping          → test reachability (no creds)
  POST /api/v1/connections/vcenter/test-auth     → test real auth
  POST /api/v1/connections/vcenter               → save connection
  GET  /api/v1/connections/vcenter               → list saved connections
  PUT  /api/v1/connections/vcenter/<id>          → update
  DELETE /api/v1/connections/vcenter/<id>        → remove
  GET  /api/v1/connections/vcenter/<id>/hosts    → list ESXi
  GET  /api/v1/connections/vcenter/<id>/vms      → list all VMs
  GET  /api/v1/connections/vcenter/<id>/hosts/<mor>/vms → VMs on host

Workflow for OpenStack:
  POST /api/v1/connections/openstack/ping        → test URL reachability
  POST /api/v1/connections/openstack/test-auth   → test Keystone auth
  POST /api/v1/connections/openstack             → save connection
  GET  /api/v1/connections/openstack             → list saved connections
  PUT  /api/v1/connections/openstack/<id>        → update
  DELETE /api/v1/connections/openstack/<id>      → remove
  GET  /api/v1/connections/openstack/<id>/resources → quotas + networks + flavors
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from flask import g, jsonify, request

from src.api.auth_middleware import admin_only, require_auth
from src.api.connection_store import (
    get_connection_store,
    ping_host,
    test_vcenter_auth,
    test_openstack_auth,
)

logger = logging.getLogger("migration.api.connections")


def register_connection_routes(app):

    cs = get_connection_store

    # ════════════════════════════════════════════════════════════
    # LIST ALL — one call for the Infrastructure page
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections")
    @require_auth
    def list_all_connections():
        store = cs()
        return jsonify({
            "vcenters":   [c.to_dict() for c in store.list_vcenters()],
            "openstacks": [c.to_dict() for c in store.list_openstacks()],
        })

    # ════════════════════════════════════════════════════════════
    # VCENTER — PING (step 1)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/vcenter/ping", methods=["POST"])
    @require_auth
    def vcenter_ping():
        """
        Step 1 — Test TCP reachability only (no credentials).
        Body: { "host": "192.168.1.10", "port": 443 }
        """
        data = request.get_json(force=True) or {}
        host = data.get("host", "").strip()
        port = int(data.get("port", 443))

        if not host:
            return jsonify({"error": "host requis"}), 400

        reachable, info = ping_host(host, port)
        return jsonify({
            "host":       host,
            "port":       port,
            "reachable":  reachable,
            "latency":    info if reachable else None,
            "error":      info if not reachable else None,
        })

    # ════════════════════════════════════════════════════════════
    # VCENTER — TEST AUTH (step 2)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/vcenter/test-auth", methods=["POST"])
    @require_auth
    def vcenter_test_auth():
        """
        Step 2 — Test real vCenter authentication.
        Returns vCenter version + datacenter list on success.

        Body: { "host":"...", "port":443, "username":"...",
                "password":"...", "ssl_verify":false }
        """
        data     = request.get_json(force=True) or {}
        host     = data.get("host", "").strip()
        port     = int(data.get("port", 443))
        username = data.get("username", "").strip()
        password = data.get("password", "")
        ssl_v    = data.get("ssl_verify", False)

        if not all([host, username, password]):
            return jsonify({"error": "host, username et password requis"}), 400

        success, message, info = test_vcenter_auth(host, port, username, password, ssl_v)
        return jsonify({
            "success":     success,
            "message":     message,
            "datacenters": info.get("datacenters", []),
            "version":     info.get("version", ""),
            "fullName":    info.get("fullName", ""),
        })

    # ════════════════════════════════════════════════════════════
    # VCENTER — SAVE (step 3)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/vcenter", methods=["GET"])
    @require_auth
    def list_vcenters():
        store = cs()
        return jsonify({
            "count":    len(store.list_vcenters()),
            "vcenters": [c.to_dict() for c in store.list_vcenters()],
        })

    @app.route("/api/v1/connections/vcenter", methods=["POST"])
    @admin_only
    def add_vcenter():
        """
        Save a vCenter connection after successful auth test.

        Body: { "name":"vCenter Prod", "host":"...", "port":443,
                "username":"...", "password":"...",
                "datacenter":"DC-PROD", "ssl_verify":false }
        """
        data = request.get_json(force=True) or {}
        required = ["name", "host", "username", "password"]
        missing  = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Champs requis: {', '.join(missing)}"}), 400

        store = cs()
        conn  = store.add_vcenter(
            name       = data["name"],
            host       = data["host"],
            port       = int(data.get("port", 443)),
            username   = data["username"],
            password   = data["password"],
            datacenter = data.get("datacenter", ""),
            ssl_verify = data.get("ssl_verify", False),
            added_by   = g.current_user.login,
        )
        # Run a quick test to set initial status
        ok, msg, info = test_vcenter_auth(
            conn.host, conn.port, conn.username, conn.password, conn.ssl_verify
        )
        store.set_vcenter_status(
            conn.conn_id,
            status="connected" if ok else "error",
            error="" if ok else msg,
        )
        logger.info(f"vCenter '{conn.name}' ajouté par {g.current_user.login}")
        return jsonify({
            "message":   f"vCenter '{conn.name}' enregistré",
            "vcenter":   conn.to_dict(),
            "connected": ok,
        }), 201

    @app.route("/api/v1/connections/vcenter/<conn_id>", methods=["PUT"])
    @admin_only
    def update_vcenter(conn_id: str):
        data  = request.get_json(force=True) or {}
        store = cs()
        conn  = store.update_vcenter(conn_id, **data)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        return jsonify({"message": "Mis à jour", "vcenter": conn.to_dict()})

    @app.route("/api/v1/connections/vcenter/<conn_id>", methods=["DELETE"])
    @admin_only
    def delete_vcenter(conn_id: str):
        store = cs()
        if not store.delete_vcenter(conn_id):
            return jsonify({"error": "Connexion introuvable"}), 404
        return jsonify({"message": "Connexion vCenter supprimée"})

    @app.route("/api/v1/connections/vcenter/<conn_id>/retest", methods=["POST"])
    @require_auth
    def retest_vcenter(conn_id: str):
        """Re-test an existing vCenter connection."""
        store = cs()
        conn  = store.get_vcenter(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        ok, msg, info = test_vcenter_auth(
            conn.host, conn.port, conn.username, conn.password, conn.ssl_verify
        )
        store.set_vcenter_status(
            conn_id,
            status     = "connected" if ok else "error",
            error      = "" if ok else msg,
        )
        return jsonify({"success": ok, "message": msg, "datacenters": info.get("datacenters", [])})

    # ════════════════════════════════════════════════════════════
    # VCENTER — BROWSE (ESXi + VMs)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/vcenter/<conn_id>/hosts")
    @require_auth
    def conn_vcenter_hosts(conn_id: str):
        """List ESXi hosts from a saved connection."""
        store = cs()
        conn  = store.get_vcenter(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(conn.to_vmware_config())
            ext.connect()
            hosts = ext.list_hosts()
            ext.disconnect()
            store.set_vcenter_status(conn_id, "connected", host_count=len(hosts))
            return jsonify({"conn_id": conn_id, "count": len(hosts), "hosts": hosts})
        except Exception as exc:
            store.set_vcenter_status(conn_id, "error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/connections/vcenter/<conn_id>/vms")
    @require_auth
    def conn_vcenter_vms(conn_id: str):
        """List all VMs from a saved connection."""
        store = cs()
        conn  = store.get_vcenter(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(conn.to_vmware_config())
            ext.connect()
            vms = ext.list_vms()
            ext.disconnect()
            store.set_vcenter_status(conn_id, "connected", vm_count=len(vms))
            return jsonify({"conn_id": conn_id, "count": len(vms),
                            "vms": [v.to_dict() for v in vms]})
        except Exception as exc:
            store.set_vcenter_status(conn_id, "error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/connections/vcenter/<conn_id>/hosts/<host_mor>/vms")
    @require_auth
    def conn_vcenter_host_vms(conn_id: str, host_mor: str):
        """List VMs on a specific ESXi host."""
        store = cs()
        conn  = store.get_vcenter(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(conn.to_vmware_config())
            ext.connect()
            vms = ext.list_vms_on_host(host_mor)
            ext.disconnect()
            return jsonify({"conn_id": conn_id, "host_mor": host_mor,
                            "count": len(vms), "vms": [v.to_dict() for v in vms]})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ════════════════════════════════════════════════════════════
    # OPENSTACK — PING (step 1)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/openstack/ping", methods=["POST"])
    @require_auth
    def openstack_ping():
        """
        Step 1 — Test TCP reachability of Keystone URL.
        Body: { "auth_url": "http://192.168.10.100:5000/v3" }
        """
        data     = request.get_json(force=True) or {}
        auth_url = data.get("auth_url", "").strip()
        if not auth_url:
            return jsonify({"error": "auth_url requis"}), 400

        parsed = urlparse(auth_url)
        host   = parsed.hostname or ""
        port   = parsed.port or (443 if parsed.scheme == "https" else 80)

        if not host:
            return jsonify({"error": "URL invalide"}), 400

        reachable, info = ping_host(host, port)
        return jsonify({
            "auth_url":  auth_url,
            "host":      host,
            "port":      port,
            "reachable": reachable,
            "latency":   info if reachable else None,
            "error":     info if not reachable else None,
        })

    # ════════════════════════════════════════════════════════════
    # OPENSTACK — TEST AUTH (step 2)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/openstack/test-auth", methods=["POST"])
    @require_auth
    def openstack_test_auth():
        """
        Step 2 — Test real Keystone authentication.
        Returns networks, flavors, quota on success.
        """
        data = request.get_json(force=True) or {}
        required = ["auth_url", "project_name", "username", "password"]
        missing  = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Champs requis: {', '.join(missing)}"}), 400

        success, message, info = test_openstack_auth(
            auth_url       = data["auth_url"],
            project_name   = data["project_name"],
            username       = data["username"],
            password       = data["password"],
            user_domain    = data.get("user_domain_name", "Default"),
            project_domain = data.get("project_domain_name", "Default"),
            region         = data.get("region_name", "RegionOne"),
            ssl_verify     = data.get("ssl_verify", True),
        )
        return jsonify({
            "success":  success,
            "message":  message,
            "networks": info.get("networks", []),
            "flavors":  info.get("flavors", []),
            "quota":    info.get("quota", {}),
        })

    # ════════════════════════════════════════════════════════════
    # OPENSTACK — SAVE (step 3)
    # ════════════════════════════════════════════════════════════

    @app.route("/api/v1/connections/openstack", methods=["GET"])
    @require_auth
    def list_openstacks():
        store = cs()
        return jsonify({
            "count":      len(store.list_openstacks()),
            "openstacks": [c.to_dict() for c in store.list_openstacks()],
        })

    @app.route("/api/v1/connections/openstack", methods=["POST"])
    @admin_only
    def add_openstack():
        """Save an OpenStack connection after successful auth test."""
        data = request.get_json(force=True) or {}
        required = ["name", "auth_url", "project_name", "username", "password"]
        missing  = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Champs requis: {', '.join(missing)}"}), 400

        store = cs()
        conn  = store.add_openstack(
            name                 = data["name"],
            auth_url             = data["auth_url"],
            project_name         = data["project_name"],
            username             = data["username"],
            password             = data["password"],
            user_domain_name     = data.get("user_domain_name", "Default"),
            project_domain_name  = data.get("project_domain_name", "Default"),
            region_name          = data.get("region_name", "RegionOne"),
            ssl_verify           = data.get("ssl_verify", True),
            os_type              = data.get("os_type", "custom"),
            enterprise_project_id = data.get("enterprise_project_id", ""),
            volume_type          = data.get("volume_type", ""),
            availability_zone    = data.get("availability_zone", "nova"),
            external_network     = data.get("external_network", "public"),
            added_by             = g.current_user.login,
        )
        ok, msg, _ = test_openstack_auth(
            conn.auth_url, conn.project_name, conn.username, conn.password,
            conn.user_domain_name, conn.project_domain_name, conn.region_name,
            conn.ssl_verify,
        )
        store.set_openstack_status(conn.conn_id, "connected" if ok else "error",
                                   error="" if ok else msg)
        logger.info(f"OpenStack '{conn.name}' ajouté par {g.current_user.login}")
        return jsonify({
            "message":    f"OpenStack '{conn.name}' enregistré",
            "openstack":  conn.to_dict(),
            "connected":  ok,
        }), 201

    @app.route("/api/v1/connections/openstack/<conn_id>", methods=["PUT"])
    @admin_only
    def update_openstack(conn_id: str):
        data  = request.get_json(force=True) or {}
        store = cs()
        conn  = store.update_openstack(conn_id, **data)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        return jsonify({"message": "Mis à jour", "openstack": conn.to_dict()})

    @app.route("/api/v1/connections/openstack/<conn_id>", methods=["DELETE"])
    @admin_only
    def delete_openstack(conn_id: str):
        store = cs()
        if not store.delete_openstack(conn_id):
            return jsonify({"error": "Connexion introuvable"}), 404
        return jsonify({"message": "Connexion OpenStack supprimée"})

    @app.route("/api/v1/connections/openstack/<conn_id>/retest", methods=["POST"])
    @require_auth
    def retest_openstack(conn_id: str):
        store = cs()
        conn  = store.get_openstack(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        ok, msg, info = test_openstack_auth(
            conn.auth_url, conn.project_name, conn.username, conn.password,
            conn.user_domain_name, conn.project_domain_name,
            conn.region_name, conn.ssl_verify,
        )
        store.set_openstack_status(conn_id, "connected" if ok else "error",
                                   error="" if ok else msg)
        return jsonify({"success": ok, "message": msg,
                        "networks": info.get("networks", []),
                        "quota": info.get("quota", {})})

    @app.route("/api/v1/connections/openstack/<conn_id>/resources")
    @require_auth
    def conn_openstack_resources(conn_id: str):
        """Get live quota + networks + flavors for a saved connection."""
        store = cs()
        conn  = store.get_openstack(conn_id)
        if not conn:
            return jsonify({"error": "Connexion introuvable"}), 404
        ok, msg, info = test_openstack_auth(
            conn.auth_url, conn.project_name, conn.username, conn.password,
            conn.user_domain_name, conn.project_domain_name,
            conn.region_name, conn.ssl_verify,
        )
        if not ok:
            return jsonify({"error": msg}), 500
        return jsonify({
            "conn_id":  conn_id,
            "name":     conn.name,
            "networks": info.get("networks", []),
            "flavors":  info.get("flavors", []),
            "quota":    info.get("quota", {}),
        })
