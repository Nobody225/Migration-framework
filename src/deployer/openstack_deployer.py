"""
src/deployer/openstack_deployer.py
────────────────────────────────────
Adapter-aware deployment module.

Orchestrates the full OpenStack provisioning sequence:
  1. Resolve / create Nova flavor
  2. Upload QCOW2 boot image → Glance
  3. Create Cinder boot volume from image
  4. Upload and create additional data volumes
  5. Pre-create Neutron ports (MAC/IP control)
  6. Boot Nova instance from boot volume

This module is adapter-agnostic: it calls BaseOpenStackAdapter methods only.
Environment-specific logic lives entirely in the concrete adapters.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List

from src.adapters.base.adapter import BaseOpenStackAdapter, OpenStackAdapterError
from src.core.models import (
    ConversionResult, OpenStackFlavorSpec,
    OpenStackInstance, OpenStackVolume,
)

logger = logging.getLogger("migration.deployer")


class OpenStackDeployer:
    """Deploys a converted VM onto OpenStack via the provided adapter."""

    def __init__(self, config: Dict):
        self._boot_timeout_s   = config.get("boot_timeout_s", 600)
        self._upload_timeout_s = config.get("upload_timeout_s", 3600)

    def deploy(
        self,
        conversion: ConversionResult,
        adapter: BaseOpenStackAdapter,
    ) -> OpenStackInstance:
        """
        Full deployment sequence for one converted VM.
        Returns the deployed OpenStackInstance.
        Raises OpenStackAdapterError on unrecoverable failure.
        """
        vm_name = conversion.source_vm.name
        logger.info(f"[Deployer] Starting deployment: '{vm_name}' → {adapter.name}")

        flavor_id   = self._step_flavor(conversion, adapter)
        boot_vol    = self._step_boot_volume(conversion, adapter, vm_name)
        extra_vols  = self._step_data_volumes(conversion, adapter, vm_name)
        mappings    = adapter.create_ports(conversion.network_mappings, vm_name)
        port_ids    = [m.openstack_port_id for m in mappings if m.openstack_port_id]

        # Sync enriched mappings back into ConversionResult
        conversion.network_mappings = mappings

        extra_vol_ids = [v.volume_id for v in extra_vols]

        instance = adapter.boot_instance(
            conversion=conversion,
            flavor_id=flavor_id,
            boot_volume_id=boot_vol.volume_id,
            port_ids=port_ids,
            extra_volume_ids=extra_vol_ids,
        )
        instance.volumes = [boot_vol] + extra_vols

        logger.info(
            f"[Deployer] Deployment done: '{vm_name}' → {instance.instance_id} "
            f"[{instance.status}] on {adapter.name}"
        )
        return instance

    # ── Private steps ─────────────────────────────────────────────

    def _step_flavor(
        self, conversion: ConversionResult, adapter: BaseOpenStackAdapter
    ) -> str:
        vm   = conversion.source_vm
        spec = conversion.flavor_spec
        if not spec:
            spec = OpenStackFlavorSpec(
                name=f"migration.{vm.name}.{vm.total_vcpus}c{vm.memory_gb:.0f}g",
                vcpus=vm.total_vcpus,
                ram_mb=vm.memory_mb,
                disk_gb=max(1, int(vm.total_disk_gb)),
                is_existing=False,
            )
            conversion.flavor_spec = spec
        flavor_id = adapter.resolve_flavor(spec)
        logger.info(f"[Deployer] Flavor: {spec.name} ({flavor_id})")
        return flavor_id

    def _step_boot_volume(
        self,
        conversion: ConversionResult,
        adapter: BaseOpenStackAdapter,
        vm_name: str,
    ) -> OpenStackVolume:
        if not conversion.converted_disks:
            raise OpenStackAdapterError(
                "No converted disks in ConversionResult",
                adapter.target, "deploy",
            )

        boot_disk = conversion.converted_disks[0]
        if not os.path.exists(boot_disk.qcow2_path):
            raise OpenStackAdapterError(
                f"QCOW2 file missing: {boot_disk.qcow2_path}",
                adapter.target, "deploy",
            )

        ts         = datetime.now().strftime("%Y%m%d%H%M%S")
        image_name = f"migration-{vm_name}-boot-{ts}"
        # Taille dynamique : virtual_size Glance > actual > original > fallback
        try:
            img_info = adapter._conn.image.get_image(image_id)
            vs = img_info.get("virtual_size") or img_info.get("size") or 0
            virtual_gb = max(1, int(vs / (1024**3))) if vs else 0
        except Exception:
            virtual_gb = 0
        actual_gb = int(boot_disk.actual_size_gb or 0)
        original_gb = int(boot_disk.original_size_gb or 0)
        # Ajouter 20% de marge pour tenir compte de la taille virtuelle
        size_gb = max(virtual_gb, actual_gb, original_gb, 10)
        if size_gb < 16 and virtual_gb == 0:
            size_gb = 20  # fallback securise

        logger.info(f"[Deployer] Uploading boot image '{image_name}'")
        image_id = adapter.upload_image(
            qcow2_path=boot_disk.qcow2_path,
            image_name=image_name,
            disk_format="qcow2",
            container_format="bare",
        )

        logger.info(f"[Deployer] Creating boot volume ({size_gb}GB) from {image_id}")
        return adapter.create_volume_from_image(
            image_id=image_id,
            size_gb=size_gb,
            volume_name=f"migration-{vm_name}-boot",
            bootable=True,
        )

    def _step_data_volumes(
        self,
        conversion: ConversionResult,
        adapter: BaseOpenStackAdapter,
        vm_name: str,
    ) -> List[OpenStackVolume]:
        volumes = []
        for idx, disk in enumerate(conversion.converted_disks[1:], start=1):
            if not os.path.exists(disk.qcow2_path):
                logger.warning(f"[Deployer] Data disk #{idx} missing — skipping")
                continue

            ts         = datetime.now().strftime("%Y%m%d%H%M%S")
            image_name = f"migration-{vm_name}-data{idx}-{ts}"
            size_gb    = max(1, int(disk.original_size_gb))

            logger.info(f"[Deployer] Uploading data disk #{idx}")
            image_id = adapter.upload_image(
                qcow2_path=disk.qcow2_path,
                image_name=image_name,
                disk_format="qcow2",
                container_format="bare",
            )
            vol = adapter.create_volume_from_image(
                image_id=image_id,
                size_gb=size_gb,
                volume_name=f"migration-{vm_name}-data{idx}",
                bootable=False,
            )
            volumes.append(vol)
        return volumes
