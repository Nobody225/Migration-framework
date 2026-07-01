"""
src/api/project_routes.py
──────────────────────────
REST endpoints for migration projects.

GET    /api/v1/projects                        → list all projects
POST   /api/v1/projects                        → create project
GET    /api/v1/projects/<id>                   → project details
PUT    /api/v1/projects/<id>                   → update project
DELETE /api/v1/projects/<id>                   → delete (only if no jobs)
GET    /api/v1/projects/<id>/stats             → live KPIs
POST   /api/v1/projects/<id>/jobs/<job_id>     → attach job to project
DELETE /api/v1/projects/<id>/jobs/<job_id>     → detach job
GET    /api/v1/jobs/<job_id>/project           → get project for a job
"""

from __future__ import annotations

import logging
from flask import Blueprint, g, jsonify, request

from src.api.auth_middleware import require_auth, admin_only
from src.api.project_store import (
    ProjectPriority, ProjectStatus, get_project_store,
)

logger = logging.getLogger("migration.api.projects")
project_bp = Blueprint("projects", __name__, url_prefix="/api/v1")


def register_project_routes(app, get_orchestrator_fn):

    @app.route("/api/v1/projects", methods=["GET"])
    @require_auth
    def list_projects():
        ps     = get_project_store()
        status = request.args.get("status")
        vc     = request.args.get("vcenter")
        target = request.args.get("target")
        projects = ps.list_all(status=status, vcenter=vc, target=target)
        return jsonify({
            "count":    len(projects),
            "projects": [p.to_dict() for p in projects],
        })

    @app.route("/api/v1/projects", methods=["POST"])
    @require_auth
    def create_project():
        data = request.get_json(force=True) or {}
        required = ["name", "code"]
        missing  = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Champs manquants: {', '.join(missing)}"}), 400

        ps   = get_project_store()
        proj, err = ps.create(
            name                 = data["name"],
            code                 = data["code"],
            created_by           = g.current_user.login,
            description          = data.get("description", ""),
            chef_de_projet_cuid  = data.get("chef_de_projet_cuid", ""),
            chef_de_projet_name  = data.get("chef_de_projet_name", ""),
            team                 = data.get("team", ""),
            source_vcenter       = data.get("source_vcenter", ""),
            target_openstack     = data.get("target_openstack", ""),
            priority             = data.get("priority", ProjectPriority.MEDIUM.value),
            status               = data.get("status", ProjectStatus.PLANNED.value),
            planned_start        = data.get("planned_start"),
            planned_end          = data.get("planned_end"),
            notes                = data.get("notes", ""),
        )
        if not proj:
            return jsonify({"error": err}), 400
        return jsonify({"message": "Projet créé", "project": proj.to_dict()}), 201

    @app.route("/api/v1/projects/<project_id>", methods=["GET"])
    @require_auth
    def get_project(project_id: str):
        ps   = get_project_store()
        proj = ps.get(project_id) or ps.get_by_code(project_id)
        if not proj:
            return jsonify({"error": f"Projet '{project_id}' introuvable"}), 404
        return jsonify(proj.to_dict())

    @app.route("/api/v1/projects/<project_id>", methods=["PUT"])
    @require_auth
    def update_project(project_id: str):
        ps   = get_project_store()
        data = request.get_json(force=True) or {}
        proj, err = ps.update(project_id, **data)
        if not proj:
            return jsonify({"error": err}), 404
        return jsonify({"message": "Projet mis à jour", "project": proj.to_dict()})

    @app.route("/api/v1/projects/<project_id>", methods=["DELETE"])
    @admin_only
    def delete_project(project_id: str):
        ps      = get_project_store()
        ok, msg = ps.delete(project_id)
        if not ok:
            return jsonify({"error": msg}), 400
        return jsonify({"message": msg})

    @app.route("/api/v1/projects/<project_id>/stats", methods=["GET"])
    @require_auth
    def project_stats(project_id: str):
        ps   = get_project_store()
        proj = ps.get(project_id)
        if not proj:
            return jsonify({"error": f"Projet '{project_id}' introuvable"}), 404
        orch  = get_orchestrator_fn()
        stats = ps.compute_stats(project_id, orch)
        return jsonify({**proj.to_dict(), **stats})

    @app.route("/api/v1/projects/<project_id>/jobs/<job_id>", methods=["POST"])
    @require_auth
    def attach_job(project_id: str, job_id: str):
        ps      = get_project_store()
        ok, err = ps.attach_job(project_id, job_id)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"message": f"Job {job_id} rattaché au projet"})

    @app.route("/api/v1/projects/<project_id>/jobs/<job_id>", methods=["DELETE"])
    @require_auth
    def detach_job(project_id: str, job_id: str):
        ps      = get_project_store()
        ok, err = ps.detach_job(project_id, job_id)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"message": f"Job {job_id} retiré du projet"})

    @app.route("/api/v1/jobs/<job_id>/project", methods=["GET"])
    @require_auth
    def job_project(job_id: str):
        ps   = get_project_store()
        proj = ps.find_by_job(job_id)
        if not proj:
            return jsonify({"project": None})
        return jsonify({"project": proj.to_dict()})
