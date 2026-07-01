"""
src/adapters/huawei/adapter.py
──────────────────────────────
Huawei FusionSphere / ManageOne OpenStack adapter.

Huawei-specific behaviour:
  - ManageOne enterprise_project_id propagated to all resources
  - EVS volume types: SSD (ultra-high I/O), SAS (high I/O), SATA (standard)
  - IMS image properties: __support_kvm=true required
  - VPC network names may be prefixed with enterprise project
  - IAM agency-based cross-project auth
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import openstack

from src.adapters.base.adapter import BaseOpenStackAdapter, OpenStackAdapterError
from src.core.models import (
    ConversionResult, NetworkMapping, OpenStackFlavorSpec,
    OpenStackInstance, OpenStackTarget, OpenStackVolume,
)

logger = logging.getLogger("migration.adapter.huawei")

# EVS volume type mapping (alias → Huawei type name)
_EVS_TYPES = {"ssd": "SSD", "sas": "SAS", "sata": "SATA"}


class HuaweiAdapter(BaseOpenStackAdapter):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._target   = OpenStackTarget.HUAWEI
        self._hw       = config.get("huawei", {})
        self._ep_id    = self._hw.get("enterprise_project_id", "0")

    # ── Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._conn = openstack.connect(
                auth_url=self.config["auth_url"],
                project_name=self.config["project_name"],
                username=self.config["username"],
                password=self.config["password"],
                user_domain_name=self.config.get("user_domain_name", "op_svc_domain"),
                project_domain_name=self.config.get("project_domain_name", "op_svc_domain"),
                verify=self.config.get("ssl_verify", True),
                region_name=self.config.get("region_name", "cn-east-3"),
            )
            self._conn.authorize()
            logger.info(f"Connected to Huawei FusionSphere (enterprise_project={self._ep_id})")
            return True
        except Exception as exc:
            raise OpenStackAdapterError(str(exc), self._target, "connect") from exc

    # ── Glance (IMS) ─────────────────────────────────────────────

    def upload_image(
        self,
        qcow2_path: str,
        image_name: str,
        disk_format: str = "qcow2",
        container_format: str = "bare",
        extra_properties: Optional[Dict[str, str]] = None,
    ) -> str:
        self._require_connection("upload_image")

        props: Dict[str, str] = {
            "__support_kvm":         "true",
            "__os_type":             "Linux",
            "hw_vif_model":          "virtio",
            "enterprise_project_id": self._ep_id,
        }
        props.update(self._hw.get("image_properties", {}))
        if extra_properties:
            props.update(extra_properties)

        logger.info(f"[Huawei] Uploading image '{image_name}' ← {qcow2_path}")
        image = self._conn.image.create_image(
            name=image_name,
            disk_format=disk_format,
            container_format=container_format,
            visibility="private",
            **props,
        )
        with open(qcow2_path, "rb") as fh:
            self._conn.image.upload_image(image.id, data=fh)
        self._conn.image.wait_for_image(image.id, status="active")
        logger.info(f"[Huawei] Image active: {image.id}")
        return image.id

    # ── Flavor ───────────────────────────────────────────────────

    def create_flavor(self, spec: OpenStackFlavorSpec) -> str:
        self._require_connection("create_flavor")

        extra: Dict[str, str] = {
            "cond:operation:status": "normal",
        }
        extra.update(spec.extra_specs)

        flavor = self._conn.compute.create_flavor(
            name=spec.name, vcpus=spec.vcpus, ram=spec.ram_mb,
            disk=spec.disk_gb, is_public=False,
        )
        self._conn.compute.create_flavor_extra_specs(flavor.id, extra)
        logger.info(f"[Huawei] Flavor created: {flavor.id} ({spec.name})")
        return flavor.id

    # ── EVS volumes ──────────────────────────────────────────────

    def create_volume_from_image(
        self,
        image_id: str,
        size_gb: int,
        volume_name: str,
        volume_type: Optional[str] = None,
        bootable: bool = False,
    ) -> OpenStackVolume:
        self._require_connection("create_volume_from_image")

        alias  = self._hw.get("volume_type", "sas").lower()
        vtype  = volume_type or _EVS_TYPES.get(alias, "SAS")
        meta   = {"enterprise_project_id": self._ep_id}

        logger.info(f"[Huawei] Creating volume '{volume_name}' ({size_gb}GB, type={vtype})")
        vol = self._conn.block_storage.create_volume(
            name=volume_name, size=size_gb,
            volume_type=vtype, image_id=image_id, metadata=meta,
        )
        self._conn.block_storage.wait_for_status(
            vol, status="available", failures=["error"], interval=10, wait=600
        )
        logger.info(f"[Huawei] Volume ready: {vol.id}")
        return OpenStackVolume(
            volume_id=vol.id, name=volume_name,
            size_gb=size_gb, volume_type=vtype,
            status="available", bootable=bootable,
        )

    # ── VPC networking ───────────────────────────────────────────

    def resolve_network_mappings(
        self, mappings: List[NetworkMapping],
    ) -> List[NetworkMapping]:
        self._require_connection("resolve_network_mappings")

        for m in mappings:
            # ManageOne may prefix network names with enterprise project ID
            for candidate in [m.vmware_network_name, f"{self._ep_id}_{m.vmware_network_name}"]:
                net = self._conn.network.find_network(candidate)
                if net:
                    break

            if net:
                subnet = next(iter(self._conn.network.subnets(network_id=net.id)), None)
                m.openstack_network_id   = net.id
                m.openstack_network_name = net.name
                m.openstack_subnet_id    = subnet.id if subnet else ""
                logger.info(f"[Huawei] Network resolved: '{m.vmware_network_name}' → {net.id}")
            else:
                logger.warning(f"[Huawei] VPC network not found for '{m.vmware_network_name}'")

        return mappings

    def create_ports(
        self,
        mappings: List[NetworkMapping],
        instance_name: str,
    ) -> List[NetworkMapping]:
        self._require_connection("create_ports")

        for i, m in enumerate(mappings):
            if not m.openstack_network_id:
                continue
            body: Dict[str, Any] = {
                "name":       f"{instance_name}-nic-{i}",
                "network_id": m.openstack_network_id,
            }
            if m.openstack_subnet_id:
                fip: Dict[str, Any] = {"subnet_id": m.openstack_subnet_id}
                if m.ip_address:
                    fip["ip_address"] = m.ip_address
                body["fixed_ips"] = [fip]
            port = self._conn.network.create_port(**body)
            m.openstack_port_id = port.id
            logger.info(f"[Huawei] Port created: {port.id}")

        return mappings

    # ── ECS boot ─────────────────────────────────────────────────

    def boot_instance(
        self,
        conversion: ConversionResult,
        flavor_id: str,
        boot_volume_id: str,
        port_ids: List[str],
        extra_volume_ids: List[str],
    ) -> OpenStackInstance:
        self._require_connection("boot_instance")

        vm   = conversion.source_vm
        az   = self._hw.get("availability_zone", "az1.dc1")
        bdm  = self._build_block_device_mapping(boot_volume_id, extra_volume_ids)

        server = self._conn.compute.create_server(
            name=vm.name,
            flavor_id=flavor_id,
            networks=[{"port": pid} for pid in port_ids if pid],
            block_device_mapping_v2=bdm,
            availability_zone=az,
            user_data=conversion.user_data or None,
            metadata={
                "migrated_from":         "vmware",
                "source_uuid":           vm.instance_uuid,
                "enterprise_project_id": self._ep_id,
            },
        )
        self.wait_for_active(server.id)
        server   = self._conn.compute.get_server(server.id)
        addresses = {n: [a["addr"] for a in ads] for n, ads in (server.addresses or {}).items()}

        return OpenStackInstance(
            instance_id=server.id, name=vm.name,
            target=self._target,
            flavor_name=conversion.flavor_spec.name if conversion.flavor_spec else "",
            status=server.status, ip_addresses=addresses,
            availability_zone=az,
            extended_properties={"enterprise_project_id": self._ep_id},
        )

    # ── Verification ─────────────────────────────────────────────

    def verify_instance(self, instance: OpenStackInstance) -> Tuple[bool, List[str]]:
        passed, issues = super().verify_instance(instance)
        try:
            server  = self._conn.compute.get_server(instance.instance_id)
            meta_ep = (server.metadata or {}).get("enterprise_project_id")
            if meta_ep != self._ep_id:
                issues.append(
                    f"enterprise_project_id mismatch: expected {self._ep_id}, got {meta_ep}"
                )
        except Exception as exc:
            issues.append(f"Enterprise project check failed: {exc}")
        return len(issues) == 0, issues


# ════════════════════════════════════════════════════════════════
# CUSTOM (COMMUNITY) ADAPTER
# ════════════════════════════════════════════════════════════════

logger_custom = logging.getLogger("migration.adapter.custom")


class CustomAdapter(BaseOpenStackAdapter):
    """
    Vanilla community OpenStack adapter.
    Pure openstacksdk — no vendor extensions.
    Reference implementation and fallback for unknown environments.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._target  = OpenStackTarget.CUSTOM
        self._custom  = config.get("custom", {})

    def connect(self) -> bool:
        try:
            self._conn = openstack.connect(
                auth_url=self.config["auth_url"],
                project_name=self.config["project_name"],
                username=self.config["username"],
                password=self.config["password"],
                user_domain_name=self.config.get("user_domain_name", "Default"),
                project_domain_name=self.config.get("project_domain_name", "Default"),
                verify=self.config.get("ssl_verify", False),
                region_name=self.config.get("region_name", "RegionOne"),
            )
            self._conn.authorize()
            logger_custom.info("Connected to custom community OpenStack")
            return True
        except Exception as exc:
            raise OpenStackAdapterError(str(exc), self._target, "connect") from exc

    def upload_image(
        self,
        qcow2_path: str,
        image_name: str,
        disk_format: str = "qcow2",
        container_format: str = "bare",
        extra_properties: Optional[Dict[str, str]] = None,
    ) -> str:
        self._require_connection("upload_image")
        props: Dict[str, str] = {"hw_vif_model": "virtio"}
        if extra_properties:
            props.update(extra_properties)
        logger_custom.info(f"[Custom] Uploading image '{image_name}'")
        image = self._conn.image.create_image(
            name=image_name, disk_format=disk_format,
            container_format=container_format, visibility="private", **props,
        )
        with open(qcow2_path, "rb") as fh:
            self._conn.image.upload_image(image.id, data=fh)
        self._conn.image.wait_for_image(image.id, status="active")
        return image.id

    def create_flavor(self, spec: OpenStackFlavorSpec) -> str:
        self._require_connection("create_flavor")
        flavor = self._conn.compute.create_flavor(
            name=spec.name, vcpus=spec.vcpus, ram=spec.ram_mb,
            disk=spec.disk_gb, ephemeral=spec.ephemeral_gb,
        )
        if spec.extra_specs:
            self._conn.compute.create_flavor_extra_specs(flavor.id, spec.extra_specs)
        return flavor.id

    def create_volume_from_image(
        self,
        image_id: str,
        size_gb: int,
        volume_name: str,
        volume_type: Optional[str] = None,
        bootable: bool = False,
    ) -> OpenStackVolume:
        self._require_connection("create_volume_from_image")
        vtype = volume_type or self._custom.get("volume_type") or None
        kwargs: Dict[str, Any] = {
            "name": volume_name, "size": size_gb, "image_id": image_id,
        }
        if vtype:
            kwargs["volume_type"] = vtype
        vol = self._conn.block_storage.create_volume(**kwargs)
        self._conn.block_storage.wait_for_status(
            vol, status="available", failures=["error"], interval=10, wait=600
        )
        return OpenStackVolume(
            volume_id=vol.id, name=volume_name,
            size_gb=size_gb, volume_type=vtype or "",
            status="available", bootable=bootable,
        )

    def resolve_network_mappings(
        self, mappings: List[NetworkMapping],
    ) -> List[NetworkMapping]:
        self._require_connection("resolve_network_mappings")
        for m in mappings:
            net = self._conn.network.find_network(m.vmware_network_name)
            if net:
                subnet = next(iter(self._conn.network.subnets(network_id=net.id)), None)
                m.openstack_network_id   = net.id
                m.openstack_network_name = net.name
                m.openstack_subnet_id    = subnet.id if subnet else ""
            else:
                logger_custom.warning(f"[Custom] Network not found: '{m.vmware_network_name}'")
        return mappings

    def create_ports(
        self,
        mappings: List[NetworkMapping],
        instance_name: str,
    ) -> List[NetworkMapping]:
        self._require_connection("create_ports")
        for i, m in enumerate(mappings):
            if not m.openstack_network_id:
                continue
            body: Dict[str, Any] = {
                "name":       f"{instance_name}-port-{i}",
                "network_id": m.openstack_network_id,
            }
            if m.openstack_subnet_id:
                body["fixed_ips"] = [{"subnet_id": m.openstack_subnet_id}]
            if m.ip_address:
                body["fixed_ips"] = [{"ip_address": m.ip_address}]
            port = self._conn.network.create_port(**body)
            m.openstack_port_id = port.id
        return mappings

    def boot_instance(
        self,
        conversion: ConversionResult,
        flavor_id: str,
        boot_volume_id: str,
        port_ids: List[str],
        extra_volume_ids: List[str],
    ) -> OpenStackInstance:
        self._require_connection("boot_instance")
        vm   = conversion.source_vm
        az   = self._custom.get("availability_zone", "nova")
        bdm  = self._build_block_device_mapping(boot_volume_id, extra_volume_ids)
        server = self._conn.compute.create_server(
            name=vm.name, flavor_id=flavor_id,
            networks=[{"port": pid} for pid in port_ids if pid],
            block_device_mapping_v2=bdm, availability_zone=az,
            metadata={"migrated_from": "vmware", "source_uuid": vm.instance_uuid},
            user_data=conversion.user_data or None,
        )
        self.wait_for_active(server.id)
        server    = self._conn.compute.get_server(server.id)
        addresses = {n: [a["addr"] for a in ads] for n, ads in (server.addresses or {}).items()}
        return OpenStackInstance(
            instance_id=server.id, name=vm.name, target=self._target,
            flavor_name=conversion.flavor_spec.name if conversion.flavor_spec else "",
            status=server.status, ip_addresses=addresses, availability_zone=az,
        )


# ════════════════════════════════════════════════════════════════
# ADAPTER FACTORY — single entry point
# ════════════════════════════════════════════════════════════════

class AdapterFactory:
    """
    Instantiates the correct OpenStack adapter for a given target.

    Supports runtime registration for new environments (e.g. future
    VMware Cloud Foundation target):
        AdapterFactory.register(OpenStackTarget.NEW, NewAdapter)

    Usage:
        adapter = AdapterFactory.create(OpenStackTarget.REDHAT, config)
        adapter.connect()
    """

    _registry: Dict[OpenStackTarget, type] = {}

    @classmethod
    def register(cls, target: OpenStackTarget, adapter_cls: type) -> None:
        cls._registry[target] = adapter_cls

    @classmethod
    def create(
        cls,
        target: OpenStackTarget,
        config: Dict[str, Any],
    ) -> BaseOpenStackAdapter:
        adapter_cls = cls._registry.get(target)
        if not adapter_cls:
            registered = [t.value for t in cls._registry]
            raise ValueError(
                f"No adapter registered for '{target.value}'. "
                f"Registered targets: {registered}"
            )
        return adapter_cls(config)

    @classmethod
    def available_targets(cls) -> List[OpenStackTarget]:
        return list(cls._registry.keys())


# ── Register built-in adapters at import time ────────────────────
from src.adapters.redhat.adapter import RedHatAdapter  # noqa: E402

AdapterFactory.register(OpenStackTarget.REDHAT,  RedHatAdapter)
AdapterFactory.register(OpenStackTarget.HUAWEI,  HuaweiAdapter)
AdapterFactory.register(OpenStackTarget.CUSTOM,  CustomAdapter)
