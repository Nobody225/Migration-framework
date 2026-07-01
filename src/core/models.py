"""
src/core/models.py
──────────────────
Enterprise-grade data models for the VMware → multi-OpenStack migration framework.
Single source of truth for all cross-module entities.

Orange Group context: supports RedHat RHOSP, Huawei FusionSphere, Custom OpenStack.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ════════════════════════════════════════════════════════════════
# ENUMERATIONS
# ════════════════════════════════════════════════════════════════

class MigrationStatus(Enum):
    PENDING      = "pending"
    EXTRACTING   = "extracting"
    CONVERTING   = "converting"
    DEPLOYING    = "deploying"
    EVALUATING   = "evaluating"
    OPTIMIZING   = "optimizing"
    COMPLETED    = "completed"
    FAILED       = "failed"
    ROLLED_BACK  = "rolled_back"
    CANCELLED    = "cancelled"


class OpenStackTarget(Enum):
    """Supported OpenStack environments — Orange Group."""
    REDHAT  = "redhat"    # Red Hat OpenStack Platform (RHOSP)
    HUAWEI  = "huawei"    # Huawei FusionSphere / ManageOne
    CUSTOM  = "custom"    # Community-based in-house OpenStack


class MigrationMode(Enum):
    COLD = "cold"   # VM powered off before migration (safe, default)
    WARM = "warm"   # VM quiesced during disk copy (minimal downtime)


class DiskFormat(Enum):
    VMDK  = "vmdk"
    QCOW2 = "qcow2"
    RAW   = "raw"
    VHD   = "vhd"


class DiskType(Enum):
    OS   = "os"    # Boot disk
    DATA = "data"  # Additional data disk


class PowerState(Enum):
    RUNNING = "poweredOn"
    STOPPED = "poweredOff"
    PAUSED  = "suspended"


class LogLevel(Enum):
    DEBUG    = "DEBUG"
    INFO     = "INFO"
    WARNING  = "WARNING"
    ERROR    = "ERROR"
    CRITICAL = "CRITICAL"


class NetworkAdapterType(Enum):
    VMXNET3 = "VMXNET3"
    E1000   = "E1000"
    E1000E  = "E1000E"
    VIRTIO  = "virtio"


# ════════════════════════════════════════════════════════════════
# VMWARE SOURCE MODELS
# ════════════════════════════════════════════════════════════════

@dataclass
class VMwareDisk:
    """Virtual disk as reported by vSphere."""
    label: str                              # "Hard disk 1"
    disk_type: DiskType      = DiskType.OS
    size_gb: float           = 0.0
    format: DiskFormat       = DiskFormat.VMDK
    datastore: str           = ""
    vmdk_path: str           = ""           # "[datastore1] vm/vm.vmdk"
    thin_provisioned: bool   = True
    controller_type: str     = "SCSI"
    controller_unit: int     = 0
    uuid: str                = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class VMwareNIC:
    """Network interface as reported by vSphere."""
    label: str
    mac_address: str              = ""
    network_name: str             = ""      # Portgroup / DVPortgroup name
    dvswitch_uuid: str            = ""
    adapter_type: NetworkAdapterType = NetworkAdapterType.VMXNET3
    connected: bool               = True
    ip_addresses: List[str]       = field(default_factory=list)
    vlan_id: Optional[int]        = None


@dataclass
class VMwareSnapshot:
    snapshot_id: str
    name: str
    description: str                    = ""
    created_at: Optional[datetime]      = None
    is_current: bool                    = False
    children: List["VMwareSnapshot"]    = field(default_factory=list)


@dataclass
class VMwarePerformanceMetrics:
    """
    Performance counters from vCenter Performance Manager.
    Collected over a rolling window — used as migration baseline.
    """
    cpu_usage_percent: float   = 0.0
    cpu_usage_mhz: float       = 0.0
    memory_active_mb: float    = 0.0
    memory_balloon_mb: float   = 0.0
    disk_read_kbps: float      = 0.0
    disk_write_kbps: float     = 0.0
    disk_read_iops: float      = 0.0
    disk_write_iops: float     = 0.0
    net_rx_kbps: float         = 0.0
    net_tx_kbps: float         = 0.0
    collection_interval_s: int = 300
    collected_at: Optional[datetime] = None


@dataclass
class VMwareVM:
    """
    Complete representation of a VMware virtual machine.
    Single source of truth produced by the Extractor module.
    Consumed by Converter, Deployer, and Evaluator.
    """
    # Identifiers
    mor_id: str           # Managed Object Reference (vim.VirtualMachine._moId)
    name: str
    instance_uuid: str    = ""
    bios_uuid: str        = ""

    # Compute
    num_cpus: int                = 1
    num_cores_per_socket: int    = 1
    memory_mb: int               = 1024
    cpu_reservation_mhz: int     = 0
    memory_reservation_mb: int   = 0

    # OS
    guest_id: str         = ""           # "rhel8_64Guest"
    guest_full_name: str  = ""           # "Red Hat Enterprise Linux 8 (64-bit)"
    hostname: str         = ""
    vmware_tools_status: str = ""        # vmToolsOk / vmToolsNotInstalled / …

    # State
    power_state: PowerState = PowerState.STOPPED
    has_snapshots: bool     = False

    # Hardware
    disks: List[VMwareDisk]         = field(default_factory=list)
    nics: List[VMwareNIC]           = field(default_factory=list)
    snapshots: List[VMwareSnapshot] = field(default_factory=list)
    total_disk_gb: float            = 0.0

    # vSphere location
    datacenter: str    = ""
    cluster: str       = ""
    host_fqdn: str     = ""
    datastore: str     = ""
    resource_pool: str = ""
    folder: str        = ""

    # Metadata
    annotation: str                       = ""
    tags: Dict[str, str]                  = field(default_factory=dict)
    custom_attributes: Dict[str, str]     = field(default_factory=dict)

    # Performance baseline (collected by Extractor)
    performance: Optional[VMwarePerformanceMetrics] = None

    # Extraction metadata
    extracted_at: Optional[datetime]  = None
    extractor_version: str            = "2.0.0"

    @property
    def total_vcpus(self) -> int:
        return self.num_cpus * self.num_cores_per_socket

    @property
    def memory_gb(self) -> float:
        return round(self.memory_mb / 1024, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mor_id":       self.mor_id,
            "name":         self.name,
            "uuid":         self.instance_uuid,
            "vcpus":        self.total_vcpus,
            "memory_mb":    self.memory_mb,
            "guest_os":     self.guest_full_name,
            "power_state":  self.power_state.value,
            "disks":        [{"label": d.label, "size_gb": d.size_gb, "path": d.vmdk_path} for d in self.disks],
            "nics":         [{"label": n.label, "network": n.network_name, "mac": n.mac_address} for n in self.nics],
            "extracted_at": self.extracted_at.isoformat() if self.extracted_at else None,
        }


# ════════════════════════════════════════════════════════════════
# CONVERSION MODELS
# ════════════════════════════════════════════════════════════════

@dataclass
class ConvertedDisk:
    """Disk artifact produced after VMDK → QCOW2 conversion."""
    source_disk: VMwareDisk
    qcow2_path: str               = ""
    original_size_gb: float       = 0.0
    actual_size_gb: float         = 0.0   # Effective size after compression
    conversion_duration_s: float  = 0.0
    checksum_sha256: str          = ""
    verified: bool                = False
    format: DiskFormat            = DiskFormat.QCOW2


@dataclass
class NetworkMapping:
    """
    Maps a VMware portgroup to an OpenStack Neutron network.
    Resolved by the adapter per target environment.
    """
    vmware_network_name: str
    vmware_vlan_id: Optional[int]    = None
    openstack_network_id: str        = ""
    openstack_network_name: str      = ""
    openstack_subnet_id: str         = ""
    openstack_port_id: str           = ""
    ip_address: Optional[str]        = None
    security_groups: List[str]       = field(default_factory=list)


@dataclass
class OpenStackFlavorSpec:
    """Flavor definition — existing or to be created on the target."""
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int
    ephemeral_gb: int          = 0
    is_existing: bool          = True
    flavor_id: str             = ""
    extra_specs: Dict[str, str] = field(default_factory=dict)  # hw:cpu_policy=dedicated…


@dataclass
class ConversionResult:
    """
    Complete output of the Converter module.
    Consumed by Deployer and its target-specific adapter.
    """
    source_vm: VMwareVM
    converted_disks: List[ConvertedDisk]    = field(default_factory=list)
    network_mappings: List[NetworkMapping]  = field(default_factory=list)
    flavor_spec: Optional[OpenStackFlavorSpec] = None
    user_data: str                          = ""   # cloud-init script
    metadata: Dict[str, Any]               = field(default_factory=dict)
    warnings: List[str]                    = field(default_factory=list)
    incompatibilities: List[str]           = field(default_factory=list)
    converted_at: Optional[datetime]       = None

    @property
    def has_blockers(self) -> bool:
        return len(self.incompatibilities) > 0


# ════════════════════════════════════════════════════════════════
# OPENSTACK TARGET MODELS
# ════════════════════════════════════════════════════════════════

@dataclass
class OpenStackVolume:
    volume_id: str
    name: str
    size_gb: int
    volume_type: str = "standard"
    status: str      = ""
    bootable: bool   = False


@dataclass
class OpenStackInstance:
    """OpenStack instance created after migration."""
    instance_id: str
    name: str
    target: OpenStackTarget
    flavor_name: str
    image_id: str                         = ""
    status: str                           = ""
    ip_addresses: Dict[str, List[str]]    = field(default_factory=dict)
    volumes: List[OpenStackVolume]        = field(default_factory=list)
    security_groups: List[str]            = field(default_factory=list)
    created_at: Optional[datetime]        = None
    availability_zone: str                = ""
    hypervisor_hostname: str              = ""
    extended_properties: Dict[str, Any]   = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
# PERFORMANCE & OPTIMIZATION MODELS
# ════════════════════════════════════════════════════════════════

@dataclass
class BenchmarkResult:
    """Single benchmark measurement."""
    tool: str           # sysbench / fio / iperf3
    test_name: str      # cpu_prime / seq_read / tcp_bandwidth
    metric_name: str    # events_per_sec / bandwidth_mbps / latency_ms
    value: float
    unit: str
    environment: str              = ""   # "vmware" | "openstack"
    duration_s: float             = 0.0
    timestamp: Optional[datetime] = None
    raw_output: str               = ""


@dataclass
class PerformanceComparison:
    """Delta between VMware baseline and OpenStack result for one metric."""
    metric_name: str
    vmware_value: float
    openstack_value: float
    unit: str
    delta_percent: float    = 0.0
    is_regression: bool     = False
    threshold_pct: float    = 15.0

    def __post_init__(self):
        if self.vmware_value > 0:
            self.delta_percent = round(
                ((self.openstack_value - self.vmware_value) / self.vmware_value) * 100, 2
            )
            self.is_regression = self.delta_percent < -self.threshold_pct


@dataclass
class PerformanceReport:
    """Full comparative performance report — produced by Evaluator."""
    job_id: str
    vm_name: str
    instance_id: str
    target: OpenStackTarget

    vmware_benchmarks: List[BenchmarkResult]       = field(default_factory=list)
    openstack_benchmarks: List[BenchmarkResult]    = field(default_factory=list)
    comparisons: List[PerformanceComparison]       = field(default_factory=list)
    overall_score: float                           = 0.0   # 0–100
    regressions: List[str]                         = field(default_factory=list)
    recommendations: List[str]                     = field(default_factory=list)
    evaluated_at: Optional[datetime]               = None

    @property
    def passed(self) -> bool:
        return len(self.regressions) == 0


@dataclass
class OptimizationRecommendation:
    resource: str           # "vcpu" / "ram" / "disk_type" / "flavor"
    current_value: Any
    recommended_value: Any
    reason: str
    estimated_saving_percent: float = 0.0
    priority: str                   = "medium"  # low / medium / high


# ════════════════════════════════════════════════════════════════
# AUDIT — enterprise compliance
# ════════════════════════════════════════════════════════════════

@dataclass
class AuditEntry:
    """Immutable audit trail entry — append-only, never modified after creation."""
    entry_id: str                   = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str                     = ""
    timestamp: datetime             = field(default_factory=datetime.now)
    level: LogLevel                 = LogLevel.INFO
    module: str                     = ""
    event: str                      = ""
    detail: str                     = ""
    operator: str                   = "system"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id":  self.entry_id,
            "job_id":    self.job_id,
            "timestamp": self.timestamp.isoformat(),
            "level":     self.level.value,
            "module":    self.module,
            "event":     self.event,
            "detail":    self.detail,
            "operator":  self.operator,
        }


# ════════════════════════════════════════════════════════════════
# MIGRATION JOB — central pipeline entity
# ════════════════════════════════════════════════════════════════

@dataclass
class MigrationJob:
    """
    Represents the full lifecycle of a single VM migration.
    Flows through all 5 pipeline phases.
    Designed for enterprise audit: every state change is recorded.
    """
    job_id: str        = field(default_factory=lambda: str(uuid.uuid4()))
    vm_name: str       = ""
    target: OpenStackTarget    = OpenStackTarget.CUSTOM
    mode: MigrationMode        = MigrationMode.COLD
    status: MigrationStatus    = MigrationStatus.PENDING
    operator: str              = "system"

    # Phase outputs — populated progressively
    source_vm: Optional[VMwareVM]                          = None
    conversion: Optional[ConversionResult]                 = None
    instance: Optional[OpenStackInstance]                  = None
    performance_report: Optional[PerformanceReport]        = None
    optimization_recommendations: List[OptimizationRecommendation] = field(default_factory=list)

    # Timing
    created_at: datetime               = field(default_factory=datetime.now)
    started_at: Optional[datetime]     = None
    completed_at: Optional[datetime]   = None

    # Audit trail — append-only
    audit_log: List[AuditEntry]        = field(default_factory=list)

    # ── Convenience ──────────────────────────────────────────────

    def log(
        self,
        event: str,
        detail: str = "",
        level: LogLevel = LogLevel.INFO,
        module: str = "",
    ) -> None:
        self.audit_log.append(AuditEntry(
            job_id=self.job_id,
            level=level,
            module=module,
            event=event,
            detail=detail,
            operator=self.operator,
        ))

    def log_error(self, event: str, detail: str = "", module: str = "") -> None:
        self.log(event, detail, LogLevel.ERROR, module)
        self.status = MigrationStatus.FAILED

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            MigrationStatus.COMPLETED,
            MigrationStatus.FAILED,
            MigrationStatus.ROLLED_BACK,
            MigrationStatus.CANCELLED,
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "job_id":      self.job_id,
            "vm_name":     self.vm_name,
            "target":      self.target.value,
            "mode":        self.mode.value,
            "status":      self.status.value,
            "operator":    self.operator,
            "created_at":  self.created_at.isoformat(),
            "duration_s":  self.duration_seconds,
            "instance_id": self.instance.instance_id if self.instance else None,
            "perf_score":  self.performance_report.overall_score if self.performance_report else None,
            "audit_count": len(self.audit_log),
        }
