"""
src/api/project_store.py
──────────────────────────
Project model and persistent store.

A Project groups multiple migration jobs under a common context:
  - Name, code, description
  - Chef de projet (CUID)
  - Source vCenter + target OpenStack
  - Status: planned / in_progress / completed / suspended
  - All linked job IDs

Persisted in config/projects.json.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

_STORE_PATH = os.environ.get(
    "PROJECT_STORE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "projects.json"),
)


# ════════════════════════════════════════════════════════════════
# ENUMS
# ════════════════════════════════════════════════════════════════

class ProjectStatus(Enum):
    PLANNED    = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED  = "completed"
    SUSPENDED  = "suspended"

class ProjectPriority(Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    CRITICAL = "critical"


# ════════════════════════════════════════════════════════════════
# PROJECT MODEL
# ════════════════════════════════════════════════════════════════

class Project:
    def __init__(
        self,
        name: str,
        code: str,
        description: str = "",
        chef_de_projet_cuid: str = "",
        chef_de_projet_name: str = "",
        team: str = "",
        source_vcenter: str = "",
        target_openstack: str = "",
        priority: str = ProjectPriority.MEDIUM.value,
        status: str = ProjectStatus.PLANNED.value,
        planned_start: Optional[str] = None,
        planned_end: Optional[str] = None,
        notes: str = "",
        job_ids: Optional[List[str]] = None,
        project_id: Optional[str] = None,
        created_by: str = "system",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        self.project_id          = project_id or str(uuid.uuid4())
        self.name                = name.strip()
        self.code                = code.strip().upper()
        self.description         = description
        self.chef_de_projet_cuid = chef_de_projet_cuid.strip().upper()
        self.chef_de_projet_name = chef_de_projet_name.strip()
        self.team                = team.strip()
        self.source_vcenter      = source_vcenter
        self.target_openstack    = target_openstack
        self.priority            = priority
        self.status              = status
        self.planned_start       = planned_start
        self.planned_end         = planned_end
        self.notes               = notes
        self.job_ids             = job_ids or []
        self.created_by          = created_by
        self.created_at          = created_at or datetime.utcnow().isoformat()
        self.updated_at          = updated_at or self.created_at

    def add_job(self, job_id: str) -> None:
        if job_id not in self.job_ids:
            self.job_ids.append(job_id)
            self.updated_at = datetime.utcnow().isoformat()
            # Auto-transition to in_progress
            if self.status == ProjectStatus.PLANNED.value:
                self.status = ProjectStatus.IN_PROGRESS.value

    def remove_job(self, job_id: str) -> None:
        if job_id in self.job_ids:
            self.job_ids.remove(job_id)
            self.updated_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "project_id":          self.project_id,
            "name":                self.name,
            "code":                self.code,
            "description":         self.description,
            "chef_de_projet_cuid": self.chef_de_projet_cuid,
            "chef_de_projet_name": self.chef_de_projet_name,
            "team":                self.team,
            "source_vcenter":      self.source_vcenter,
            "target_openstack":    self.target_openstack,
            "priority":            self.priority,
            "status":              self.status,
            "planned_start":       self.planned_start,
            "planned_end":         self.planned_end,
            "notes":               self.notes,
            "job_ids":             self.job_ids,
            "job_count":           len(self.job_ids),
            "created_by":          self.created_by,
            "created_at":          self.created_at,
            "updated_at":          self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__init__.__code__.co_varnames})


# ════════════════════════════════════════════════════════════════
# PROJECT STORE
# ════════════════════════════════════════════════════════════════

class ProjectStore:
    def __init__(self, path: str = _STORE_PATH):
        self._path: str = os.path.abspath(path)
        self._projects: Dict[str, Project] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for p in data.get("projects", []):
                proj = Project.from_dict(p)
                self._projects[proj.project_id] = proj
        except Exception as exc:
            import logging
            logging.getLogger("migration.projects").warning(
                f"Could not load project store: {exc}"
            )

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {
            "version":    "1.0",
            "updated_at": datetime.utcnow().isoformat(),
            "projects":   [p.to_dict() for p in self._projects.values()],
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ── CRUD ──────────────────────────────────────────────────

    def create(
        self,
        name: str,
        code: str,
        created_by: str,
        **kwargs,
    ) -> tuple[Optional[Project], str]:
        # Code must be unique
        code_up = code.strip().upper()
        if any(p.code == code_up for p in self._projects.values()):
            return None, f"Le code projet '{code_up}' existe déjà"
        if not name.strip():
            return None, "Le nom du projet est requis"

        proj = Project(name=name, code=code, created_by=created_by, **kwargs)
        self._projects[proj.project_id] = proj
        self._save()
        return proj, ""

    def get(self, project_id: str) -> Optional[Project]:
        return self._projects.get(project_id)

    def get_by_code(self, code: str) -> Optional[Project]:
        code_up = code.strip().upper()
        return next((p for p in self._projects.values() if p.code == code_up), None)

    def list_all(
        self,
        status: Optional[str] = None,
        vcenter: Optional[str] = None,
        target: Optional[str] = None,
    ) -> List[Project]:
        projects = list(self._projects.values())
        if status:
            projects = [p for p in projects if p.status == status]
        if vcenter:
            projects = [p for p in projects if p.source_vcenter == vcenter]
        if target:
            projects = [p for p in projects if p.target_openstack == target]
        # Sort: in_progress first, then by created_at desc
        order = {
            ProjectStatus.IN_PROGRESS.value: 0,
            ProjectStatus.PLANNED.value:     1,
            ProjectStatus.SUSPENDED.value:   2,
            ProjectStatus.COMPLETED.value:   3,
        }
        return sorted(projects, key=lambda p: (order.get(p.status, 9), p.created_at))

    def update(self, project_id: str, **kwargs) -> tuple[Optional[Project], str]:
        proj = self._projects.get(project_id)
        if not proj:
            return None, f"Projet '{project_id}' introuvable"
        allowed = {
            "name", "description", "chef_de_projet_cuid", "chef_de_projet_name",
            "team", "source_vcenter", "target_openstack", "priority",
            "status", "planned_start", "planned_end", "notes",
        }
        for k, v in kwargs.items():
            if k in allowed:
                setattr(proj, k, v)
        proj.updated_at = datetime.utcnow().isoformat()
        self._save()
        return proj, ""

    def delete(self, project_id: str) -> tuple[bool, str]:
        proj = self._projects.get(project_id)
        if not proj:
            return False, f"Projet '{project_id}' introuvable"
        if proj.job_ids:
            return False, f"Impossible de supprimer un projet contenant {len(proj.job_ids)} migration(s)"
        del self._projects[project_id]
        self._save()
        return True, "Projet supprimé"

    def attach_job(self, project_id: str, job_id: str) -> tuple[bool, str]:
        proj = self._projects.get(project_id)
        if not proj:
            return False, f"Projet '{project_id}' introuvable"
        proj.add_job(job_id)
        self._save()
        return True, ""

    def detach_job(self, project_id: str, job_id: str) -> tuple[bool, str]:
        proj = self._projects.get(project_id)
        if not proj:
            return False, f"Projet '{project_id}' introuvable"
        proj.remove_job(job_id)
        self._save()
        return True, ""

    def find_by_job(self, job_id: str) -> Optional[Project]:
        """Return the project that contains a given job ID."""
        return next(
            (p for p in self._projects.values() if job_id in p.job_ids),
            None,
        )

    def compute_stats(self, project_id: str, orchestrator) -> dict:
        """
        Compute live KPIs for a project by querying the orchestrator
        for job details.
        """
        proj = self._projects.get(project_id)
        if not proj:
            return {}

        jobs = [orchestrator.get_job(jid) for jid in proj.job_ids]
        jobs = [j for j in jobs if j]

        total        = len(jobs)
        completed    = sum(1 for j in jobs if j.status.value == "completed")
        failed       = sum(1 for j in jobs if j.status.value == "failed")
        in_progress  = sum(1 for j in jobs if j.status.value not in (
            "completed", "failed", "rolled_back", "cancelled", "pending"))
        pending      = sum(1 for j in jobs if j.status.value == "pending")

        scores = [j.performance_report.overall_score
                  for j in jobs if j.performance_report]
        avg_score = round(sum(scores) / len(scores), 1) if scores else None

        durations = [j.duration_seconds for j in jobs if j.duration_seconds]
        total_duration_s = round(sum(durations), 1) if durations else None
        avg_duration_s   = round(sum(durations) / len(durations), 1) if durations else None

        progress_pct = round(completed / total * 100, 1) if total else 0

        return {
            "project_id":       project_id,
            "total_jobs":       total,
            "completed":        completed,
            "failed":           failed,
            "in_progress":      in_progress,
            "pending":          pending,
            "progress_pct":     progress_pct,
            "avg_perf_score":   avg_score,
            "total_duration_s": total_duration_s,
            "avg_duration_s":   avg_duration_s,
            "jobs": [
                {
                    "job_id":     j.job_id,
                    "vm_name":    j.vm_name,
                    "status":     j.status.value,
                    "target":     j.target.value,
                    "perf_score": j.performance_report.overall_score
                                  if j.performance_report else None,
                    "duration_s": j.duration_seconds,
                    "started_at":   j.started_at.isoformat() if j.started_at else None,
                    "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                }
                for j in jobs
            ],
        }


# ── Singleton ─────────────────────────────────────────────────
_project_store: Optional[ProjectStore] = None

def get_project_store() -> ProjectStore:
    global _project_store
    if _project_store is None:
        _project_store = ProjectStore()
    return _project_store
