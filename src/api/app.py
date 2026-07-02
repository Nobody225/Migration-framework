"""
src/api/app.py
───────────────
REST API + WebSocket server for the migration framework.

Endpoints:
  GET  /api/v1/health                    → Health check
  GET  /api/v1/vms                       → List VMware VMs
  POST /api/v1/migrate                   → Start migration job(s)
  GET  /api/v1/jobs                      → List all jobs
  GET  /api/v1/jobs/<job_id>             → Job details
  GET  /api/v1/jobs/<job_id>/logs        → Job audit log
  GET  /api/v1/jobs/<job_id>/report      → Performance report
  GET  /api/v1/jobs/<job_id>/recommendations → Optimization recs
  DELETE /api/v1/jobs/<job_id>           → Cancel a pending job
  GET  /api/v1/stats                     → Global migration stats

WebSocket events (Socket.IO):
  job_update   → pushed on every status transition
  job_complete → pushed when job reaches terminal state
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.core.models import MigrationMode, MigrationStatus, OpenStackTarget
from src.utils.config_loader import load_config
from src.utils.framework_factory import build_framework
from src.api.connection_routes import register_connection_routes
from src.api.auth_routes import auth_bp
from src.api.project_routes import register_project_routes

logger = logging.getLogger("migration.api")


# ════════════════════════════════════════════════════════════════
# APP FACTORY
# ════════════════════════════════════════════════════════════════

def create_app(config_path: str = "config/config.yaml") -> tuple[Flask, SocketIO]:
    """
    Create and configure the Flask application.
    Returns (app, socketio) tuple.
    """
    app = Flask(
        __name__,
        static_folder=os.path.join(
            os.path.dirname(__file__), "..", "..", "dashboard", "static"
        ),
        template_folder=os.path.join(
            os.path.dirname(__file__), "..", "..", "dashboard", "templates"
        ),
    )

    CORS(app)
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        logger=False,
        engineio_logger=False,
    )

    # Load config and build framework
    config = load_config(config_path)
    orchestrator = build_framework(config)

    # Register real-time status hook
    def on_status_change(job):
        socketio.emit("job_update", _job_summary(job))
        if job.is_terminal:
            socketio.emit("job_complete", _job_summary(job))

    orchestrator.set_status_hook(on_status_change)

    # Store on app context
    app.config["ORCHESTRATOR"] = orchestrator
    app.config["FRAMEWORK_CONFIG"] = config

    # Register blueprints
    _register_routes(app, socketio)

    app.register_blueprint(auth_bp)
    register_connection_routes(app)
    register_project_routes(app, get_orchestrator_fn=lambda: app.config["ORCHESTRATOR"])
    register_vcenter_openstack_routes(app)
    return app, socketio


from src.api.instance_actions import register_instance_routes
from src.api.auth_routes import auth_bp
from src.api.project_routes import register_project_routes
from src.api.auth_middleware import require_auth, admin_only

def _register_routes(app: Flask, socketio: SocketIO):
    """Register all API routes."""

    def get_orch():
        return app.config["ORCHESTRATOR"]

    # ── Health ──────────────────────────────────────────────────

    @app.route("/api/v1/health")
    def health():
        return jsonify({
            "status":    "ok",
            "timestamp": datetime.now().isoformat(),
            "version":   "2.0.0",
            "framework": "VMware → OpenStack Migration Framework",
            "targets":   [t.value for t in OpenStackTarget],
        })

    # ── Dashboard (serve HTML) ───────────────────────────────────

    @app.route("/static/dashboard/<path:filename>")
    def dashboard_static(filename):
        import os
        static_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "dashboard", "static"
        )
        return send_from_directory(static_dir, filename)

    @app.route("/")
    def dashboard():
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", "templates"),
            "index.html"
        )

    @app.route("/login")
    def login_page():
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", "templates"),
            "login.html"
        )

    # ── VMs ─────────────────────────────────────────────────────

    @app.route("/api/v1/vms")
    def list_vms():
        """List VMs available in vSphere."""
        config = app.config["FRAMEWORK_CONFIG"]
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            extractor  = VMwareExtractor(config.get("vmware", {}))
            extractor.connect()
            vms        = extractor.list_vms()
            extractor.disconnect()
            return jsonify({
                "count": len(vms),
                "vms":   [vm.to_dict() for vm in vms],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Jobs ─────────────────────────────────────────────────────

    @app.route("/api/v1/jobs", methods=["GET"])
    def list_jobs():
        """List all migration jobs with optional filters."""
        orch   = get_orch()
        status = request.args.get("status")
        target = request.args.get("target")

        status_filter = MigrationStatus(status) if status else None
        target_filter = OpenStackTarget(target) if target else None

        jobs = orch.list_jobs(status=status_filter, target=target_filter)
        return jsonify({
            "count": len(jobs),
            "jobs":  [_job_summary(j) for j in jobs],
        })

    @app.route("/api/v1/jobs/<job_id>", methods=["GET"])
    def get_job(job_id: str):
        """Get full details of a specific job."""
        orch = get_orch()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        return jsonify(_job_detail(job))

    @app.route("/api/v1/jobs/<job_id>/logs", methods=["GET"])
    def get_job_logs(job_id: str):
        """Get the full audit log for a job."""
        orch = get_orch()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404

        level_filter = request.args.get("level", "").upper()
        entries = [e.to_dict() for e in job.audit_log]

        if level_filter:
            entries = [e for e in entries if e["level"] == level_filter]

        return jsonify({
            "job_id":  job_id,
            "count":   len(entries),
            "entries": entries,
        })

    @app.route("/api/v1/jobs/<job_id>/report", methods=["GET"])
    def get_job_report(job_id: str):
        """Get performance report for a completed job."""
        orch = get_orch()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        if not job.performance_report:
            return jsonify({"error": "No performance report available"}), 404

        r = job.performance_report
        return jsonify({
            "job_id":       job_id,
            "vm_name":      r.vm_name,
            "target":       r.target.value,
            "overall_score": r.overall_score,
            "passed":       r.passed,
            "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            "regressions":  r.regressions,
            "recommendations": r.recommendations,
            "comparisons": [
                {
                    "metric":      c.metric_name,
                    "vmware":      c.vmware_value,
                    "openstack":   c.openstack_value,
                    "unit":        c.unit,
                    "delta_pct":   c.delta_percent,
                    "regression":  c.is_regression,
                }
                for c in r.comparisons
            ],
            "vmware_metrics_count":    len(r.vmware_benchmarks),
            "openstack_metrics_count": len(r.openstack_benchmarks),
        })

    @app.route("/api/v1/jobs/<job_id>/recommendations", methods=["GET"])
    def get_recommendations(job_id: str):
        """Get optimization recommendations for a job."""
        orch = get_orch()
        job  = orch.get_job(job_id)
        if not job:
            return jsonify({"error": f"Job not found: {job_id}"}), 404

        return jsonify({
            "job_id": job_id,
            "count":  len(job.optimization_recommendations),
            "recommendations": [
                {
                    "resource":          r.resource,
                    "current":           str(r.current_value),
                    "recommended":       str(r.recommended_value),
                    "reason":            r.reason,
                    "saving_pct":        r.estimated_saving_percent,
                    "priority":          r.priority,
                }
                for r in job.optimization_recommendations
            ],
        })

    @app.route("/api/v1/jobs/<job_id>", methods=["DELETE"])
    def cancel_job(job_id: str):
        """Cancel a pending migration job."""
        orch    = get_orch()
        success = orch.cancel_job(job_id)
        if not success:
            return jsonify({"error": "Job not found or cannot be cancelled"}), 400
        return jsonify({"job_id": job_id, "status": "cancelled"})

    # ── Migrate ──────────────────────────────────────────────────

    @app.route("/api/v1/migrate", methods=["POST"])
    def start_migration():
        """
        Start one or more migration jobs.

        Body (JSON):
        {
            "vms": ["vm-name-1", "vm-name-2"],
            "vcenter_id": "<conn_id>",       // UUID depuis ConnectionStore (dashboard)
            "openstack_id": "<conn_id>",     // UUID depuis ConnectionStore (dashboard)
            "target": "custom",              // Fallback si openstack_id absent
            "mode": "cold" | "warm",
            "operator": "john.doe",
            "dry_run": false,
            "skip_evaluation": false,
            "parallel": false
        }
        """
        orch = get_orch()
        data = request.get_json(force=True) or {}

        vm_names = data.get("vms", [])
        if not vm_names:
            return jsonify({"error": "'vms' list is required"}), 400

        # ── Résolution OpenStack ─────────────────────────────────
        openstack_id = data.get("openstack_id")  # conn_id depuis ConnectionStore
        target_str   = data.get("target", "custom")

        # Si openstack_id fourni, déterminer le type depuis ConnectionStore
        if openstack_id:
            try:
                from src.api.connection_store import get_connection_store
                store = get_connection_store()
                os_conn = store.get_openstack(openstack_id)
                if os_conn:
                    target_str = os_conn.os_type  # "custom", "redhat", "huawei"
            except Exception:
                pass

        try:
            target = OpenStackTarget(target_str.lower())
        except ValueError:
            target = OpenStackTarget.CUSTOM

        # ── Résolution vCenter ───────────────────────────────────
        vcenter_id = data.get("vcenter_id")  # conn_id depuis ConnectionStore

        # ── Mode de migration ────────────────────────────────────
        mode_str = data.get("mode", "cold")
        try:
            mode = MigrationMode(mode_str.lower())
        except ValueError:
            return jsonify({"error": f"Mode invalide: {mode_str}"}), 400

        operator  = data.get("operator", "api-user")
        skip_eval = data.get("skip_evaluation", False)
        parallel  = data.get("parallel", False)
        dry_run   = data.get("dry_run", None)

        # Override dry_run si spécifié dans la requête
        if dry_run is not None:
            app.config["FRAMEWORK_CONFIG"]["migration"]["dry_run"] = bool(dry_run)
            orch.dry_run = bool(dry_run)

        requests_list = [
            {
                "vm_name":         vm_name,
                "target":          target,
                "mode":            mode,
                "operator":        operator,
                "skip_evaluation": skip_eval,
                "vcenter_id":      vcenter_id,
                "openstack_id":    openstack_id,
            }
            for vm_name in vm_names
        ]

        # Lancer en background
        import threading
        def run_migrations():
            orch.migrate_batch(requests_list, parallel=parallel)

        thread = threading.Thread(target=run_migrations, daemon=True)
        thread.start()

        pending_jobs = [
            j for j in orch.list_jobs(status=MigrationStatus.PENDING)
            if j.vm_name in vm_names
        ]

        return jsonify({
            "message":      f"Migration démarrée pour {len(vm_names)} VM(s)",
            "jobs":         [_job_summary(j) for j in pending_jobs],
            "vcenter_id":   vcenter_id,
            "openstack_id": openstack_id,
            "dry_run":      orch.dry_run,
        }), 202

    # ── Stats ────────────────────────────────────────────────────

    @app.route("/api/v1/stats")
    def get_stats():
        """Global migration statistics."""
        orch = get_orch()
        all_jobs = orch.list_jobs()

        status_counts = {}
        for status in MigrationStatus:
            count = sum(1 for j in all_jobs if j.status == status)
            if count > 0:
                status_counts[status.value] = count

        target_counts = {}
        for target in OpenStackTarget:
            count = sum(1 for j in all_jobs if j.target == target)
            if count > 0:
                target_counts[target.value] = count

        completed = [j for j in all_jobs if j.status == MigrationStatus.COMPLETED]
        avg_score = (
            sum(j.performance_report.overall_score for j in completed if j.performance_report)
            / max(1, len([j for j in completed if j.performance_report]))
        ) if completed else 0

        avg_duration = (
            sum(j.duration_seconds for j in completed if j.duration_seconds)
            / max(1, len([j for j in completed if j.duration_seconds]))
        ) if completed else 0

        return jsonify({
            "total_jobs":       len(all_jobs),
            "by_status":        status_counts,
            "by_target":        target_counts,
            "success_rate_pct": round(
                len(completed) / max(1, len(all_jobs)) * 100, 1
            ),
            "avg_perf_score":   round(avg_score, 1),
            "avg_duration_s":   round(avg_duration, 1),
        })

    # ── WebSocket ────────────────────────────────────────────────

    # Register post-migration instance action routes

    # ── Topology & ESXi host endpoints ──────────────────────────────

    @app.route("/api/v1/vcenters/<vcenter_id>/topology")
    def vcenter_topology(vcenter_id: str):
        """Return full DC → Cluster → ESXi hierarchy."""
        config  = app.config["FRAMEWORK_CONFIG"]
        vcenters = config.get("vcenters", [])
        if vcenter_id == "default" or not vcenters:
            vc_config = config.get("vmware", {})
        else:
            vc_config = next((v for v in vcenters if v.get("id") == vcenter_id), config.get("vmware", {}))
        if not vc_config:
            return jsonify({"error": f"vCenter '{vcenter_id}' non configuré"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(vc_config)
            ext.connect()
            topo = ext.list_topology()
            ext.disconnect()
            return jsonify({"vcenter_id": vcenter_id, **topo})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/vcenters/<vcenter_id>/hosts")
    def vcenter_hosts(vcenter_id: str):
        """Return flat list of all ESXi hosts with VM counts."""
        config   = app.config["FRAMEWORK_CONFIG"]
        vcenters = config.get("vcenters", [])
        if vcenter_id == "default" or not vcenters:
            vc_config = config.get("vmware", {})
        else:
            vc_config = next((v for v in vcenters if v.get("id") == vcenter_id), config.get("vmware", {}))
        if not vc_config:
            return jsonify({"error": f"vCenter '{vcenter_id}' non configuré"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(vc_config)
            ext.connect()
            hosts = ext.list_hosts()
            ext.disconnect()
            return jsonify({
                "vcenter_id": vcenter_id,
                "count":      len(hosts),
                "hosts":      hosts,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/vcenters/<vcenter_id>/hosts/<host_mor_id>/vms")
    def host_vms(vcenter_id: str, host_mor_id: str):
        """Return all VMs on a specific ESXi host."""
        config   = app.config["FRAMEWORK_CONFIG"]
        vcenters = config.get("vcenters", [])
        if vcenter_id == "default" or not vcenters:
            vc_config = config.get("vmware", {})
        else:
            vc_config = next((v for v in vcenters if v.get("id") == vcenter_id), config.get("vmware", {}))
        if not vc_config:
            return jsonify({"error": f"vCenter '{vcenter_id}' non configuré"}), 404
        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            ext = VMwareExtractor(vc_config)
            ext.connect()
            vms = ext.list_vms_on_host(host_mor_id)
            ext.disconnect()
            return jsonify({
                "vcenter_id":  vcenter_id,
                "host_mor_id": host_mor_id,
                "count":       len(vms),
                "vms":         [vm.to_dict() for vm in vms],
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    register_instance_routes(
        app,
        get_orchestrator_fn=lambda: app.config["ORCHESTRATOR"],
        get_config_fn=lambda: app.config["FRAMEWORK_CONFIG"],
    )

    @socketio.on("connect")
    def on_connect():
        logger.info(f"WebSocket client connected")
        # Send current state to newly connected client
        orch     = get_orch()
        all_jobs = orch.list_jobs()
        emit("initial_state", {
            "jobs": [_job_summary(j) for j in all_jobs]
        })

    @socketio.on("subscribe_job")
    def on_subscribe(data):
        """Client subscribes to updates for a specific job."""
        job_id = data.get("job_id")
        orch   = get_orch()
        job    = orch.get_job(job_id)
        if job:
            emit("job_update", _job_summary(job))


# ════════════════════════════════════════════════════════════════
# SERIALIZERS
# ════════════════════════════════════════════════════════════════

def _job_summary(job) -> Dict[str, Any]:
    """Compact job representation for lists and WebSocket events."""
    from src.api.project_store import get_project_store
    proj = get_project_store().find_by_job(job.job_id)
    return {
        "project_id":   proj.project_id if proj else None,
        "project_name": proj.name if proj else None,
        "project_code": proj.code if proj else None,
        "job_id":      job.job_id,
        "vm_name":     job.vm_name,
        "target":      job.target.value,
        "mode":        job.mode.value,
        "status":      job.status.value,
        "operator":    job.operator,
        "created_at":  job.created_at.isoformat(),
        "started_at":  job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_s":  job.duration_seconds,
        "instance_id": job.instance.instance_id if job.instance else None,
        "perf_score":  job.performance_report.overall_score if job.performance_report else None,
        "regressions": len(job.performance_report.regressions) if job.performance_report else 0,
        "audit_count": len(job.audit_log),
    }


def _job_detail(job) -> Dict[str, Any]:
    """Full job representation for single-job endpoint."""
    detail = _job_summary(job)
    detail["source_vm"] = job.source_vm.to_dict() if job.source_vm else None
    detail["optimization_recommendations"] = [
        {
            "resource":    r.resource,
            "current":     str(r.current_value),
            "recommended": str(r.recommended_value),
            "reason":      r.reason,
            "saving_pct":  r.estimated_saving_percent,
            "priority":    r.priority,
        }
        for r in job.optimization_recommendations
    ]
    detail["errors"] = [
        e.to_dict() for e in job.audit_log
        if e.level.value == "ERROR"
    ]
    return detail


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migration Framework API Server")
    parser.add_argument("--config",  default="config/config.yaml")
    parser.add_argument("--host",    default="0.0.0.0")
    parser.add_argument("--port",    type=int, default=8080)
    parser.add_argument("--debug",   action="store_true", default=False)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app, socketio = create_app(args.config)
    print(f"\n Migration Framework API")
    print(f" Running on http://{args.host}:{args.port}")
    print(f" Dashboard: http://localhost:{args.port}/\n")

    socketio.run(app, host=args.host, port=args.port, debug=args.debug)


# ════════════════════════════════════════════════════════════════
# MULTI-VCENTER ENDPOINTS
# ════════════════════════════════════════════════════════════════

def register_vcenter_openstack_routes(app):
    """Register multi-vCenter and OpenStack health check routes."""

    @app.route("/api/v1/vcenters/<vcenter_id>/vms")
    def list_vms_from_vcenter(vcenter_id: str):
        """List VMs from a specific vCenter."""
        config = app.config["FRAMEWORK_CONFIG"]
        vcenters = config.get("vcenters", [])

        # Find the right vcenter config
        if vcenter_id == "default" or not vcenters:
            vc_config = config.get("vmware", {})
        else:
            vc_config = next(
                (v for v in vcenters if v.get("id") == vcenter_id),
                config.get("vmware", {}),
            )

        if not vc_config:
            return jsonify({"error": f"vCenter '{vcenter_id}' not found"}), 404

        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            extractor = VMwareExtractor(vc_config)
            extractor.connect()
            vms = extractor.list_vms()
            extractor.disconnect()
            return jsonify({
                "vcenter_id": vcenter_id,
                "vcenter_host": vc_config.get("host", ""),
                "count": len(vms),
                "vms": [vm.to_dict() for vm in vms],
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/vcenters/<vcenter_id>/test")
    def test_vcenter(vcenter_id: str):
        """Test connectivity to a vCenter."""
        config = app.config["FRAMEWORK_CONFIG"]
        vcenters = config.get("vcenters", [])
        if vcenter_id == "default" or not vcenters:
            vc_config = config.get("vmware", {})
        else:
            vc_config = next((v for v in vcenters if v.get("id") == vcenter_id), {})

        if not vc_config:
            return jsonify({"status": "error", "message": "vCenter not found"}), 404

        try:
            from src.extractor.vmware_extractor import VMwareExtractor
            extractor = VMwareExtractor(vc_config)
            extractor.connect()
            extractor.disconnect()
            return jsonify({
                "status": "ok",
                "vcenter_id": vcenter_id,
                "host": vc_config.get("host", ""),
                "message": "Connexion réussie",
            })
        except Exception as exc:
            return jsonify({
                "status": "error",
                "vcenter_id": vcenter_id,
                "host": vc_config.get("host", ""),
                "message": str(exc),
            }), 500

    # ── OpenStack health checks ──────────────────────────────────

    @app.route("/api/v1/openstack/status")
    def openstack_status():
        """Check availability of all configured OpenStack targets."""
        config = app.config["FRAMEWORK_CONFIG"]
        os_configs = config.get("openstack", {})
        results = []

        for target_name, target_cfg in os_configs.items():
            result = _check_openstack(target_name, target_cfg)
            results.append(result)

        overall = all(r["status"] == "ok" for r in results)
        return jsonify({
            "overall": "ok" if overall else "degraded",
            "targets": results,
        })

    @app.route("/api/v1/openstack/<target>/status")
    def openstack_target_status(target: str):
        """Check availability of one specific OpenStack target."""
        config = app.config["FRAMEWORK_CONFIG"]
        target_cfg = config.get("openstack", {}).get(target)
        if not target_cfg:
            return jsonify({"error": f"Target '{target}' not configured"}), 404
        result = _check_openstack(target, target_cfg)
        return jsonify(result)

    @app.route("/api/v1/openstack/<target>/resources")
    def openstack_resources(target: str):
        """Get quota and resource usage for an OpenStack target."""
        config = app.config["FRAMEWORK_CONFIG"]
        target_cfg = config.get("openstack", {}).get(target)
        if not target_cfg:
            return jsonify({"error": f"Target '{target}' not configured"}), 404

        try:
            from src.core.models import OpenStackTarget as OST
            from src.adapters.huawei.adapter import AdapterFactory
            tgt = OST(target)
            adapter = AdapterFactory.create(tgt, target_cfg)
            adapter.connect()
            conn = adapter._conn

            # Compute quota
            limits = conn.compute.get_limits()
            abs_l  = limits.get("absolute", {})

            # Network info
            networks = list(conn.network.networks())
            # Flavors
            flavors = list(conn.compute.flavors())

            adapter.disconnect()

            return jsonify({
                "target": target,
                "status": "ok",
                "compute": {
                    "max_instances":    abs_l.get("maxTotalInstances", 0),
                    "used_instances":   abs_l.get("totalInstancesUsed", 0),
                    "max_vcpus":        abs_l.get("maxTotalCores", 0),
                    "used_vcpus":       abs_l.get("totalCoresUsed", 0),
                    "max_ram_mb":       abs_l.get("maxTotalRAMSize", 0),
                    "used_ram_mb":      abs_l.get("totalRAMUsed", 0),
                },
                "networks": [{"id": n.id, "name": n.name} for n in networks[:10]],
                "flavors":  [{"id": f.id, "name": f.name,
                              "vcpus": f.vcpus, "ram": f.ram, "disk": f.disk}
                             for f in flavors[:20]],
            })
        except Exception as exc:
            return jsonify({"target": target, "status": "error", "message": str(exc)}), 500

    return app


def _check_openstack(target_name: str, target_cfg: dict) -> dict:
    """Perform a lightweight Keystone auth check."""
    import time
    start = time.monotonic()
    try:
        from src.core.models import OpenStackTarget as OST
        from src.adapters.huawei.adapter import AdapterFactory
        tgt     = OST(target_name)
        adapter = AdapterFactory.create(tgt, target_cfg)
        adapter.connect()
        adapter.disconnect()
        latency = round((time.monotonic() - start) * 1000)
        return {
            "target":   target_name,
            "status":   "ok",
            "latency_ms": latency,
            "auth_url": target_cfg.get("auth_url", ""),
            "message":  "Authentification réussie",
        }
    except Exception as exc:
        latency = round((time.monotonic() - start) * 1000)
        return {
            "target":   target_name,
            "status":   "error",
            "latency_ms": latency,
            "auth_url": target_cfg.get("auth_url", ""),
            "message":  str(exc),
        }

