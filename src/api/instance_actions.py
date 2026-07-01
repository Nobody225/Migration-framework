"""
src/api/instance_actions.py
────────────────────────────
Post-migration instance management endpoints.
Registered as a Blueprint on the main Flask app.

Endpoints:
  POST /api/v1/instances/<job_id>/action    → reboot, stop, snapshot, resize, console, floating-ip, sg
  POST /api/v1/instances/<job_id>/optimize  → apply an optimization recommendation
  GET  /api/v1/instances/<job_id>/status    → live instance status from OpenStack
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

logger = logging.getLogger("migration.api.instances")

instance_bp = Blueprint("instances", __name__)


def register_instance_routes(app, get_orchestrator_fn, get_config_fn):
    """Register instance action routes on the Flask app."""

    # ── Live instance status ──────────────────────────────────

    @app.route("/api/v1/instances/<job_id>/status")
    def instance_status(job_id: str):
        """
        Fetch real-time instance status from the target OpenStack.
        Returns current power state, IP addresses, and resource usage.
        """
        orch = get_orchestrator_fn()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        if not job.instance:
            return jsonify({"error": "No instance associated with this job"}), 404

        try:
            from src.adapters.huawei.adapter import AdapterFactory
            config  = get_config_fn()
            adapter = AdapterFactory.create(
                job.target,
                config.get("openstack", {}).get(job.target.value, {}),
            )
            adapter.connect()
            server = adapter.get_instance(job.instance.instance_id)
            adapter.disconnect()

            if not server:
                return jsonify({"error": "Instance not found on OpenStack"}), 404

            return jsonify({
                "job_id":      job_id,
                "instance_id": job.instance.instance_id,
                "name":        job.instance.name,
                "status":      getattr(server, "status", "UNKNOWN"),
                "power_state": getattr(server, "OS-EXT-STS:power_state", None),
                "task_state":  getattr(server, "OS-EXT-STS:task_state", None),
                "addresses":   getattr(server, "addresses", {}),
                "flavor":      getattr(server, "flavor", {}).get("original_name", ""),
                "az":          getattr(server, "OS-EXT-AZ:availability_zone", ""),
                "hypervisor":  getattr(server, "OS-EXT-SRV-ATTR:hypervisor_hostname", ""),
            })

        except Exception as exc:
            logger.warning(f"Could not fetch live status for {job_id}: {exc}")
            # Return last known status from job data
            return jsonify({
                "job_id":      job_id,
                "instance_id": job.instance.instance_id,
                "name":        job.instance.name,
                "status":      job.instance.status,
                "addresses":   job.instance.ip_addresses,
                "source":      "cached",
                "note":        "Live status unavailable — returning cached data",
            })

    # ── Instance actions ──────────────────────────────────────

    @app.route("/api/v1/instances/<job_id>/action", methods=["POST"])
    def instance_action(job_id: str):
        """
        Execute an action on a migrated instance.

        Supported actions:
          reboot      → Nova soft reboot
          stop        → Nova stop (power off)
          start       → Nova start (power on)
          snapshot    → Create Glance snapshot from instance
          resize      → Initiate Nova resize (requires new_flavor in body)
          console     → Get VNC console URL
          floating-ip → Assign a floating IP
          sg          → List security groups (read-only in this version)
        """
        orch = get_orchestrator_fn()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        if not job.instance:
            return jsonify({"error": "No instance associated with this job"}), 404

        data   = request.get_json(force=True) or {}
        action = data.get("action", "").lower()

        SUPPORTED_ACTIONS = ["reboot", "stop", "start", "snapshot", "resize", "console", "floating-ip", "sg"]
        if action not in SUPPORTED_ACTIONS:
            return jsonify({
                "error": f"Unknown action '{action}'. Supported: {SUPPORTED_ACTIONS}"
            }), 400

        try:
            config  = get_config_fn()
            from src.adapters.huawei.adapter import AdapterFactory
            adapter = AdapterFactory.create(
                job.target,
                config.get("openstack", {}).get(job.target.value, {}),
            )
            adapter.connect()
            result  = _execute_action(adapter, job.instance.instance_id, action, data)
            adapter.disconnect()

            job.log(f"Action exécutée: {action}", f"Instance: {job.instance.instance_id}", module="api")
            return jsonify({
                "job_id":      job_id,
                "instance_id": job.instance.instance_id,
                "action":      action,
                "message":     result,
            })

        except Exception as exc:
            logger.error(f"Action '{action}' failed for {job_id}: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ── Optimization ──────────────────────────────────────────

    @app.route("/api/v1/instances/<job_id>/optimize", methods=["POST"])
    def apply_optimization(job_id: str):
        """
        Apply an optimization recommendation to a migrated instance.

        Body:
          {
            "resource": "vcpu" | "ram" | "flavor" | "disk_type",
            "recommended_value": "m1.small" | "2" | "SSD" | ...
          }
        """
        orch = get_orchestrator_fn()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        if not job.instance:
            return jsonify({"error": "No instance associated with this job"}), 404

        data      = request.get_json(force=True) or {}
        resource  = data.get("resource", "")
        new_value = data.get("recommended_value", "")

        if not resource or not new_value:
            return jsonify({"error": "resource and recommended_value are required"}), 400

        try:
            config  = get_config_fn()
            from src.adapters.huawei.adapter import AdapterFactory
            adapter = AdapterFactory.create(
                job.target,
                config.get("openstack", {}).get(job.target.value, {}),
            )
            adapter.connect()
            msg = _apply_optimization(adapter, job.instance.instance_id, resource, new_value)
            adapter.disconnect()

            job.log(
                f"Optimisation appliquée: {resource}",
                f"Nouvelle valeur: {new_value}",
                module="api",
            )
            return jsonify({
                "job_id":      job_id,
                "instance_id": job.instance.instance_id,
                "resource":    resource,
                "new_value":   new_value,
                "message":     msg,
            })

        except Exception as exc:
            logger.error(f"Optimization failed for {job_id}: {exc}")
            return jsonify({"error": str(exc)}), 500


# ════════════════════════════════════════════════════════════════
# ACTION HANDLERS
# ════════════════════════════════════════════════════════════════

def _execute_action(adapter, instance_id: str, action: str, data: Dict[str, Any]) -> str:
    """Execute a Nova action on the instance. Returns a human-readable result message."""
    conn = adapter._conn

    if action == "reboot":
        conn.compute.reboot_server(instance_id, reboot_type="SOFT")
        return "Instance redémarrée (soft reboot)"

    elif action == "stop":
        conn.compute.stop_server(instance_id)
        return "Instance arrêtée"

    elif action == "start":
        conn.compute.start_server(instance_id)
        return "Instance démarrée"

    elif action == "snapshot":
        server   = conn.compute.get_server(instance_id)
        snap_name = f"snapshot-{server.name}-{_now()}"
        image_id  = conn.compute.create_server_image(instance_id, name=snap_name)
        return f"Snapshot créé: {snap_name} ({image_id})"

    elif action == "resize":
        new_flavor = data.get("new_flavor", "")
        if not new_flavor:
            raise ValueError("new_flavor is required for resize action")
        flavor = conn.compute.find_flavor(new_flavor)
        if not flavor:
            raise ValueError(f"Flavor not found: {new_flavor}")
        conn.compute.resize_server(instance_id, flavor.id)
        return f"Resize initié vers flavor: {new_flavor}"

    elif action == "console":
        console = conn.compute.get_server_console_output(instance_id, length=50)
        # For VNC, use: conn.compute.create_server_remote_console(instance_id, ...)
        return "Console VNC disponible via Horizon Dashboard"

    elif action == "floating-ip":
        config  = getattr(adapter, 'config', {})
        ext_net = config.get(adapter.name, {}).get("external_network", "public")
        fip     = conn.network.create_ip(floating_network_id=ext_net)
        port    = next(iter(conn.network.ports(device_id=instance_id)), None)
        if port:
            conn.network.update_ip(fip.id, port_id=port.id)
            return f"IP flottante assignée: {fip.floating_ip_address}"
        return f"IP flottante créée: {fip.floating_ip_address} (association manuelle requise)"

    elif action == "sg":
        server = conn.compute.get_server(instance_id)
        sgs    = [sg["name"] for sg in (server.security_groups or [])]
        return f"Security groups: {', '.join(sgs) or 'aucun'}"

    raise ValueError(f"Action non implémentée: {action}")


def _apply_optimization(adapter, instance_id: str, resource: str, value: str) -> str:
    """Apply a resource optimization to the instance."""
    conn = adapter._conn

    if resource == "flavor":
        flavor = conn.compute.find_flavor(value)
        if not flavor:
            raise ValueError(f"Flavor '{value}' introuvable")
        conn.compute.resize_server(instance_id, flavor.id)
        return f"Resize vers {value} initié — confirmez avec 'confirm_resize'"

    elif resource in ("vcpu", "ram"):
        # These require a resize to a new flavor — we suggest the closest match
        return f"Right-sizing {resource} → créez un flavor personnalisé avec {value} et effectuez un resize"

    elif resource == "disk_type":
        return f"Migration de volume vers type {value} — opération disponible via Cinder volume retype"

    else:
        return f"Recommandation {resource} enregistrée: {value}"


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")
