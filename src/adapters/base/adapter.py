"""
src/adapters/base/adapter.py
────────────────────────────
Abstract base adapter — GoF Adapter Pattern + Template Method.

Defines the full contract every OpenStack target must satisfy.
Concrete adapters (RedHat, Huawei, Custom) override only what diverges
from standard openstacksdk behaviour.

Responsibility split:
  BaseOpenStackAdapter  → interface + shared utility methods (wait, delete, verify)
  Concrete adapters     → auth quirks, volume types, network lookup, scheduler hints
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from src.core.models import (
    ConversionResult, NetworkMapping,
    OpenStackFlavorSpec, OpenStackInstance,
    OpenStackTarget, OpenStackVolume,
)


class OpenStackAdapterError(Exception):
    """Raised when an adapter operation fails — wraps target context."""
    def __init__(self, message: str, target: OpenStackTarget, operation: str):
        self.target    = target
        self.operation = operation
        super().__init__(f"[{target.value.upper()}:{operation}] {message}")


class BaseOpenStackAdapter(ABC):
    """
    Abstract adapter for a single OpenStack target environment.

    Typical usage inside the Deployer:
        adapter = AdapterFactory.create(OpenStackTarget.REDHAT, config)
        adapter.connect()
        image_id   = adapter.upload_image(qcow2_path, name)
        boot_vol   = adapter.create_volume_from_image(image_id, ...)
        mappings   = adapter.resolve_network_mappings(mappings)
        mappings   = adapter.create_ports(mappings, vm_name)
        instance   = adapter.boot_instance(conversion, flavor_id, boot_vol.volume_id, ...)
        ok, issues = adapter.verify_instance(instance)
        # on failure:
        adapter.rollback(instance)
        adapter.disconnect()
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._conn  = None                  # openstack.connection.Connection
        self._target: OpenStackTarget       # must be set by subclass __init__

    # ── Connection ───────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> bool:
        """
        Authenticate against the target OpenStack (Keystone).
        Must set self._conn.
        Returns True on success, raises OpenStackAdapterError on failure.
        """
        ...

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def is_connected(self) -> bool:
        return self._conn is not None

    # ── Glance — Image management ────────────────────────────────

    @abstractmethod
    def upload_image(
        self,
        qcow2_path: str,
        image_name: str,
        disk_format: str = "qcow2",
        container_format: str = "bare",
        extra_properties: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Upload a QCOW2 file to Glance.
        Adapters inject environment-specific image properties.
        Returns the image UUID.
        """
        ...

    def get_image(self, name_or_id: str) -> Optional[Any]:
        self._require_connection("get_image")
        return self._conn.image.find_image(name_or_id)

    def delete_image(self, image_id: str) -> bool:
        try:
            self._conn.image.delete_image(image_id, ignore_missing=True)
            return True
        except Exception:
            return False

    # ── Nova — Flavor management ─────────────────────────────────

    def resolve_flavor(self, spec: OpenStackFlavorSpec) -> str:
        """
        Returns a flavor ID for the given spec.
        Tries existing flavors first; calls create_flavor() if not found.
        Adapters may override to inject env-specific extra_specs.
        """
        self._require_connection("resolve_flavor")

        if spec.is_existing and spec.flavor_id:
            return spec.flavor_id

        flavor = self._conn.compute.find_flavor(spec.name)
        if flavor:
            return flavor.id

        return self.create_flavor(spec)

    @abstractmethod
    def create_flavor(self, spec: OpenStackFlavorSpec) -> str:
        """
        Create a Nova flavor with environment-specific extra_specs.
        Returns the flavor ID.
        """
        ...

    # ── Cinder — Volume management ───────────────────────────────

    @abstractmethod
    def create_volume_from_image(
        self,
        image_id: str,
        size_gb: int,
        volume_name: str,
        volume_type: Optional[str] = None,
        bootable: bool = False,
    ) -> OpenStackVolume:
        """
        Create a Cinder volume from a Glance image.
        Adapters select the correct volume type for the environment.
        Blocks until the volume reaches 'available'.
        """
        ...

    def create_empty_volume(
        self,
        size_gb: int,
        volume_name: str,
        volume_type: Optional[str] = None,
    ) -> OpenStackVolume:
        """Create a blank data volume (no image source)."""
        self._require_connection("create_empty_volume")

        vtype = volume_type or self.config.get("default_volume_type")
        kwargs: Dict[str, Any] = {"name": volume_name, "size": size_gb}
        if vtype:
            kwargs["volume_type"] = vtype

        vol = self._conn.block_storage.create_volume(**kwargs)
        self._conn.block_storage.wait_for_status(
            vol, status="available", failures=["error"], interval=5, wait=300
        )
        return OpenStackVolume(
            volume_id=vol.id,
            name=volume_name,
            size_gb=size_gb,
            volume_type=vtype or "",
            status="available",
        )

    def delete_volume(self, volume_id: str) -> bool:
        try:
            self._conn.block_storage.delete_volume(volume_id, ignore_missing=True)
            return True
        except Exception:
            return False

    # ── Neutron — Network management ─────────────────────────────

    @abstractmethod
    def resolve_network_mappings(
        self,
        mappings: List[NetworkMapping],
    ) -> List[NetworkMapping]:
        """
        Resolve VMware portgroup names → Neutron network/subnet IDs.
        Each adapter implements environment-specific lookup logic:
          - RedHat: OVN provider:segmentation_id lookup
          - Huawei: ManageOne enterprise project prefix
          - Custom: direct name match
        Returns enriched mappings (with openstack_network_id populated).
        """
        ...

    @abstractmethod
    def create_ports(
        self,
        mappings: List[NetworkMapping],
        instance_name: str,
    ) -> List[NetworkMapping]:
        """
        Pre-create Neutron ports before boot (MAC/IP control).
        Returns mappings enriched with openstack_port_id.
        """
        ...

    def delete_port(self, port_id: str) -> bool:
        try:
            self._conn.network.delete_port(port_id, ignore_missing=True)
            return True
        except Exception:
            return False

    # ── Nova — Instance lifecycle ────────────────────────────────

    @abstractmethod
    def boot_instance(
        self,
        conversion: ConversionResult,
        flavor_id: str,
        boot_volume_id: str,
        port_ids: List[str],
        extra_volume_ids: List[str],
    ) -> OpenStackInstance:
        """
        Boot a Nova instance from a Cinder boot volume.
        Adapters inject scheduler hints, AZ, enterprise metadata.
        Must call wait_for_active() before returning.
        """
        ...

    def wait_for_active(
        self,
        instance_id: str,
        timeout_s: int = 600,
        poll_interval_s: int = 10,
    ) -> bool:
        """
        Poll Nova until instance reaches ACTIVE or ERROR.
        Raises OpenStackAdapterError on ERROR or timeout.
        """
        import time
        self._require_connection("wait_for_active")

        elapsed = 0
        while elapsed < timeout_s:
            server = self._conn.compute.get_server(instance_id)
            if server.status == "ACTIVE":
                return True
            if server.status == "ERROR":
                fault = getattr(server, "fault", {}) or {}
                raise OpenStackAdapterError(
                    f"Instance entered ERROR state. Fault: {fault.get('message', 'unknown')}",
                    self._target, "wait_for_active",
                )
            time.sleep(poll_interval_s)
            elapsed += poll_interval_s

        raise OpenStackAdapterError(
            f"Timeout after {timeout_s}s waiting for instance {instance_id}",
            self._target, "wait_for_active",
        )

    def delete_instance(self, instance_id: str) -> bool:
        try:
            self._conn.compute.delete_server(instance_id, force=True)
            return True
        except Exception:
            return False

    # ── Post-deployment verification ─────────────────────────────

    def verify_instance(
        self,
        instance: OpenStackInstance,
    ) -> Tuple[bool, List[str]]:
        """
        Standard post-deployment health checks.
        Adapters may call super() then add env-specific checks.
        Returns (passed: bool, issues: List[str]).
        """
        issues = []
        if not self._conn:
            return False, ["Adapter not connected"]

        server = self._conn.compute.get_server(instance.instance_id)

        if server.status != "ACTIVE":
            issues.append(f"Status is {server.status!r}, expected 'ACTIVE'")

        if not (server.addresses or instance.ip_addresses):
            issues.append("No network addresses assigned")

        for vol in instance.volumes:
            if vol.bootable:
                v = self._conn.block_storage.get_volume(vol.volume_id)
                if not getattr(v, "attachments", []):
                    issues.append(f"Boot volume {vol.volume_id} not attached")

        return len(issues) == 0, issues

    # ── Rollback ─────────────────────────────────────────────────

    def rollback(self, instance: OpenStackInstance) -> List[str]:
        """
        Destroy all resources created for this instance.
        Template method — adapters may override for env-specific cleanup.
        Returns list of actions taken.
        """
        actions: List[str] = []

        if instance.instance_id:
            if self.delete_instance(instance.instance_id):
                actions.append(f"Deleted instance {instance.instance_id}")

        for vol in instance.volumes:
            if self.delete_volume(vol.volume_id):
                actions.append(f"Deleted volume {vol.volume_id}")

        return actions

    # ── Identity ─────────────────────────────────────────────────

    @property
    def target(self) -> OpenStackTarget:
        return self._target

    @property
    def name(self) -> str:
        return self._target.value

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"target={self._target.value} "
            f"connected={self.is_connected()}>"
        )

    # ── Internal helpers ─────────────────────────────────────────

    def _require_connection(self, operation: str) -> None:
        if not self._conn:
            raise OpenStackAdapterError(
                "Adapter is not connected — call connect() first",
                self._target, operation,
            )

    def _build_block_device_mapping(
        self,
        boot_volume_id: str,
        extra_volume_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Build the block_device_mapping_v2 payload for Nova boot."""
        bdm = [{
            "boot_index":            "0",
            "uuid":                  boot_volume_id,
            "source_type":           "volume",
            "destination_type":      "volume",
            "delete_on_termination": False,
        }]
        for vol_id in extra_volume_ids:
            bdm.append({
                "boot_index":            "-1",
                "uuid":                  vol_id,
                "source_type":           "volume",
                "destination_type":      "volume",
                "delete_on_termination": False,
            })
        return bdm
