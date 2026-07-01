"""
src/extractor/vmware_extractor.py
──────────────────────────────────
VMware discovery module — pyVmomi implementation.

Extracts from vCenter / ESXi:
  - Full VM inventory: CPU, RAM, OS, disks, NICs, snapshots
  - Real-time performance counters via vCenter PerformanceManager
  - Custom attributes and tags
  - Datastore and cluster location

All extracted data is returned as VMwareVM dataclass instances.
No business logic here — pure data collection.
"""

from __future__ import annotations

import logging
import ssl
from datetime import datetime
from typing import Any, Dict, List, Optional

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

from src.core.models import (
    DiskFormat, DiskType, NetworkAdapterType,
    PowerState, VMwareDisk, VMwareNIC,
    VMwarePerformanceMetrics, VMwareSnapshot, VMwareVM,
)

logger = logging.getLogger("migration.extractor")

# vSphere performance counter keys we collect.
# Format: "{group}.{counter}.{rollupType}"
_PERF_COUNTERS = {
    "cpu.usage.average":                    "cpu_usage_percent",
    "cpu.usagemhz.average":                 "cpu_usage_mhz",
    "mem.active.average":                   "memory_active_mb",
    "mem.vmmemctl.average":                 "memory_balloon_mb",
    "disk.read.average":                    "disk_read_kbps",
    "disk.write.average":                   "disk_write_kbps",
    "disk.numberReadAveraged.average":      "disk_read_iops",
    "disk.numberWriteAveraged.average":     "disk_write_iops",
    "net.received.average":                 "net_rx_kbps",
    "net.transmitted.average":              "net_tx_kbps",
}

_POWER_STATE_MAP = {
    "poweredOn":  PowerState.RUNNING,
    "poweredOff": PowerState.STOPPED,
    "suspended":  PowerState.PAUSED,
}


class VMwareExtractor:
    """
    Connects to vCenter (or ESXi directly) and extracts VM data via pyVmomi.

    Handles SSL certificate bypass for lab/test environments.
    Uses container views for efficient bulk enumeration.
    """

    def __init__(self, config: Dict[str, Any]):
        self.host       = config["host"]
        self.port       = config.get("port", 443)
        self.username   = config["username"]
        self.password   = config["password"]
        self.ssl_verify = config.get("ssl_verify", False)
        self.datacenter = config.get("datacenter")
        self._metrics_interval = config.get("metrics_interval_s", 300)

        self._si: Optional[Any]      = None   # vim.ServiceInstance
        self._content: Optional[Any] = None   # vim.ServiceInstanceContent
        self._counter_map: Dict[str, int] = {}  # counter_key → counter_id

    # ── Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            if self.ssl_verify:
                self._si = SmartConnect(
                    host=self.host, user=self.username,
                    pwd=self.password, port=self.port,
                )
            else:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
                self._si = SmartConnect(
                    host=self.host, user=self.username,
                    pwd=self.password, port=self.port,
                    sslContext=ctx,
                )
            self._content = self._si.RetrieveContent()
            self._build_counter_map()
            logger.info(f"Connected to vCenter: {self.host}")
            return True
        except Exception as exc:
            logger.error(f"vCenter connection failed ({self.host}): {exc}")
            raise

    def disconnect(self) -> None:
        if self._si:
            try:
                Disconnect(self._si)
            except Exception:
                pass
            self._si = None
            self._content = None

    # ── VM discovery ─────────────────────────────────────────────

    def list_vms(self, datacenter: Optional[str] = None) -> List[VMwareVM]:
        """Return all accessible VMs, optionally scoped to a datacenter."""
        view = self._container_view(datacenter or self.datacenter)
        vms  = []
        for mo in view.view:
            try:
                vm = self._extract_vm(mo)
                if vm:
                    vms.append(vm)
            except Exception as exc:
                logger.warning(f"Skipped VM '{getattr(mo, 'name', '?')}': {exc}")
        view.Destroy()
        logger.info(f"Discovered {len(vms)} VMs")
        return vms

    # ── Hierarchical navigation: DC → Cluster → ESXi → VMs ──────

    def list_topology(self, datacenter: Optional[str] = None) -> dict:
        """
        Return the full vSphere topology for a datacenter:
        {
          "datacenter": "DC-PROD",
          "clusters": [
            {
              "name": "Cluster-01",
              "mor_id": "domain-c10",
              "hosts": [
                {
                  "name": "esxi-01.orange-it.local",
                  "mor_id": "host-22",
                  "model": "Dell PowerEdge R740",
                  "cpu_model": "Intel Xeon Gold 6154",
                  "cpu_cores": 36,
                  "memory_gb": 256,
                  "power_state": "poweredOn",
                  "connection_state": "connected",
                  "vm_count": 12,
                  "vms": [ ... ]   # lightweight VM list
                }
              ]
            }
          ],
          "standalone_hosts": [ ... ]   # ESXi not in a cluster
        }
        """
        dc_name = datacenter or self.datacenter
        content = self._content

        # Find datacenter object
        dc_obj = self._find_datacenter(dc_name)
        if not dc_obj:
            logger.warning(f"Datacenter '{dc_name}' not found")
            return {"datacenter": dc_name, "clusters": [], "standalone_hosts": []}

        host_folder = dc_obj.hostFolder
        clusters         = []
        standalone_hosts = []

        for child in self._iter_folder(host_folder):
            if hasattr(child, "host"):
                # It's a ClusterComputeResource
                cluster_info = self._extract_cluster(child)
                clusters.append(cluster_info)
            elif hasattr(child, "config") and hasattr(child, "vm"):
                # Standalone ComputeResource (single ESXi)
                host = self._extract_host(child.host[0] if child.host else child)
                standalone_hosts.append(host)

        return {
            "datacenter":       dc_name,
            "clusters":         clusters,
            "standalone_hosts": standalone_hosts,
        }

    def list_hosts(self, datacenter: Optional[str] = None) -> list:
        """
        Return a flat list of all ESXi hosts with their VM count.
        Faster than list_topology when you don't need the cluster tree.
        """
        dc_name  = datacenter or self.datacenter
        dc_obj   = self._find_datacenter(dc_name)
        if not dc_obj:
            return []

        from pyVmomi import vim
        container = self._content.viewManager.CreateContainerView(
            dc_obj, [vim.HostSystem], True
        )
        hosts = []
        for host_mo in container.view:
            try:
                hosts.append(self._extract_host(host_mo))
            except Exception as exc:
                logger.warning(f"Skipped host '{getattr(host_mo,'name','?')}': {exc}")
        container.Destroy()
        logger.info(f"Discovered {len(hosts)} ESXi hosts")
        return hosts

    def list_vms_on_host(self, host_mor_id: str) -> list:
        """
        Return all VMs running on a specific ESXi host (by MOR ID).
        """
        from pyVmomi import vim
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.HostSystem], True
        )
        target_host = None
        for host_mo in container.view:
            if str(host_mo._moId) == host_mor_id:
                target_host = host_mo
                break
        container.Destroy()

        if not target_host:
            logger.warning(f"Host MOR '{host_mor_id}' not found")
            return []

        vms = []
        for vm_mo in (target_host.vm or []):
            try:
                vm = self._extract_vm(vm_mo)
                if vm:
                    vms.append(vm)
            except Exception as exc:
                logger.warning(f"Skipped VM on host: {exc}")

        logger.info(f"Found {len(vms)} VMs on host {host_mor_id}")
        return vms

    # ── Private: cluster / host extraction ───────────────────────

    def _find_datacenter(self, dc_name: Optional[str]):
        """Find a datacenter MO by name, or return the first one."""
        from pyVmomi import vim
        container = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datacenter], True
        )
        dc = None
        for obj in container.view:
            if not dc_name or obj.name == dc_name:
                dc = obj
                break
        container.Destroy()
        return dc

    def _iter_folder(self, folder):
        """Recursively iterate children of a folder."""
        for child in (folder.childEntity or []):
            from pyVmomi import vim
            if isinstance(child, vim.Folder):
                yield from self._iter_folder(child)
            else:
                yield child

    def _extract_cluster(self, cluster_mo) -> dict:
        """Extract cluster info with all its hosts."""
        hosts = []
        for host_mo in (cluster_mo.host or []):
            try:
                hosts.append(self._extract_host(host_mo))
            except Exception as exc:
                logger.warning(f"Skipped host in cluster: {exc}")
        return {
            "name":       cluster_mo.name,
            "mor_id":     str(cluster_mo._moId),
            "host_count": len(hosts),
            "hosts":      hosts,
        }

    def _extract_host(self, host_mo) -> dict:
        """Extract a single ESXi host's info (lightweight — no VM details)."""
        hw      = host_mo.hardware
        summary = host_mo.summary
        config  = host_mo.config

        cpu_cores = 0
        cpu_model = ""
        memory_gb = 0.0
        if hw:
            cpu_cores = hw.cpuInfo.numCpuCores if hw.cpuInfo else 0
            memory_gb = round(hw.memorySize / (1024 ** 3), 1) if hw.memorySize else 0
        if config and config.product:
            pass  # ESXi version info
        if summary and summary.hardware:
            cpu_model = summary.hardware.cpuModel or ""

        vm_count = len(host_mo.vm or [])

        # Datastores summary
        datastores = []
        for ds in (host_mo.datastore or []):
            try:
                ds_info = ds.summary
                datastores.append({
                    "name":       ds.name,
                    "capacity_gb": round(ds_info.capacity / (1024**3), 1) if ds_info.capacity else 0,
                    "free_gb":     round(ds_info.freeSpace / (1024**3), 1) if ds_info.freeSpace else 0,
                    "type":        ds_info.type or "unknown",
                })
            except Exception:
                pass

        return {
            "name":             host_mo.name,
            "mor_id":           str(host_mo._moId),
            "model":            getattr(getattr(hw, "systemInfo", None), "model", "") if hw else "",
            "cpu_model":        cpu_model,
            "cpu_cores":        cpu_cores,
            "memory_gb":        memory_gb,
            "power_state":      str(host_mo.runtime.powerState) if host_mo.runtime else "unknown",
            "connection_state": str(host_mo.runtime.connectionState) if host_mo.runtime else "unknown",
            "in_maintenance":   host_mo.runtime.inMaintenanceMode if host_mo.runtime else False,
            "vm_count":         vm_count,
            "datastores":       datastores,
        }

    def get_vm(self, vm_name: str) -> Optional[VMwareVM]:
        """Return a single VM by exact name. Returns None if not found."""
        view = self._container_view(self.datacenter)
        result = None
        for mo in view.view:
            if mo.name == vm_name:
                result = self._extract_vm(mo)
                break
        view.Destroy()
        if result is None:
            logger.warning(f"VM '{vm_name}' not found in vSphere")
        return result

    def collect_metrics(
        self,
        vm: VMwareVM,
        duration_seconds: Optional[int] = None,
    ) -> VMwareVM:
        """
        Enrich vm.performance with real-time counters from the
        vCenter Performance Manager.
        Returns the same VMwareVM with performance field populated.
        """
        interval = duration_seconds or self._metrics_interval
        mo = self._find_mo_by_id(vm.mor_id)
        if not mo:
            logger.warning(f"Cannot collect metrics: MOR '{vm.mor_id}' not found")
            return vm
        try:
            vm.performance = self._query_performance(mo, interval)
            logger.info(
                f"Metrics for '{vm.name}': "
                f"CPU={vm.performance.cpu_usage_percent:.1f}% | "
                f"RAM active={vm.performance.memory_active_mb:.0f}MB | "
                f"Disk R={vm.performance.disk_read_kbps:.0f}kbps"
            )
        except Exception as exc:
            logger.warning(f"Performance collection failed for '{vm.name}': {exc}")
        return vm

    # ── Private: VM extraction ───────────────────────────────────

    def _extract_vm(self, mo: Any) -> Optional[VMwareVM]:
        """Build VMwareVM from a vim.VirtualMachine managed object."""
        cfg     = mo.config
        summary = mo.summary
        runtime = mo.runtime
        guest   = mo.guest

        if not cfg:
            return None  # Template or inaccessible VM

        vm = VMwareVM(
            mor_id=str(mo._moId),
            name=mo.name,
            instance_uuid=cfg.instanceUuid or "",
            bios_uuid=cfg.uuid or "",
            num_cpus=cfg.hardware.numCPU,
            num_cores_per_socket=cfg.hardware.numCoresPerSocket,
            memory_mb=cfg.hardware.memoryMB,
            cpu_reservation_mhz=cfg.cpuAllocation.reservation if cfg.cpuAllocation else 0,
            memory_reservation_mb=cfg.memoryAllocation.reservation if cfg.memoryAllocation else 0,
            guest_id=cfg.guestId or "",
            guest_full_name=cfg.guestFullName or "",
            hostname=(guest.hostName or "") if guest else "",
            vmware_tools_status=(
                summary.guest.toolsStatus.name
                if summary.guest and summary.guest.toolsStatus else ""
            ),
            power_state=_POWER_STATE_MAP.get(
                runtime.powerState.lower() if runtime.powerState else "",
                PowerState.STOPPED
            ),
            annotation=cfg.annotation or "",
            extracted_at=datetime.now(),
        )

        # Location
        if runtime.host:
            vm.host_fqdn = runtime.host.name or ""
            parent = runtime.host.parent
            if isinstance(parent, vim.ClusterComputeResource):
                vm.cluster = parent.name
        dc = self._find_datacenter(mo)
        if dc:
            vm.datacenter = dc.name

        # Datastores
        if cfg.datastoreUrl:
            vm.datastore = cfg.datastoreUrl[0].name

        # Hardware
        vm.disks, vm.total_disk_gb = self._extract_disks(cfg.hardware.device)
        vm.nics = self._extract_nics(cfg.hardware.device, guest)

        # Snapshots
        if mo.snapshot:
            vm.has_snapshots = True
            vm.snapshots = self._walk_snapshots(mo.snapshot.rootSnapshotList)

        # Custom attributes
        vm.custom_attributes = self._custom_attrs(mo)

        return vm

    def _extract_disks(
        self, devices: List[Any]
    ) -> tuple[List[VMwareDisk], float]:
        disks: List[VMwareDisk] = []
        total  = 0.0

        for dev in devices:
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                continue

            size_gb = dev.capacityInKB / (1024 * 1024)
            total  += size_gb

            backing   = dev.backing
            vmdk_path = ""
            datastore = ""
            thin      = False

            if isinstance(backing, vim.vm.device.VirtualDisk.FlatVer2BackingInfo):
                vmdk_path = backing.fileName or ""
                thin      = backing.thinProvisioned or False
                datastore = backing.datastore.name if backing.datastore else ""

            disks.append(VMwareDisk(
                label=dev.deviceInfo.label if dev.deviceInfo else f"disk-{len(disks)}",
                disk_type=DiskType.OS if not disks else DiskType.DATA,
                size_gb=round(size_gb, 2),
                format=DiskFormat.VMDK,
                vmdk_path=vmdk_path,
                datastore=datastore,
                thin_provisioned=thin,
                controller_unit=dev.unitNumber or 0,
            ))

        return disks, round(total, 2)

    def _extract_nics(
        self, devices: List[Any], guest: Optional[Any]
    ) -> List[VMwareNIC]:
        # Build IP map from guest info (key = device key)
        ip_by_key: Dict[int, List[str]] = {}
        if guest and guest.net:
            for net_info in guest.net:
                ips = []
                if net_info.ipConfig:
                    ips = [
                        ip.ipAddress
                        for ip in net_info.ipConfig.ipAddress
                        if ":" not in ip.ipAddress  # IPv4 only
                    ]
                ip_by_key[net_info.deviceConfigId] = ips

        nics: List[VMwareNIC] = []
        for dev in devices:
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                continue

            if isinstance(dev, vim.vm.device.VirtualE1000e):
                adapter_type = NetworkAdapterType.E1000E
            elif isinstance(dev, vim.vm.device.VirtualE1000):
                adapter_type = NetworkAdapterType.E1000
            else:
                adapter_type = NetworkAdapterType.VMXNET3

            network_name  = ""
            dvswitch_uuid = ""

            if isinstance(dev.backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                network_name = dev.backing.deviceName or ""
            elif isinstance(
                dev.backing,
                vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo,
            ):
                dvswitch_uuid = dev.backing.port.switchUuid or ""
                network_name  = dev.backing.port.portgroupKey or ""

            nics.append(VMwareNIC(
                label=dev.deviceInfo.label if dev.deviceInfo else f"nic-{len(nics)}",
                mac_address=dev.macAddress or "",
                network_name=network_name,
                dvswitch_uuid=dvswitch_uuid,
                adapter_type=adapter_type,
                connected=dev.connectable.connected if dev.connectable else True,
                ip_addresses=ip_by_key.get(dev.key, []),
            ))

        return nics

    def _walk_snapshots(self, tree: List[Any]) -> List[VMwareSnapshot]:
        result = []
        for node in tree:
            snap = VMwareSnapshot(
                snapshot_id=str(node.snapshot._moId),
                name=node.name,
                description=node.description or "",
                created_at=node.createTime,
            )
            snap.children = self._walk_snapshots(node.childSnapshotList)
            result.append(snap)
        return result

    def _custom_attrs(self, mo: Any) -> Dict[str, str]:
        attrs: Dict[str, str] = {}
        try:
            if mo.customValue and self._content.customFieldsManager:
                field_map = {f.key: f.name for f in self._content.customFieldsManager.field}
                for v in mo.customValue:
                    attrs[field_map.get(v.key, str(v.key))] = v.value
        except Exception:
            pass
        return attrs

    # ── Private: performance counters ────────────────────────────

    def _build_counter_map(self) -> None:
        """Map human-readable counter key strings → vSphere numeric counter IDs."""
        for c in self._content.perfManager.perfCounter:
            key = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType.lower()}"
            self._counter_map[key] = c.key

    def _query_performance(
        self, mo: Any, interval_s: int
    ) -> VMwarePerformanceMetrics:
        """Query vCenter Performance Manager for recent rolling averages."""
        pm = self._content.perfManager

        metric_ids = [
            vim.PerformanceManager.MetricId(counterId=self._counter_map[k], instance="*")
            for k in _PERF_COUNTERS
            if k in self._counter_map
        ]
        if not metric_ids:
            return VMwarePerformanceMetrics()

        query = vim.PerformanceManager.QuerySpec(
            entity=mo,
            metricId=metric_ids,
            intervalId=20,                     # 20-second real-time granularity
            maxSample=max(1, interval_s // 20),
        )
        results = pm.QueryPerf(querySpec=[query])

        buckets: Dict[str, List[float]] = {}
        for entity_result in (results or []):
            for series in entity_result.value:
                cid  = series.id.counterId
                name = next(
                    (n for n, cid_ in self._counter_map.items() if cid_ == cid), None
                )
                if not name:
                    continue
                attr = _PERF_COUNTERS.get(name)
                if not attr:
                    continue
                vals = [v for v in series.value if v > 0]
                if vals:
                    buckets.setdefault(attr, []).extend(vals)

        def avg(key: str, div: float = 1.0) -> float:
            vs = buckets.get(key, [])
            return round(sum(vs) / len(vs) / div, 2) if vs else 0.0

        return VMwarePerformanceMetrics(
            cpu_usage_percent  = avg("cpu_usage_percent", 100.0),  # stored as 0-10000
            cpu_usage_mhz      = avg("cpu_usage_mhz"),
            memory_active_mb   = avg("memory_active_mb", 1024.0),  # KB → MB
            memory_balloon_mb  = avg("memory_balloon_mb", 1024.0),
            disk_read_kbps     = avg("disk_read_kbps"),
            disk_write_kbps    = avg("disk_write_kbps"),
            disk_read_iops     = avg("disk_read_iops"),
            disk_write_iops    = avg("disk_write_iops"),
            net_rx_kbps        = avg("net_rx_kbps"),
            net_tx_kbps        = avg("net_tx_kbps"),
            collection_interval_s=interval_s,
            collected_at=datetime.now(),
        )

    # ── Private: vSphere traversal helpers ───────────────────────

    def _container_view(self, datacenter_name: Optional[str]) -> Any:
        root = self._content.rootFolder
        if datacenter_name:
            dc = self._find_dc_by_name(datacenter_name)
            if dc:
                root = dc.vmFolder
        return self._content.viewManager.CreateContainerView(
            root, [vim.VirtualMachine], True
        )

    def _find_dc_by_name(self, name: str) -> Optional[Any]:
        view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim.Datacenter], True
        )
        dc = next((d for d in view.view if d.name == name), None)
        view.Destroy()
        return dc

    def _find_datacenter(self, mo: Any) -> Optional[Any]:
        parent = getattr(mo, "parent", None)
        while parent:
            if isinstance(parent, vim.Datacenter):
                return parent
            parent = getattr(parent, "parent", None)
        return None

    def _find_mo_by_id(self, mor_id: str) -> Optional[Any]:
        view = self._container_view(self.datacenter)
        mo   = next((m for m in view.view if str(m._moId) == mor_id), None)
        view.Destroy()
        return mo
