"""
src/utils/framework_factory.py
────────────────────────────────
Factory function that wires all modules together into a
ready-to-use MigrationOrchestrator.

Centralizes dependency injection — the CLI, API, and tests
all use this single function to get a fully configured orchestrator.
"""

from __future__ import annotations

from typing import Any, Dict

from src.core.orchestrator import MigrationOrchestrator
from src.extractor.vmware_extractor import VMwareExtractor
from src.converter.converter import VMConverter
from src.deployer.openstack_deployer import OpenStackDeployer
from src.evaluator.evaluator import PerformanceEvaluator
from src.optimizer.optimizer import ResourceOptimizer


def build_framework(config: Dict[str, Any]) -> MigrationOrchestrator:
    """
    Instantiate and wire all framework modules.

    Args:
        config: Loaded config dict (from config_loader.load_config)

    Returns:
        Fully configured MigrationOrchestrator ready to use.
    """
    migration_cfg = config.get("migration", {})

    extractor  = VMwareExtractor(config.get("vmware", {}))
    converter  = VMConverter(config.get("conversion", {}))
    deployer   = OpenStackDeployer(migration_cfg)
    evaluator  = PerformanceEvaluator(config.get("evaluation", {}))
    optimizer  = ResourceOptimizer(config.get("migration", {}))

    # Build adapter configs keyed by target name
    adapter_configs = {
        target: cfg
        for target, cfg in config.get("openstack", {}).items()
    }

    orchestrator = MigrationOrchestrator(
        extractor       = extractor,
        converter       = converter,
        deployer        = deployer,
        evaluator       = evaluator,
        optimizer       = optimizer,
        adapter_configs = adapter_configs,
        workspace_dir   = migration_cfg.get("workspace_dir", "/tmp/migration_workspace"),
        dry_run         = migration_cfg.get("dry_run", False),
        max_workers     = migration_cfg.get("max_parallel_jobs", 3),
    )

    return orchestrator
