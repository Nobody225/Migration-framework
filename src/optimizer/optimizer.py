"""
src/optimizer/optimizer.py
───────────────────────────
Resource optimizer — analyzes VM usage and recommends right-sizing.

Compares:
  - Allocated resources (flavor spec)
  - Actually observed usage (VMware performance metrics + post-migration benchmarks)

Produces OptimizationRecommendation objects with concrete flavor suggestions.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from src.core.models import (
    OpenStackInstance, OptimizationRecommendation,
    PerformanceReport, VMwareVM,
)

logger = logging.getLogger("migration.optimizer")

# Right-sizing safety margins
_CPU_HEADROOM    = 1.25   # Keep 25% above peak observed usage
_RAM_HEADROOM    = 1.30   # Keep 30% above peak active RAM
_DISK_HEADROOM   = 1.10   # Keep 10% above used disk space

# Standard flavors to snap to (vcpus, ram_mb, disk_gb)
_STANDARD_FLAVORS = [
    (1,  1024,  10), (1,  2048,  20), (1,  4096,  40),
    (2,  2048,  20), (2,  4096,  40), (2,  8192,  80),
    (4,  4096,  40), (4,  8192,  80), (4, 16384, 160),
    (8,  8192,  80), (8, 16384, 160), (8, 32768, 320),
    (16, 16384, 160), (16, 32768, 320), (16, 65536, 640),
]


class ResourceOptimizer:
    """
    Analyzes VM resource usage and produces right-sizing recommendations.

    Uses VMware performance metrics as the primary signal.
    Falls back to allocated specs if metrics are unavailable.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def recommend(
        self,
        instance: OpenStackInstance,
        source_vm: Optional[VMwareVM] = None,
        performance_report: Optional[PerformanceReport] = None,
    ) -> List[OptimizationRecommendation]:
        """
        Produce a list of right-sizing recommendations.

        Priority of signals (highest to lowest):
          1. VMware performance metrics (actual observed usage)
          2. PerformanceReport benchmark results
          3. Allocated VM specs (conservative fallback)
        """
        recommendations: List[OptimizationRecommendation] = []

        if not source_vm:
            logger.warning("No source VM data — cannot produce optimization recommendations")
            return recommendations

        # ── CPU right-sizing ──────────────────────────────────────
        cpu_rec = self._recommend_cpu(source_vm)
        if cpu_rec:
            recommendations.append(cpu_rec)

        # ── RAM right-sizing ──────────────────────────────────────
        ram_rec = self._recommend_ram(source_vm)
        if ram_rec:
            recommendations.append(ram_rec)

        # ── Flavor consolidation ──────────────────────────────────
        flavor_rec = self._recommend_flavor(source_vm)
        if flavor_rec:
            recommendations.append(flavor_rec)

        # ── Disk type upgrade ──────────────────────────────────────
        disk_rec = self._recommend_disk_type(source_vm, performance_report)
        if disk_rec:
            recommendations.append(disk_rec)

        # ── Performance-based adjustments ─────────────────────────
        if performance_report:
            perf_recs = self._recommend_from_benchmarks(performance_report, source_vm)
            recommendations.extend(perf_recs)

        logger.info(
            f"Optimizer: {len(recommendations)} recommendation(s) for '{source_vm.name}'"
        )
        return recommendations

    # ════════════════════════════════════════════════════════════
    # INDIVIDUAL RECOMMENDATIONS
    # ════════════════════════════════════════════════════════════

    def _recommend_cpu(self, vm: VMwareVM) -> Optional[OptimizationRecommendation]:
        """Recommend vCPU count based on observed usage."""
        perf = vm.performance
        allocated = vm.total_vcpus

        if perf and perf.cpu_usage_percent > 0:
            # Calculate minimum needed vCPUs from % usage
            used_vcpus = (perf.cpu_usage_percent / 100) * allocated
            recommended = max(1, math.ceil(used_vcpus * _CPU_HEADROOM))
        else:
            # No metrics — keep current allocation
            return None

        if recommended < allocated:
            saving_pct = round((1 - recommended / allocated) * 100, 1)
            return OptimizationRecommendation(
                resource="vcpu",
                current_value=allocated,
                recommended_value=recommended,
                reason=(
                    f"Observed CPU usage: {perf.cpu_usage_percent:.1f}% of {allocated} vCPUs. "
                    f"Recommended: {recommended} vCPUs (with {int((_CPU_HEADROOM-1)*100)}% headroom)."
                ),
                estimated_saving_percent=saving_pct,
                priority="medium" if saving_pct < 40 else "high",
            )
        return None

    def _recommend_ram(self, vm: VMwareVM) -> Optional[OptimizationRecommendation]:
        """Recommend RAM allocation based on active memory usage."""
        perf = vm.performance
        allocated_mb = vm.memory_mb

        if perf and perf.memory_active_mb > 0:
            recommended_mb = math.ceil(perf.memory_active_mb * _RAM_HEADROOM / 256) * 256
        else:
            return None

        if recommended_mb < allocated_mb:
            saving_pct = round((1 - recommended_mb / allocated_mb) * 100, 1)
            return OptimizationRecommendation(
                resource="ram",
                current_value=f"{allocated_mb} MB",
                recommended_value=f"{recommended_mb} MB",
                reason=(
                    f"Active RAM: {perf.memory_active_mb:.0f} MB out of {allocated_mb} MB allocated. "
                    f"Recommended: {recommended_mb} MB (with {int((_RAM_HEADROOM-1)*100)}% headroom). "
                    f"Balloon driver pressure: {perf.memory_balloon_mb:.0f} MB."
                ),
                estimated_saving_percent=saving_pct,
                priority="medium" if saving_pct < 40 else "high",
            )
        return None

    def _recommend_flavor(self, vm: VMwareVM) -> Optional[OptimizationRecommendation]:
        """Suggest the most cost-efficient standard flavor."""
        perf = vm.performance

        if perf and perf.cpu_usage_percent > 0 and perf.memory_active_mb > 0:
            min_vcpus  = max(1, math.ceil((perf.cpu_usage_percent / 100) * vm.total_vcpus * _CPU_HEADROOM))
            min_ram_mb = math.ceil(perf.memory_active_mb * _RAM_HEADROOM / 256) * 256
        else:
            min_vcpus  = vm.total_vcpus
            min_ram_mb = vm.memory_mb

        min_disk_gb = math.ceil(vm.total_disk_gb * _DISK_HEADROOM)

        # Find smallest standard flavor that fits
        for vcpus, ram_mb, disk_gb in _STANDARD_FLAVORS:
            if vcpus >= min_vcpus and ram_mb >= min_ram_mb and disk_gb >= min_disk_gb:
                current_name  = f"{vm.total_vcpus}vCPU / {vm.memory_mb}MB RAM"
                recommended   = f"m1.{vcpus}c{ram_mb//1024}g ({vcpus} vCPU, {ram_mb} MB RAM, {disk_gb} GB)"
                if vcpus < vm.total_vcpus or ram_mb < vm.memory_mb:
                    saving_cpu = max(0, (1 - vcpus / vm.total_vcpus) * 100)
                    saving_ram = max(0, (1 - ram_mb / vm.memory_mb) * 100)
                    avg_saving = round((saving_cpu + saving_ram) / 2, 1)
                    return OptimizationRecommendation(
                        resource="flavor",
                        current_value=current_name,
                        recommended_value=recommended,
                        reason=(
                            f"Based on actual usage, a smaller standard flavor fits. "
                            f"Estimated savings: ~{avg_saving}% on compute costs."
                        ),
                        estimated_saving_percent=avg_saving,
                        priority="high" if avg_saving > 30 else "medium",
                    )
                break

        return None

    def _recommend_disk_type(
        self,
        vm: VMwareVM,
        report: Optional[PerformanceReport],
    ) -> Optional[OptimizationRecommendation]:
        """Recommend disk type upgrade if I/O is a bottleneck."""
        if not report:
            return None

        # Check if disk benchmarks showed regression
        disk_regressions = [
            c for c in report.comparisons
            if ("iops" in c.metric_name or "disk" in c.metric_name)
            and c.is_regression
        ]

        if disk_regressions:
            worst = min(disk_regressions, key=lambda c: c.delta_percent)
            return OptimizationRecommendation(
                resource="disk_type",
                current_value="standard HDD volume",
                recommended_value="SSD/NVMe volume",
                reason=(
                    f"Disk I/O regression detected: {worst.metric_name} degraded by "
                    f"{worst.delta_percent:+.1f}%. Upgrading to SSD volume type "
                    "should restore or exceed VMware performance."
                ),
                estimated_saving_percent=0.0,
                priority="high",
            )

        return None

    def _recommend_from_benchmarks(
        self,
        report: PerformanceReport,
        vm: VMwareVM,
    ) -> List[OptimizationRecommendation]:
        """Generate additional recommendations from benchmark comparisons."""
        recs = []

        # If overall score is good but there are minor regressions
        if report.overall_score >= 80 and report.regressions:
            recs.append(OptimizationRecommendation(
                resource="general",
                current_value=f"score={report.overall_score:.1f}/100",
                recommended_value="score=95+/100",
                reason=(
                    f"Migration score is acceptable ({report.overall_score:.1f}/100) but "
                    f"{len(report.regressions)} minor regression(s) detected. "
                    "Consider enabling CPU/memory overcommit tuning on the hypervisor."
                ),
                estimated_saving_percent=0.0,
                priority="low",
            ))

        # If score is poor
        elif report.overall_score < 60:
            recs.append(OptimizationRecommendation(
                resource="general",
                current_value=f"score={report.overall_score:.1f}/100",
                recommended_value="score=80+/100",
                reason=(
                    f"Significant performance degradation detected (score={report.overall_score:.1f}/100). "
                    "Consider: dedicated compute host, SR-IOV networking, and NVMe volumes. "
                    "Review hypervisor overcommit ratios."
                ),
                estimated_saving_percent=0.0,
                priority="high",
            ))

        return recs
