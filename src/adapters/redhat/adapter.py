"""
src/adapters/redhat/adapter.py
──────────────────────────────
Red Hat OpenStack Platform (RHOSP) adapter.

RHOSP-specific behaviour:
  - Keystone v3 with application credentials (Red Hat SSO / IdM federation)
  - OVN Neutron: VLAN resolution via provider:segmentation_id
  - Ceph / TripleO volume types: ceph, tripleo_iscsi
  - Nova scheduler: host aggregate affinity, NUMA-aware extra_specs
  - Glance: hw_scsi_model=virtio-scsi, hw_disk_bus=scsi (required for SCSI passthrough)
  - OVN port binding verification post-boot
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

logger = logging.getLogger("migration.adapter.redhat")


class RedHatAdapter(BaseOpenStackAdapter):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._target     = OpenStackTarget.REDHAT
        self._rh_config  = config.get("redhat", {})

    # ── Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            # RHOSP supports application credentials for service accounts
            if "application_credential_id" in self.config:
                auth_kwargs = dict(
                    auth_url=self.config["auth_url"],
                    application_credential_id=self.config["application_credential_id"],
                    application_credential_secret=self.config["application_credential_secret"],
                    auth_type="v3applicationcredential",
                )
            else:
                auth_kwargs = dict(
                    auth_url=self.config["auth_url"],
                    project_name=self.config["project_name"],
                    username=self.config["username"],
                    password=self.config["password"],
                    user_domain_name=self.config.get("user_domain_name", "Default"),
                    project_domain_name=self.config.get("project_domain_name", "Default"),
                )

            self._conn = openstack.connect(
                **auth_kwargs,
                verify=self.config.get("ssl_verify", True),
                region_name=self.config.get("region_name", "RegionOne"),
            )
            self._conn.authorize()
            logger.info("Connected to Red Hat OpenStack Platform (RHOSP)")
            return True
        except Exception as exc:
            raise OpenStackAdapterError(str(exc), self._target, "connect") from exc

    # ── Glance ───────────────────────────────────────────────────

    def upload_image(
        self,
        qcow2_path: str,
        image_name: str,
        disk_format: str = "qcow2",
        container_format: str = "bare",
        extra_properties: Optional[Dict[str, str]] = None,
    ) -> str:
        self._require_connection("upload_image")

        # RHOSP: virtio-scsi provides better performance and disk hotplug support
        props: Dict[str, str] = {
            "hw_scsi_model":  "virtio-scsi",
            "hw_disk_bus":    "scsi",
            "hw_vif_model":   "virtio",
            "os_type":        "linux",
        }
        props.update(self._rh_config.get("image_properties", {}))
        if extra_properties:
            props.update(extra_properties)

        logger.info(f"[RHOSP] Uploading image '{image_name}' ← {qcow2_path}")
        image = self._conn.image.create_image(
            name=image_name,
            disk_format=disk_format,
            container_format=container_format,
            visibility="private",
            properties=props,
        )
        with open(qcow2_path, "rb") as fh:
            import requests as _req, urllib3 as _u3
            _u3.disable_warnings()
            token = self._conn.auth_token
            glance_url = self._conn.endpoint_for("image")
            url = f"{glance_url}/v2/images/{image.id}/file"
            r = _req.put(url, data=fh,
                headers={"X-Auth-Token": token, "Content-Type": "application/octet-stream"},
                verify=self.config.get("ssl_verify", False))
            r.raise_for_status()
        # Attendre que l image soit active
        import time as _time
        for _ in range(120):
            img = self._conn.image.get_image(image.id)
            if img.status == "active": break
            if img.status == "killed": raise Exception(f"Image {image.id} en erreur")
            _time.sleep(5)
        logger.info(f"[RHOSP] Image active: {image.id}")
        return image.id

    # ── Flavor ───────────────────────────────────────────────────

    def create_flavor(self, spec: OpenStackFlavorSpec) -> str:
        self._require_connection("create_flavor")

        extra: Dict[str, str] = {
            "hw:mem_page_size": "any",   # NUMA-aware — telecom workloads
        }
        extra.update(spec.extra_specs)

        flavor = self._conn.compute.create_flavor(
            name=spec.name,
            vcpus=spec.vcpus,
            ram=spec.ram_mb,
            disk=spec.disk_gb,
            ephemeral=spec.ephemeral_gb,
        )
        self._conn.compute.create_flavor_extra_specs(flavor.id, extra)
        logger.info(f"[RHOSP] Flavor created: {flavor.id} ({spec.name})")
        return flavor.id

    # ── Cinder ───────────────────────────────────────────────────

    def create_volume_from_image(
        self,
        image_id: str,
        size_gb: int,
        volume_name: str,
        volume_type: Optional[str] = None,
        bootable: bool = False,
    ) -> OpenStackVolume:
        self._require_connection("create_volume_from_image")

        vtype = volume_type or self._rh_config.get("volume_type") or None
        logger.info(f"[RHOSP] Creating volume '{volume_name}' ({size_gb}GB, type={vtype})")

        vol_kwargs = dict(name=volume_name, size=size_gb, image_id=image_id)
        if vtype:
            vol_kwargs["volume_type"] = vtype
        vol = self._conn.block_storage.create_volume(**vol_kwargs)
        self._conn.block_storage.wait_for_status(
            vol, status="available", failures=["error"], interval=10, wait=3600
        )
        logger.info(f"[RHOSP] Volume ready: {vol.id}")
        if bootable:
            self._conn.block_storage.set_volume_bootable_status(vol.id, True)
        return OpenStackVolume(
            volume_id=vol.id,
            name=volume_name,
            size_gb=size_gb,
            volume_type=vtype,
            status="available",
            bootable=bootable,
        )

    # ── Neutron (OVN) ────────────────────────────────────────────

    def resolve_network_mappings(
        self,
        mappings: List[NetworkMapping],
    ) -> List[NetworkMapping]:
        self._require_connection("resolve_network_mappings")

        for m in mappings:
            # Try direct name match first
            net = self._conn.network.find_network(m.vmware_network_name)

            # OVN fallback: match on VLAN segmentation_id
            if not net and m.vmware_vlan_id is not None:
                candidates = list(self._conn.network.networks(
                    **{"provider:segmentation_id": m.vmware_vlan_id}
                ))
                net = candidates[0] if candidates else None

            if net:
                subnet = next(iter(self._conn.network.subnets(network_id=net.id)), None)
                m.openstack_network_id   = net.id
                m.openstack_network_name = net.name
                m.openstack_subnet_id    = subnet.id if subnet else ""
                logger.info(f"[RHOSP] Network resolved: '{m.vmware_network_name}' → {net.id}")
            else:
                logger.warning(f"[RHOSP] No Neutron network found for '{m.vmware_network_name}'")

        return mappings

    def create_ports(
        self,
        mappings: List[NetworkMapping],
        instance_name: str,
    ) -> List[NetworkMapping]:
        self._require_connection("create_ports")

        for i, m in enumerate(mappings):
            if not m.openstack_network_id:
                logger.warning(f"[RHOSP] Skipping port #{i} — network not resolved")
                continue

            port_body: Dict[str, Any] = {
                "name":       f"{instance_name}-port-{i}",
                "network_id": m.openstack_network_id,
            }
            if m.openstack_subnet_id:
                fip: Dict[str, Any] = {"subnet_id": m.openstack_subnet_id}
                if m.ip_address:
                    fip["ip_address"] = m.ip_address
                port_body["fixed_ips"] = [fip]

            if m.security_groups:
                sg_ids = [
                    sg.id
                    for sg_name in m.security_groups
                    if (sg := self._conn.network.find_security_group(sg_name))
                ]
                if sg_ids:
                    port_body["security_groups"] = sg_ids

            port = self._conn.network.create_port(**port_body)
            m.openstack_port_id = port.id
            logger.info(f"[RHOSP] Port created: {port.id}")

        return mappings

    # ── Nova boot ────────────────────────────────────────────────

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
        name = vm.name

        # Boot from Volume (prod) ou Boot from Image (test)
        if boot_volume_id:
            bdm = self._build_block_device_mapping(boot_volume_id, extra_volume_ids)
        else:
            # Boot from Image directement
            glance_id = getattr(conversion, "glance_image_id", None)
            bdm = [{"uuid": glance_id, "source_type": "image",
                    "destination_type": "local", "boot_index": 0,
                    "delete_on_termination": True}] if glance_id else []
        networks = [{"port": pid} for pid in port_ids if pid]
        az       = self._rh_config.get("availability_zone", "nova")

        # Scheduler hints: host aggregate affinity
        hints: Dict[str, Any] = {}
        agg_name = self._rh_config.get("aggregate_name")
        if agg_name:
            agg = self._conn.compute.find_aggregate(agg_name)
            if agg:
                hints["aggregate_instance_extra_specs:aggregate"] = agg.name

        server_kwargs: Dict[str, Any] = {
            "name":                    name,
            "flavor_id":               flavor_id,
            "networks":                networks,
            # Boot from Volume (prod) ou Boot from Image (test)
            "availability_zone":       az,
            # user_data omis si None
            "metadata": {
                "migrated_from": "vmware",
                "source_uuid":   vm.instance_uuid,
                "framework":     "orange-migration-v2",
                **conversion.metadata,
            },
        }
        if hints:
            server_kwargs["scheduler_hints"] = hints

        logger.info(f"[RHOSP] Booting '{name}' | flavor={flavor_id} | az={az}")
        server = self._conn.compute.create_server(**server_kwargs)
        self.wait_for_active(server.id)
        server = self._conn.compute.get_server(server.id)

        addresses = {
            net: [a["addr"] for a in ads]
            for net, ads in (server.addresses or {}).items()
        }
        return OpenStackInstance(
            instance_id=server.id,
            name=name,
            target=self._target,
            flavor_name=conversion.flavor_spec.name if conversion.flavor_spec else "",
            status=server.status,
            ip_addresses=addresses,
            availability_zone=az,
            hypervisor_hostname=getattr(server, "OS-EXT-SRV-ATTR:hypervisor_hostname", ""),
        )

    # ── RHOSP-specific verification ──────────────────────────────

    def verify_instance(self, instance: OpenStackInstance) -> Tuple[bool, List[str]]:
        passed, issues = super().verify_instance(instance)

        # RHOSP: confirm OVN port is bound to a compute hypervisor
        try:
            server = self._conn.compute.get_server(instance.instance_id)
            if not getattr(server, "OS-EXT-SRV-ATTR:hypervisor_hostname", None):
                issues.append("OVN port not yet bound to a hypervisor")
        except Exception as exc:
            issues.append(f"OVN binding check failed: {exc}")

        return len(issues) == 0, issues
