"""
src/evaluator/evaluator.py
───────────────────────────
Performance evaluator — orchestrates all benchmarks and produces
a PerformanceReport comparing VMware baseline vs OpenStack results.

Workflow:
  1. Connect SSH to source VM (VMware) → run all benchmarks → collect baseline
  2. Wait for OpenStack instance to be fully booted
  3. Connect SSH to OpenStack instance → run same benchmarks
  4. Compare results → compute deltas → flag regressions
  5. Return PerformanceReport with score, regressions, recommendations
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.models import (
    BenchmarkResult, OpenStackInstance, PerformanceComparison,
    PerformanceReport, VMwareVM,
)
from src.evaluator.ssh_client import SSHClient, SSHClientError
from src.evaluator.benchmarks import (
    CPUBenchmark, MemoryBenchmark, DiskBenchmark, NetworkBenchmark,
)

logger = logging.getLogger("migration.evaluator")


class PerformanceEvaluator:
    """
    Runs benchmarks on source VM and migrated instance,
    then produces a comparative PerformanceReport.

    All benchmarks are optional — if a tool is unavailable,
    that metric is skipped with a warning (non-blocking).

    Config keys (from config.yaml [evaluation]):
      ssh_user           : SSH username on VMs (default: root)
      ssh_key_path       : Path to SSH private key
      wait_after_boot_s  : Seconds to wait after instance boot before benchmarking
      sysbench.*         : sysbench config
      fio.*              : fio config
      iperf3.*           : iperf3 config
      thresholds.*       : regression alert thresholds (percent)
    """

    def __init__(self, config: Dict[str, Any]):
        self.config         = config
        self.ssh_user       = config.get("ssh_user", "root")
        self.ssh_key_path   = config.get("ssh_key_path", "~/.ssh/id_rsa")
        self.wait_boot_s    = config.get("wait_after_boot_s", 60)
        self.thresholds     = config.get("thresholds", {
            "cpu_pct":        15,
            "memory_pct":     10,
            "disk_iops_pct":  20,
            "network_bw_pct": 15,
        })

        # Instantiate benchmark runners
        self._cpu   = CPUBenchmark(config.get("sysbench", {}))
        self._mem   = MemoryBenchmark(config.get("sysbench", {}))
        self._disk  = DiskBenchmark(config.get("fio", {}))
        self._net   = NetworkBenchmark(config.get("iperf3", {}))

    # ════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════

    def benchmark_vm(
        self,
        ip_address: str,
        environment: str,
        ssh_password: Optional[str] = None,
    ) -> List[BenchmarkResult]:
        """
        Run all benchmarks on a single host (VMware VM or OpenStack instance).

        Args:
            ip_address:   Reachable IP of the target host
            environment:  "vmware" or "openstack" — labels the results
            ssh_password: Optional password (key auth preferred)

        Returns:
            List of BenchmarkResult — may be empty if SSH fails.
        """
        results: List[BenchmarkResult] = []

        client = SSHClient(
            host=ip_address,
            username=self.ssh_user,
            key_path=self.ssh_key_path,
            password=ssh_password,
        )

        try:
            client.connect()
            logger.info(f"Running benchmarks on {ip_address} ({environment})")

            # CPU
            try:
                cpu_results = self._cpu.run(client, environment)
                results.extend(cpu_results)
                logger.info(f"CPU benchmark: {len(cpu_results)} metrics collected")
            except Exception as e:
                logger.warning(f"CPU benchmark failed: {e}")

            # Memory
            try:
                mem_results = self._mem.run(client, environment)
                results.extend(mem_results)
                logger.info(f"Memory benchmark: {len(mem_results)} metrics collected")
            except Exception as e:
                logger.warning(f"Memory benchmark failed: {e}")

            # Disk
            try:
                disk_results = self._disk.run(client, environment)
                results.extend(disk_results)
                logger.info(f"Disk benchmark: {len(disk_results)} metrics collected")
            except Exception as e:
                logger.warning(f"Disk benchmark failed: {e}")

            # Network
            try:
                net_results = self._net.run(client, environment)
                results.extend(net_results)
                logger.info(f"Network benchmark: {len(net_results)} metrics collected")
            except Exception as e:
                logger.warning(f"Network benchmark failed: {e}")

        except SSHClientError as e:
            logger.error(f"SSH connection failed to {ip_address}: {e}")
        finally:
            client.disconnect()

        logger.info(f"Total metrics collected from {ip_address}: {len(results)}")
        return results

    def compare(
        self,
        source_vm: VMwareVM,
        instance: OpenStackInstance,
        vmware_ip: Optional[str] = None,
    ) -> PerformanceReport:
        """
        Full comparative evaluation: VMware → OpenStack.

        Steps:
          1. Run benchmarks on VMware VM (using vmware_ip or first NIC IP)
          2. Wait for OpenStack instance to stabilize
          3. Run benchmarks on OpenStack instance
          4. Compute deltas and generate report

        Args:
            source_vm:  VMware source VM (for IP and metadata)
            instance:   Deployed OpenStack instance
            vmware_ip:  Override IP for VMware VM (if not in NIC info)
        """
        report = PerformanceReport(
            job_id="",
            vm_name=source_vm.name,
            instance_id=instance.instance_id,
            target=instance.target,
        )

        # ── Step 1: Benchmark VMware source ──────────────────────
        vm_ip = vmware_ip or self._get_vm_ip(source_vm)
        if vm_ip:
            logger.info(f"Benchmarking VMware source: {source_vm.name} ({vm_ip})")
            report.vmware_benchmarks = self.benchmark_vm(vm_ip, "vmware")
        else:
            logger.warning(f"No IP found for VMware VM '{source_vm.name}' — skipping VMware benchmarks")

        # ── Step 2: Wait for instance to stabilize ────────────────
        if self.wait_boot_s > 0:
            logger.info(f"Waiting {self.wait_boot_s}s for instance to stabilize...")
            time.sleep(self.wait_boot_s)

        # ── Step 3: Benchmark OpenStack instance ──────────────────
        os_ip = self._get_instance_ip(instance)
        if os_ip:
            logger.info(f"Benchmarking OpenStack instance: {instance.name} ({os_ip})")

            # Wait for SSH to become available
            ssh_client = SSHClient(
                host=os_ip,
                username=self.ssh_user,
                key_path=self.ssh_key_path,
            )
            try:
                ssh_client.wait_for_ssh(max_wait_s=300)
                ssh_client.disconnect()
            except SSHClientError as e:
                logger.error(f"Instance SSH not available: {e}")
                report.evaluated_at = datetime.now()
                return report

            report.openstack_benchmarks = self.benchmark_vm(os_ip, "openstack")
        else:
            logger.warning(f"No IP found for instance '{instance.name}' — skipping OpenStack benchmarks")

        # ── Step 4: Compare and score ─────────────────────────────
        if report.vmware_benchmarks and report.openstack_benchmarks:
            report.comparisons = self._compute_comparisons(
                report.vmware_benchmarks,
                report.openstack_benchmarks,
            )
            report.regressions     = self._find_regressions(report.comparisons)
            report.overall_score   = self._compute_score(report.comparisons)
            report.recommendations = self._generate_recommendations(report)

        report.evaluated_at = datetime.now()

        logger.info(
            f"Evaluation complete: score={report.overall_score:.1f}/100 | "
            f"regressions={len(report.regressions)} | "
            f"vmware_metrics={len(report.vmware_benchmarks)} | "
            f"openstack_metrics={len(report.openstack_benchmarks)}"
        )
        return report

    # ════════════════════════════════════════════════════════════
    # COMPARISON LOGIC
    # ════════════════════════════════════════════════════════════

    def _compute_comparisons(
        self,
        vmware_results: List[BenchmarkResult],
        openstack_results: List[BenchmarkResult],
    ) -> List[PerformanceComparison]:
        """
        Match VMware and OpenStack results by metric_name and compute deltas.
        Only compares metrics that exist in both environments.
        """
        comparisons = []

        # Build lookup: metric_name → BenchmarkResult for each environment
        vm_by_metric = {r.metric_name: r for r in vmware_results}
        os_by_metric = {r.metric_name: r for r in openstack_results}

        # Threshold map by metric category
        def threshold_for(metric: str) -> float:
            if "cpu" in metric or "events" in metric:
                return float(self.thresholds.get("cpu_pct", 15))
            if "memory" in metric or "bandwidth" in metric:
                return float(self.thresholds.get("memory_pct", 10))
            if "iops" in metric or "disk" in metric:
                return float(self.thresholds.get("disk_iops_pct", 20))
            if "network" in metric or "tcp" in metric or "udp" in metric:
                return float(self.thresholds.get("network_bw_pct", 15))
            return 15.0

        for metric_name in vm_by_metric:
            if metric_name not in os_by_metric:
                continue
            vm_r = vm_by_metric[metric_name]
            os_r = os_by_metric[metric_name]

            comp = PerformanceComparison(
                metric_name=metric_name,
                vmware_value=vm_r.value,
                openstack_value=os_r.value,
                unit=vm_r.unit,
                threshold_pct=threshold_for(metric_name),
            )
            comparisons.append(comp)

        return comparisons

    def _find_regressions(
        self, comparisons: List[PerformanceComparison]
    ) -> List[str]:
        """Return human-readable regression descriptions."""
        regressions = []
        for c in comparisons:
            if c.is_regression:
                regressions.append(
                    f"{c.metric_name}: {c.vmware_value:.2f} → {c.openstack_value:.2f} "
                    f"{c.unit} (delta: {c.delta_percent:+.1f}%, threshold: -{c.threshold_pct:.0f}%)"
                )
        return regressions

    def _compute_score(self, comparisons: List[PerformanceComparison]) -> float:
        """
        Compute a 0–100 overall migration score.
        Starts at 100, deducts points per regression proportional to severity.
        """
        if not comparisons:
            return 0.0

        score = 100.0
        for c in comparisons:
            if c.is_regression:
                # Deduction: proportional to how far beyond threshold we are
                excess = abs(c.delta_percent) - c.threshold_pct
                deduction = min(excess * 0.5, 20.0)  # max 20 points per metric
                score -= deduction

        return max(0.0, round(score, 1))

    def _generate_recommendations(self, report: PerformanceReport) -> List[str]:
        """Generate actionable recommendations based on regressions."""
        recs = []

        for c in report.comparisons:
            if not c.is_regression:
                continue

            if "cpu" in c.metric_name or "events" in c.metric_name:
                recs.append(
                    f"CPU regression detected ({c.delta_percent:+.1f}%). "
                    "Consider using a flavor with dedicated CPUs (hw:cpu_policy=dedicated) "
                    "or enabling CPU pinning on the hypervisor."
                )
            elif "disk" in c.metric_name or "iops" in c.metric_name:
                recs.append(
                    f"Disk I/O regression ({c.delta_percent:+.1f}%). "
                    "Try switching to a higher-performance volume type (SSD/NVMe) "
                    "or increase queue depth in the application."
                )
            elif "memory" in c.metric_name:
                recs.append(
                    f"Memory bandwidth regression ({c.delta_percent:+.1f}%). "
                    "Consider enabling huge pages (hw:mem_page_size=large) on the flavor."
                )
            elif "network" in c.metric_name or "tcp" in c.metric_name:
                recs.append(
                    f"Network throughput regression ({c.delta_percent:+.1f}%). "
                    "Verify SR-IOV is enabled or switch to a DPDK-accelerated vNIC."
                )

        # Deduplicate recommendations
        return list(dict.fromkeys(recs))

    # ════════════════════════════════════════════════════════════
    # HELPERS
    # ════════════════════════════════════════════════════════════

    def _get_vm_ip(self, vm: VMwareVM) -> Optional[str]:
        """Extract first available IPv4 from VMware NIC info."""
        for nic in vm.nics:
            for ip in nic.ip_addresses:
                if ip and not ip.startswith("169.254") and ":" not in ip:
                    return ip
        return None

    def _get_instance_ip(self, instance: OpenStackInstance) -> Optional[str]:
        """Extract first available IPv4 from OpenStack instance addresses."""
        for network, addresses in instance.ip_addresses.items():
            for addr in addresses:
                if addr and not addr.startswith("169.254") and ":" not in addr:
                    return addr
        return None
