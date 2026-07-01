"""
src/core/orchestrator.py
────────────────────────
Enterprise migration orchestrator — Orange Group context.

Responsibilities:
  1. Drive the 5-phase pipeline for each MigrationJob
  2. Select and instantiate the correct OpenStack adapter per job
  3. Handle errors, retries, and rollback at every phase boundary
  4. Maintain a thread-safe job registry
  5. Emit real-time status events (WebSocket hook for dashboard)
  6. Support sequential and parallel batch migration

Pipeline:
  PHASE 1  Extractor  → VMware discovery + performance metrics
  PHASE 2  Converter  → Disk conversion + compatibility analysis + network/flavor mapping
  PHASE 3  Deployer   → Glance upload + Cinder volumes + Neutron ports + Nova boot
  PHASE 4  Evaluator  → Before/after benchmarking → PerformanceReport
  PHASE 5  Optimizer  → Right-sizing recommendations

Design principles:
  - Each phase is isolated: a failure in phase N never silently corrupts phase N+1
  - Evaluation and optimization are non-blocking: failure produces a warning, not a rollback
  - The adapter is instantiated fresh per job (no shared auth tokens between jobs)
  - All log output goes to both Python's logging system and the job's AuditEntry list
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.core.models import (
    LogLevel, MigrationJob, MigrationMode,
    MigrationStatus, OpenStackTarget,
)
from src.adapters.base.adapter import BaseOpenStackAdapter, OpenStackAdapterError

logger = logging.getLogger("migration.orchestrator")


class PipelineError(Exception):
    """Signals a hard failure in a pipeline phase — triggers rollback."""
    def __init__(self, phase: str, message: str):
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


class MigrationOrchestrator:
    """
    Core engine for the VMware → multi-OpenStack migration framework.

    Thread-safe: multiple jobs run concurrently via ThreadPoolExecutor.
    Each job owns its own adapter instance — no shared OpenStack auth state.
    """

    def __init__(
        self,
        extractor,
        converter,
        deployer,
        evaluator,
        optimizer,
        adapter_configs: Dict[str, Any],
        workspace_dir: str = "/opt/migration/workspace",
        dry_run: bool = False,
        max_workers: int = 3,
    ):
        self.extractor       = extractor
        self.converter       = converter
        self.deployer        = deployer
        self.evaluator       = evaluator
        self.optimizer       = optimizer
        self.adapter_configs = adapter_configs   # keyed by OpenStackTarget.value
        self.workspace_dir   = workspace_dir
        self.dry_run         = dry_run
        self.max_workers     = max_workers

        self._jobs: Dict[str, MigrationJob] = {}
        self._lock = threading.Lock()
        self._status_hook: Optional[Callable[[MigrationJob], None]] = None

        logger.info(
            f"Orchestrator ready | dry_run={dry_run} | workspace={workspace_dir} | "
            f"max_workers={max_workers}"
        )

    # ════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════

    def set_status_hook(self, hook: Callable[[MigrationJob], None]) -> None:
        """Register a callback fired on every job status transition (WebSocket push)."""
        self._status_hook = hook

    def migrate(
        self,
        vm_name: str,
        target: OpenStackTarget,
        mode: MigrationMode = MigrationMode.COLD,
        operator: str = "system",
        skip_evaluation: bool = False,
    ) -> MigrationJob:
        """
        Run the full migration pipeline for one VM.

        Returns a completed (or failed/rolled-back) MigrationJob with
        a full AuditEntry trail.
        """
        job = self._create_job(vm_name, target, mode, operator)

        try:
            adapter = self._get_adapter(target)

            # ── Phase 1 ── Extract ────────────────────────────────
            self._transition(job, MigrationStatus.EXTRACTING)
            self._phase_extract(job)

            # ── Phase 2 ── Convert ────────────────────────────────
            self._transition(job, MigrationStatus.CONVERTING)
            self._phase_convert(job, adapter)

            if self.dry_run:
                job.log("DRY-RUN: halting before deployment", module="orchestrator")
                self._transition(job, MigrationStatus.COMPLETED)
                return job

            # ── Phase 3 ── Deploy ─────────────────────────────────
            self._transition(job, MigrationStatus.DEPLOYING)
            self._phase_deploy(job, adapter)

            # ── Phase 4 ── Evaluate (non-blocking) ───────────────
            if not skip_evaluation:
                self._transition(job, MigrationStatus.EVALUATING)
                self._phase_evaluate(job)

            # ── Phase 5 ── Optimize (non-blocking) ────────────────
            self._transition(job, MigrationStatus.OPTIMIZING)
            self._phase_optimize(job)

            # ── Done ──────────────────────────────────────────────
            job.completed_at = datetime.now()
            self._transition(job, MigrationStatus.COMPLETED)
            job.log(
                "Migration completed",
                f"duration={job.duration_seconds:.1f}s | "
                f"instance={job.instance.instance_id if job.instance else 'n/a'}",
                module="orchestrator",
            )

        except PipelineError as pe:
            job.log_error(f"Hard failure in phase {pe.phase}", str(pe), module="orchestrator")
            logger.error(f"[{job.job_id}] PipelineError: {pe}")
            self._attempt_rollback(job)

        except Exception as exc:
            job.log_error("Unhandled orchestrator exception", str(exc), module="orchestrator")
            logger.exception(f"[{job.job_id}] Unexpected error")
            self._attempt_rollback(job)

        return job

    def migrate_batch(
        self,
        requests: List[Dict[str, Any]],
        parallel: bool = False,
    ) -> List[MigrationJob]:
        """
        Migrate multiple VMs.

        Each item in `requests` is a dict accepted by migrate():
            {"vm_name": ..., "target": ..., "mode": ..., "operator": ...}

        parallel=True uses ThreadPoolExecutor up to max_workers.
        """
        if not parallel:
            return [self.migrate(**r) for r in requests]

        results: List[MigrationJob] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.migrate, **r): r["vm_name"] for r in requests}
            for future in as_completed(futures):
                vm_name = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error(f"[BATCH] '{vm_name}' raised: {exc}")
        return results

    def get_job(self, job_id: str) -> Optional[MigrationJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(
        self,
        status: Optional[MigrationStatus] = None,
        target: Optional[OpenStackTarget] = None,
    ) -> List[MigrationJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if target:
            jobs = [j for j in jobs if j.target == target]
        return jobs

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a PENDING job before it starts."""
        job = self.get_job(job_id)
        if job and job.status == MigrationStatus.PENDING:
            self._transition(job, MigrationStatus.CANCELLED)
            return True
        return False

    # ════════════════════════════════════════════════════════════
    # PIPELINE PHASES — private
    # ════════════════════════════════════════════════════════════

    def _phase_extract(self, job: MigrationJob) -> None:
        try:
            self.extractor.connect()
            vm = self.extractor.get_vm(job.vm_name)

            if vm is None:
                raise PipelineError(
                    "EXTRACT", f"VM '{job.vm_name}' not found in vSphere"
                )

            vm = self.extractor.collect_metrics(vm)
            job.source_vm = vm
            job.log(
                "Extraction complete",
                f"vCPUs={vm.total_vcpus} | RAM={vm.memory_gb}GB | "
                f"disks={len(vm.disks)} | NICs={len(vm.nics)} | "
                f"OS={vm.guest_full_name} | snapshots={vm.has_snapshots}",
                module="extractor",
            )
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError("EXTRACT", str(exc)) from exc
        finally:
            self.extractor.disconnect()

    def _phase_convert(self, job: MigrationJob, adapter: BaseOpenStackAdapter) -> None:
        try:
            vm = job.source_vm

            issues = self.converter.analyze_compatibility(vm)
            blockers = [x for x in issues if x.startswith("[BLOCKER]")]
            warnings = [x for x in issues if not x.startswith("[BLOCKER]")]

            for w in warnings:
                job.log("Compatibility warning", w, LogLevel.WARNING, "converter")

            if blockers:
                raise PipelineError(
                    "CONVERT",
                    f"{len(blockers)} blocker(s): {' | '.join(blockers)}"
                )

            result = self.converter.run(vm, self.workspace_dir)

            # Network resolution is adapter-specific — done here so converter stays generic
            result.network_mappings = adapter.resolve_network_mappings(
                result.network_mappings
            )

            unresolved = [m for m in result.network_mappings if not m.openstack_network_id]
            if unresolved:
                names = [m.vmware_network_name for m in unresolved]
                job.log(
                    "Unresolved network mappings",
                    f"{len(unresolved)} network(s) could not be mapped: {names}",
                    LogLevel.WARNING, "converter",
                )

            job.conversion = result
            job.log(
                "Conversion complete",
                f"disks={len(result.converted_disks)} | "
                f"flavor={result.flavor_spec.name if result.flavor_spec else 'n/a'} | "
                f"networks_resolved="
                f"{sum(1 for m in result.network_mappings if m.openstack_network_id)}"
                f"/{len(result.network_mappings)}",
                module="converter",
            )

        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError("CONVERT", str(exc)) from exc

    def _phase_deploy(self, job: MigrationJob, adapter: BaseOpenStackAdapter) -> None:
        try:
            adapter.connect()
            instance = self.deployer.deploy(job.conversion, adapter)
            job.instance = instance

            ok, issues = adapter.verify_instance(instance)
            for issue in issues:
                job.log("Verification issue", issue, LogLevel.WARNING, "deployer")

            job.log(
                "Deployment complete",
                f"instance={instance.instance_id} | status={instance.status} | "
                f"IPs={instance.ip_addresses} | target={job.target.value}",
                module="deployer",
            )
        except OpenStackAdapterError as exc:
            raise PipelineError("DEPLOY", str(exc)) from exc
        except Exception as exc:
            raise PipelineError("DEPLOY", str(exc)) from exc
        finally:
            adapter.disconnect()

    def _phase_evaluate(self, job: MigrationJob) -> None:
        """Non-blocking: evaluation failure produces a warning, not a rollback."""
        try:
            if not (job.source_vm and job.instance):
                job.log("Evaluation skipped", "Missing source VM or instance", LogLevel.WARNING, "evaluator")
                return

            report = self.evaluator.compare(job.source_vm, job.instance)
            job.performance_report = report
            status = "PASS" if report.passed else "FAIL"
            job.log(
                "Evaluation complete",
                f"score={report.overall_score:.1f}/100 | {status} | "
                f"regressions={len(report.regressions)}",
                module="evaluator",
            )
            for rec in report.recommendations:
                job.log("Performance recommendation", rec, module="evaluator")

        except Exception as exc:
            job.log("Evaluation failed (non-blocking)", str(exc), LogLevel.WARNING, "evaluator")

    def _phase_optimize(self, job: MigrationJob) -> None:
        """Non-blocking: produces recommendations, never rolls back."""
        try:
            if not job.instance:
                return
            recs = self.optimizer.recommend(
                instance=job.instance,
                performance_report=job.performance_report,
                source_vm=job.source_vm,
            )
            job.optimization_recommendations = recs
            if recs:
                summary = " | ".join(
                    f"{r.resource}: {r.current_value}→{r.recommended_value}" for r in recs
                )
                job.log("Optimization recommendations", summary, module="optimizer")
        except Exception as exc:
            job.log("Optimization failed (non-blocking)", str(exc), LogLevel.WARNING, "optimizer")

    # ════════════════════════════════════════════════════════════
    # ROLLBACK
    # ════════════════════════════════════════════════════════════

    def _attempt_rollback(self, job: MigrationJob) -> None:
        if not job.instance:
            self._transition(job, MigrationStatus.FAILED)
            return

        try:
            adapter = self._get_adapter(job.target)
            adapter.connect()
            actions = adapter.rollback(job.instance)
            adapter.disconnect()
            for action in actions:
                job.log("Rollback action", action, LogLevel.WARNING, "orchestrator")
            self._transition(job, MigrationStatus.ROLLED_BACK)
            job.log("Rollback completed", module="orchestrator")
        except Exception as exc:
            job.log(
                "Rollback failed — manual cleanup required",
                str(exc), LogLevel.CRITICAL, "orchestrator",
            )
            self._transition(job, MigrationStatus.FAILED)

    # ════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ════════════════════════════════════════════════════════════

    def _get_adapter(self, target: OpenStackTarget) -> BaseOpenStackAdapter:
        """Instantiate a fresh adapter for the given target."""
        from src.adapters.huawei.adapter import AdapterFactory
        config = self.adapter_configs.get(target.value)
        if not config:
            raise ValueError(
                f"No OpenStack config for target '{target.value}'. "
                f"Check config.yaml [openstack] section."
            )
        return AdapterFactory.create(target, config)

    def _create_job(
        self,
        vm_name: str,
        target: OpenStackTarget,
        mode: MigrationMode,
        operator: str,
    ) -> MigrationJob:
        job = MigrationJob(
            vm_name=vm_name,
            target=target,
            mode=mode,
            operator=operator,
        )
        job.log(
            "Job created",
            f"VM={vm_name} | target={target.value} | mode={mode.value} | operator={operator}",
            module="orchestrator",
        )
        with self._lock:
            self._jobs[job.job_id] = job
        logger.info(f"Job created: {job.job_id} | VM={vm_name} | target={target.value}")
        return job

    def _transition(self, job: MigrationJob, new_status: MigrationStatus) -> None:
        prev = job.status
        job.status = new_status

        if new_status == MigrationStatus.DEPLOYING and job.started_at is None:
            job.started_at = datetime.now()

        job.log(
            "Status transition",
            f"{prev.value} → {new_status.value}",
            module="orchestrator",
        )
        logger.info(f"[{job.job_id}] {prev.value} → {new_status.value}")

        if self._status_hook:
            try:
                self._status_hook(job)
            except Exception as exc:
                logger.warning(f"Status hook error: {exc}")
