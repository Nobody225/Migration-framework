"""
tests/unit/test_api.py
───────────────────────
Unit tests for the REST API endpoints.
Uses Flask test client — no real VMware or OpenStack connections.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.core.models import (
    MigrationJob, MigrationStatus, OpenStackTarget, MigrationMode,
    PerformanceReport, BenchmarkResult, PerformanceComparison,
)


# ════════════════════════════════════════════════════════════════
# FIXTURES
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_orchestrator():
    """Mock orchestrator that returns predictable data."""
    orch = MagicMock()

    # Sample completed job
    job = MigrationJob(
        vm_name="web-server-01",
        target=OpenStackTarget.REDHAT,
        mode=MigrationMode.COLD,
        operator="test-user",
    )
    job.status = MigrationStatus.COMPLETED

    from src.core.models import OpenStackInstance
    job.instance = OpenStackInstance(
        instance_id="inst-abc-123",
        name="web-server-01",
        target=OpenStackTarget.REDHAT,
        flavor_name="m1.medium",
        status="ACTIVE",
        ip_addresses={"PROD-NET": ["10.0.1.50"]},
    )

    report = PerformanceReport(
        job_id=job.job_id,
        vm_name="web-server-01",
        instance_id="inst-abc-123",
        target=OpenStackTarget.REDHAT,
        overall_score=87.5,
        regressions=[],
        recommendations=["Consider enabling huge pages"],
    )
    report.comparisons = [
        PerformanceComparison(
            metric_name="events_per_sec",
            vmware_value=1500.0,
            openstack_value=1600.0,
            unit="events/sec",
            threshold_pct=15.0,
        )
    ]
    job.performance_report = report

    orch.list_jobs.return_value = [job]
    orch.get_job.return_value   = job
    orch.cancel_job.return_value = True

    return orch, job


@pytest.fixture
def app_client(mock_orchestrator):
    """Flask test client with mocked orchestrator."""
    orch, job = mock_orchestrator

    with patch("src.api.app.build_framework", return_value=orch), \
         patch("src.api.app.load_config", return_value={"migration": {}, "vmware": {}, "openstack": {}}):

        from src.api.app import create_app
        flask_app, socketio = create_app("config/config.yaml")
        flask_app.config["TESTING"] = True

        with flask_app.test_client() as client:
            yield client, job


# ════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════

class TestHealth:

    def test_health_returns_200(self, app_client):
        client, _ = app_client
        r = client.get("/api/v1/health")
        assert r.status_code == 200

    def test_health_contains_required_fields(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/health").data)
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "version" in data
        assert "targets" in data

    def test_health_lists_all_targets(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/health").data)
        assert "redhat" in data["targets"]
        assert "huawei" in data["targets"]
        assert "custom" in data["targets"]


# ════════════════════════════════════════════════════════════════
# JOBS
# ════════════════════════════════════════════════════════════════

class TestJobsEndpoints:

    def test_list_jobs_returns_200(self, app_client):
        client, _ = app_client
        r = client.get("/api/v1/jobs")
        assert r.status_code == 200

    def test_list_jobs_returns_count_and_list(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/jobs").data)
        assert "count" in data
        assert "jobs" in data
        assert data["count"] == len(data["jobs"])

    def test_list_jobs_job_has_required_fields(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/jobs").data)
        job = data["jobs"][0]
        for field in ["job_id", "vm_name", "target", "status", "created_at"]:
            assert field in job, f"Missing field: {field}"

    def test_get_job_returns_200(self, app_client):
        client, job = app_client
        r = client.get(f"/api/v1/jobs/{job.job_id}")
        assert r.status_code == 200

    def test_get_job_contains_vm_name(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}").data)
        assert data["vm_name"] == "web-server-01"

    def test_get_unknown_job_returns_404(self, app_client):
        client, _ = app_client
        r = client.get("/api/v1/jobs/nonexistent-id")
        assert r.status_code == 404

    def test_get_job_logs_returns_200(self, app_client):
        client, job = app_client
        r = client.get(f"/api/v1/jobs/{job.job_id}/logs")
        assert r.status_code == 200

    def test_get_job_logs_has_entries_field(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}/logs").data)
        assert "entries" in data
        assert "count" in data

    def test_cancel_pending_job(self, app_client):
        client, job = app_client
        r = client.delete(f"/api/v1/jobs/{job.job_id}")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["status"] == "cancelled"


# ════════════════════════════════════════════════════════════════
# PERFORMANCE REPORT
# ════════════════════════════════════════════════════════════════

class TestReportEndpoint:

    def test_report_returns_200_for_completed_job(self, app_client):
        client, job = app_client
        r = client.get(f"/api/v1/jobs/{job.job_id}/report")
        assert r.status_code == 200

    def test_report_contains_score(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}/report").data)
        assert "overall_score" in data
        assert data["overall_score"] == 87.5

    def test_report_contains_comparisons(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}/report").data)
        assert "comparisons" in data
        assert len(data["comparisons"]) >= 1

    def test_report_comparison_has_delta(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}/report").data)
        comp = data["comparisons"][0]
        assert "delta_pct" in comp
        assert "regression" in comp

    def test_report_404_for_unknown_job(self, app_client):
        client, _ = app_client
        r = client.get("/api/v1/jobs/unknown/report")
        assert r.status_code == 404


# ════════════════════════════════════════════════════════════════
# RECOMMENDATIONS
# ════════════════════════════════════════════════════════════════

class TestRecommendationsEndpoint:

    def test_recommendations_returns_200(self, app_client):
        client, job = app_client
        r = client.get(f"/api/v1/jobs/{job.job_id}/recommendations")
        assert r.status_code == 200

    def test_recommendations_has_count(self, app_client):
        client, job = app_client
        data = json.loads(client.get(f"/api/v1/jobs/{job.job_id}/recommendations").data)
        assert "count" in data
        assert "recommendations" in data


# ════════════════════════════════════════════════════════════════
# MIGRATE
# ════════════════════════════════════════════════════════════════

class TestMigrateEndpoint:

    def test_migrate_without_vms_returns_400(self, app_client):
        client, _ = app_client
        r = client.post(
            "/api/v1/migrate",
            json={"target": "redhat"},
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_migrate_with_invalid_target_returns_400(self, app_client):
        client, _ = app_client
        r = client.post(
            "/api/v1/migrate",
            json={"vms": ["vm-01"], "target": "invalid_target"},
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_migrate_valid_request_returns_202(self, app_client):
        client, _ = app_client
        r = client.post(
            "/api/v1/migrate",
            json={"vms": ["web-server-01"], "target": "custom", "mode": "cold"},
            content_type="application/json",
        )
        assert r.status_code == 202

    def test_migrate_response_contains_message(self, app_client):
        client, _ = app_client
        r = client.post(
            "/api/v1/migrate",
            json={"vms": ["vm-01", "vm-02"], "target": "huawei"},
            content_type="application/json",
        )
        data = json.loads(r.data)
        assert "message" in data


# ════════════════════════════════════════════════════════════════
# STATS
# ════════════════════════════════════════════════════════════════

class TestStatsEndpoint:

    def test_stats_returns_200(self, app_client):
        client, _ = app_client
        r = client.get("/api/v1/stats")
        assert r.status_code == 200

    def test_stats_has_required_fields(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/stats").data)
        for field in ["total_jobs", "by_status", "success_rate_pct", "avg_perf_score"]:
            assert field in data, f"Missing field: {field}"

    def test_stats_success_rate_between_0_and_100(self, app_client):
        client, _ = app_client
        data = json.loads(client.get("/api/v1/stats").data)
        assert 0 <= data["success_rate_pct"] <= 100
