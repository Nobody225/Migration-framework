"""
tests/unit/test_evaluator_optimizer.py
────────────────────────────────────────
Unit tests for Evaluator and Optimizer modules.
No real SSH connections — all network I/O is mocked.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from src.core.models import (
    BenchmarkResult, OpenStackInstance, OpenStackTarget,
    PerformanceComparison, PerformanceReport,
    VMwareNIC, VMwarePerformanceMetrics, VMwareVM,
    PowerState,
)
from src.evaluator.evaluator import PerformanceEvaluator
from src.evaluator.ssh_client import SSHClient, SSHClientError
from src.optimizer.optimizer import ResourceOptimizer


# ════════════════════════════════════════════════════════════════
# FIXTURES
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def eval_config():
    return {
        "ssh_user":          "root",
        "ssh_key_path":      "~/.ssh/test_key",
        "wait_after_boot_s": 0,   # No waiting in tests
        "sysbench": {
            "threads": 2,
            "cpu_max_prime": 1000,
            "duration_s": 5,
        },
        "fio": {
            "runtime_s":    5,
            "block_size_kb": 4,
            "jobs":         2,
            "iodepth":      8,
        },
        "iperf3": {
            "server":     "192.168.1.100",
            "duration_s": 5,
            "streams":    2,
        },
        "thresholds": {
            "cpu_pct":        15,
            "memory_pct":     10,
            "disk_iops_pct":  20,
            "network_bw_pct": 15,
        },
    }


@pytest.fixture
def opt_config():
    return {}


@pytest.fixture
def sample_vm() -> VMwareVM:
    return VMwareVM(
        mor_id="vm-100",
        name="app-server-01",
        num_cpus=4,
        num_cores_per_socket=1,
        memory_mb=8192,
        power_state=PowerState.STOPPED,
        nics=[
            VMwareNIC(
                label="NIC 1",
                network_name="PROD-NET",
                ip_addresses=["192.168.10.50"],
            )
        ],
        performance=VMwarePerformanceMetrics(
            cpu_usage_percent=25.0,
            cpu_usage_mhz=800.0,
            memory_active_mb=3000.0,
            memory_balloon_mb=0.0,
            disk_read_kbps=5000.0,
            disk_write_kbps=2000.0,
            disk_read_iops=500.0,
            disk_write_iops=200.0,
        ),
    )


@pytest.fixture
def sample_instance() -> OpenStackInstance:
    return OpenStackInstance(
        instance_id="os-instance-abc123",
        name="app-server-01",
        target=OpenStackTarget.REDHAT,
        flavor_name="m1.medium",
        status="ACTIVE",
        ip_addresses={"PROD-NET": ["10.0.1.50"]},
    )


@pytest.fixture
def sample_benchmark_results_vmware():
    return [
        BenchmarkResult(
            tool="sysbench", test_name="cpu_prime",
            metric_name="events_per_sec",
            value=1500.0, unit="events/sec",
            environment="vmware", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="sysbench", test_name="memory_read",
            metric_name="bandwidth_mib_sec",
            value=8000.0, unit="MiB/sec",
            environment="vmware", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="fio", test_name="rand_read",
            metric_name="rand_read_read_iops",
            value=20000.0, unit="IOPS",
            environment="vmware", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="iperf3", test_name="tcp_send",
            metric_name="tcp_send",
            value=900.0, unit="Mbps",
            environment="vmware", timestamp=datetime.now(),
        ),
    ]


@pytest.fixture
def sample_benchmark_results_openstack_good():
    """Results slightly better than VMware."""
    return [
        BenchmarkResult(
            tool="sysbench", test_name="cpu_prime",
            metric_name="events_per_sec",
            value=1600.0, unit="events/sec",
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="sysbench", test_name="memory_read",
            metric_name="bandwidth_mib_sec",
            value=8200.0, unit="MiB/sec",
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="fio", test_name="rand_read",
            metric_name="rand_read_read_iops",
            value=21000.0, unit="IOPS",
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="iperf3", test_name="tcp_send",
            metric_name="tcp_send",
            value=950.0, unit="Mbps",
            environment="openstack", timestamp=datetime.now(),
        ),
    ]


@pytest.fixture
def sample_benchmark_results_openstack_bad():
    """Results significantly worse than VMware — should trigger regressions."""
    return [
        BenchmarkResult(
            tool="sysbench", test_name="cpu_prime",
            metric_name="events_per_sec",
            value=900.0, unit="events/sec",    # -40% CPU regression
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="sysbench", test_name="memory_read",
            metric_name="bandwidth_mib_sec",
            value=7200.0, unit="MiB/sec",      # -10% memory regression
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="fio", test_name="rand_read",
            metric_name="rand_read_read_iops",
            value=12000.0, unit="IOPS",        # -40% disk regression
            environment="openstack", timestamp=datetime.now(),
        ),
        BenchmarkResult(
            tool="iperf3", test_name="tcp_send",
            metric_name="tcp_send",
            value=600.0, unit="Mbps",          # -33% network regression
            environment="openstack", timestamp=datetime.now(),
        ),
    ]


# ════════════════════════════════════════════════════════════════
# SSHClient
# ════════════════════════════════════════════════════════════════

class TestSSHClient:

    def test_connect_success(self):
        with patch("paramiko.SSHClient") as mock_paramiko:
            mock_ssh = MagicMock()
            mock_paramiko.return_value = mock_ssh

            client = SSHClient("10.0.0.1", "root", password="pass")
            result = client.connect()

            assert result is True
            assert client.is_connected()

    def test_run_returns_stdout_stderr_rc(self):
        with patch("paramiko.SSHClient") as mock_paramiko:
            mock_ssh   = MagicMock()
            mock_chan   = MagicMock()
            mock_stdout = MagicMock()
            mock_stderr = MagicMock()

            mock_stdout.read.return_value = b"hello world"
            mock_stderr.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 0

            mock_ssh.exec_command.return_value = (mock_chan, mock_stdout, mock_stderr)
            mock_paramiko.return_value = mock_ssh

            client = SSHClient("10.0.0.1", "root", password="pass")
            client._client = mock_ssh

            stdout, stderr, rc = client.run("echo hello")
            assert stdout == "hello world"
            assert rc == 0

    def test_check_tool_found(self):
        client = SSHClient("10.0.0.1", "root")
        client.run = MagicMock(return_value=("/usr/bin/sysbench", "", 0))
        assert client.check_tool("sysbench") is True

    def test_check_tool_not_found(self):
        client = SSHClient("10.0.0.1", "root")
        client.run = MagicMock(return_value=("", "", 1))
        assert client.check_tool("sysbench") is False


# ════════════════════════════════════════════════════════════════
# PerformanceEvaluator — comparisons
# ════════════════════════════════════════════════════════════════

class TestPerformanceEvaluator:

    def test_compute_comparisons_matches_metrics(
        self, eval_config,
        sample_benchmark_results_vmware,
        sample_benchmark_results_openstack_good,
    ):
        evaluator = PerformanceEvaluator(eval_config)
        comparisons = evaluator._compute_comparisons(
            sample_benchmark_results_vmware,
            sample_benchmark_results_openstack_good,
        )
        assert len(comparisons) == 4
        metric_names = {c.metric_name for c in comparisons}
        assert "events_per_sec" in metric_names
        assert "bandwidth_mib_sec" in metric_names

    def test_no_regressions_when_openstack_is_better(
        self, eval_config,
        sample_benchmark_results_vmware,
        sample_benchmark_results_openstack_good,
    ):
        evaluator = PerformanceEvaluator(eval_config)
        comparisons = evaluator._compute_comparisons(
            sample_benchmark_results_vmware,
            sample_benchmark_results_openstack_good,
        )
        regressions = evaluator._find_regressions(comparisons)
        assert len(regressions) == 0

    def test_regressions_detected_when_openstack_is_worse(
        self, eval_config,
        sample_benchmark_results_vmware,
        sample_benchmark_results_openstack_bad,
    ):
        evaluator = PerformanceEvaluator(eval_config)
        comparisons = evaluator._compute_comparisons(
            sample_benchmark_results_vmware,
            sample_benchmark_results_openstack_bad,
        )
        regressions = evaluator._find_regressions(comparisons)
        assert len(regressions) >= 1

    def test_score_100_when_no_regressions(
        self, eval_config,
        sample_benchmark_results_vmware,
        sample_benchmark_results_openstack_good,
    ):
        evaluator = PerformanceEvaluator(eval_config)
        comparisons = evaluator._compute_comparisons(
            sample_benchmark_results_vmware,
            sample_benchmark_results_openstack_good,
        )
        score = evaluator._compute_score(comparisons)
        assert score == 100.0

    def test_score_reduced_when_regressions(
        self, eval_config,
        sample_benchmark_results_vmware,
        sample_benchmark_results_openstack_bad,
    ):
        evaluator = PerformanceEvaluator(eval_config)
        comparisons = evaluator._compute_comparisons(
            sample_benchmark_results_vmware,
            sample_benchmark_results_openstack_bad,
        )
        score = evaluator._compute_score(comparisons)
        assert score < 100.0

    def test_recommendations_generated_for_cpu_regression(self, eval_config):
        evaluator = PerformanceEvaluator(eval_config)
        report = PerformanceReport(
            job_id="test", vm_name="vm-01",
            instance_id="inst-01", target=OpenStackTarget.REDHAT,
        )
        report.comparisons = [
            PerformanceComparison(
                metric_name="events_per_sec",
                vmware_value=1500.0,
                openstack_value=900.0,
                unit="events/sec",
                threshold_pct=15.0,
            )
        ]
        report.regressions = ["events_per_sec: regression"]
        recs = evaluator._generate_recommendations(report)
        assert len(recs) >= 1
        assert any("cpu" in r.lower() or "dedicated" in r.lower() for r in recs)

    def test_get_vm_ip_extracts_ipv4(self, eval_config, sample_vm):
        evaluator = PerformanceEvaluator(eval_config)
        ip = evaluator._get_vm_ip(sample_vm)
        assert ip == "192.168.10.50"

    def test_get_vm_ip_ignores_ipv6(self, eval_config):
        evaluator = PerformanceEvaluator(eval_config)
        vm = VMwareVM(
            mor_id="vm-99", name="test",
            nics=[VMwareNIC(
                label="NIC 1",
                network_name="NET",
                ip_addresses=["fe80::1", "192.168.1.5"],
            )]
        )
        ip = evaluator._get_vm_ip(vm)
        assert ip == "192.168.1.5"

    def test_get_instance_ip(self, eval_config, sample_instance):
        evaluator = PerformanceEvaluator(eval_config)
        ip = evaluator._get_instance_ip(sample_instance)
        assert ip == "10.0.1.50"


# ════════════════════════════════════════════════════════════════
# ResourceOptimizer
# ════════════════════════════════════════════════════════════════

class TestResourceOptimizer:

    def test_cpu_recommendation_for_underutilized_vm(self, opt_config, sample_vm):
        """VM has 4 vCPUs but only uses 25% — should recommend 2."""
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_cpu(sample_vm)
        assert rec is not None
        assert rec.resource == "vcpu"
        assert rec.recommended_value < sample_vm.total_vcpus

    def test_no_cpu_recommendation_without_metrics(self, opt_config, sample_vm):
        sample_vm.performance = None
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_cpu(sample_vm)
        assert rec is None

    def test_ram_recommendation_for_underutilized_vm(self, opt_config, sample_vm):
        """VM has 8GB but only 3GB active RAM — should recommend smaller."""
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_ram(sample_vm)
        assert rec is not None
        assert rec.resource == "ram"

    def test_no_ram_recommendation_without_metrics(self, opt_config, sample_vm):
        sample_vm.performance = None
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_ram(sample_vm)
        assert rec is None

    def test_flavor_recommendation_produced(self, opt_config, sample_vm):
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_flavor(sample_vm)
        # sample_vm has 4 vCPU / 8GB but uses ~25% CPU / ~3GB RAM
        # should recommend a smaller flavor
        assert rec is not None
        assert rec.resource == "flavor"

    def test_full_recommend_returns_list(self, opt_config, sample_vm, sample_instance):
        optimizer = ResourceOptimizer(opt_config)
        recs = optimizer.recommend(instance=sample_instance, source_vm=sample_vm)
        assert isinstance(recs, list)
        assert len(recs) >= 1

    def test_no_recommendations_without_vm(self, opt_config, sample_instance):
        optimizer = ResourceOptimizer(opt_config)
        recs = optimizer.recommend(instance=sample_instance, source_vm=None)
        assert recs == []

    def test_disk_recommendation_on_regression(self, opt_config, sample_vm, sample_instance):
        optimizer = ResourceOptimizer(opt_config)
        report = PerformanceReport(
            job_id="t", vm_name="vm",
            instance_id="i", target=OpenStackTarget.REDHAT,
        )
        report.comparisons = [
            PerformanceComparison(
                metric_name="rand_read_read_iops",
                vmware_value=20000.0,
                openstack_value=8000.0,
                unit="IOPS",
                threshold_pct=20.0,
            )
        ]
        rec = optimizer._recommend_disk_type(sample_vm, report)
        assert rec is not None
        assert rec.resource == "disk_type"
        assert rec.priority == "high"

    def test_saving_percent_is_positive(self, opt_config, sample_vm):
        optimizer = ResourceOptimizer(opt_config)
        rec = optimizer._recommend_cpu(sample_vm)
        assert rec is not None
        assert rec.estimated_saving_percent > 0
