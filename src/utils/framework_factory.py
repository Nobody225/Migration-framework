"""
src/utils/framework_factory.py
────────────────────────────────
Factory function that wires all modules together into a
ready-to-use MigrationOrchestrator.

Centralizes dependency injection — the CLI, API, and tests
all use this single function to get a fully configured orchestrator.

v2 — Intègre le ConnectionStore (connexions dynamiques depuis le dashboard)
     en priorité sur config.yaml.
"""

from __future__ import annotations

import os
import platform
from typing import Any, Dict

from src.core.orchestrator import MigrationOrchestrator
from src.extractor.vmware_extractor import VMwareExtractor
from src.converter.converter import VMConverter
from src.deployer.openstack_deployer import OpenStackDeployer
from src.evaluator.evaluator import PerformanceEvaluator
from src.optimizer.optimizer import ResourceOptimizer


def _default_workspace() -> str:
    """Retourne un workspace_dir adapté à l'OS courant."""
    if platform.system() == "Windows":
        return os.path.join(os.environ.get("TEMP", "C:\\Temp"), "migration_workspace")
    return "/tmp/migration_workspace"


def resolve_vcenter_config(vcenter_id: str, config: Dict) -> Dict:
    """
    Résout la config d'un vCenter par ordre de priorité :
    1. ConnectionStore (connexions ajoutées via le dashboard)
    2. Liste 'vcenters' dans config.yaml
    3. Section 'vmware' dans config.yaml (fallback)
    """
    # 1. ConnectionStore
    try:
        from src.api.connection_store import get_connection_store
        store = get_connection_store()
        conn = store.get_vcenter(vcenter_id)
        if conn:
            return conn.to_vmware_config()
    except Exception:
        pass

    # 2. Liste vcenters dans config.yaml
    for vc in config.get("vcenters", []):
        if vc.get("id") == vcenter_id:
            return vc

    # 3. Fallback section vmware
    return config.get("vmware", {})


def resolve_openstack_config(target_id: str, config: Dict) -> Dict:
    """
    Résout la config d'un OpenStack par ordre de priorité :
    1. ConnectionStore (connexions ajoutées via le dashboard)
    2. Section 'openstack.<target_id>' dans config.yaml
    """
    # 1. ConnectionStore
    try:
        from src.api.connection_store import get_connection_store
        store = get_connection_store()
        conn = store.get_openstack(target_id)
        if conn:
            return conn.to_openstack_config()
    except Exception:
        pass

    # 2. config.yaml
    return config.get("openstack", {}).get(target_id, {})


def build_adapter_configs(config: Dict) -> Dict:
    """
    Construit le dictionnaire adapter_configs pour l'orchestrateur.
    Fusionne les connexions statiques (config.yaml) et dynamiques (ConnectionStore).
    """
    adapter_configs = {}

    # 1. Connexions statiques depuis config.yaml
    for target, cfg in config.get("openstack", {}).items():
        adapter_configs[target] = cfg

    # 2. Connexions dynamiques depuis ConnectionStore (priorité)
    try:
        from src.api.connection_store import get_connection_store
        store = get_connection_store()
        for conn in store.list_openstacks():
            # Clé = conn_id (UUID) ET os_type (pour compatibilité enum)
            cfg = conn.to_openstack_config()
            adapter_configs[conn.conn_id] = cfg
            # Aussi accessible par type pour les migrations legacy
            adapter_configs[conn.os_type] = cfg
    except Exception:
        pass

    return adapter_configs


def build_framework(config: Dict[str, Any]) -> MigrationOrchestrator:
    """
    Instancie et connecte tous les modules du framework.

    Args:
        config: Config dict chargé depuis config.yaml

    Returns:
        MigrationOrchestrator prêt à l'emploi.
    """
    migration_cfg = config.get("migration", {})

    # workspace_dir cross-platform
    workspace = migration_cfg.get("workspace_dir") or _default_workspace()
    os.makedirs(workspace, exist_ok=True)

    # Extractor — config par défaut (remplacée par vcenter_id au moment du job)
    extractor  = VMwareExtractor(config.get("vmware", {}))
    converter  = VMConverter(config.get("conversion", {}))
    deployer   = OpenStackDeployer(migration_cfg)
    evaluator  = PerformanceEvaluator(config.get("evaluation", {}))
    optimizer  = ResourceOptimizer(migration_cfg)

    # Adapter configs = statique + dynamique
    adapter_configs = build_adapter_configs(config)

    orchestrator = MigrationOrchestrator(
        extractor       = extractor,
        converter       = converter,
        deployer        = deployer,
        evaluator       = evaluator,
        optimizer       = optimizer,
        adapter_configs = adapter_configs,
        framework_config = config,
        workspace_dir   = workspace,
        dry_run         = migration_cfg.get("dry_run", True),
        max_workers     = migration_cfg.get("max_parallel_jobs", 3),
    )

    return orchestrator
