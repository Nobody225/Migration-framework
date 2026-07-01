"""
tests/unit/test_core.py
────────────────────────
Unit tests for: models, VMConverter, and MigrationJob lifecycle.
No real VMware or OpenStack connections — pure Python.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from src.core.models import (
    MigrationJob, MigrationMode, MigrationStatus,
    OpenStackTarget, VMwareDisk, VMwareNIC,
    VMwareVM, PowerState, DiskType, DiskFormat,
    NetworkAdapterType, VMwarePerformanceMetrics,
    LogLevel,
)
from src.converter.vm_converter import VMConverter


# ════════════════════════════════════════════════════════════════
# FIXTURES
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def minimal_vm() -> VMwareVM:
    """A stopped VM with one disk and one NIC — minimum viable migration input."""
    return VMwareVM(
        mor_id="vm-42",
        name="test-vm-01",
        instance_uuid="uuid-1234",
        num_cpus=2,
        num_cores_per_socket=1,
        memory_mb=4096,
        guest_id="rhel8_64Guest",
        guest_full_name="Red Hat Enterprise Linux 8 (64-bit)",
        power_state=PowerState.STOPPED,
        disks=[
            VMwareDisk(
                label="Hard disk 1",
                disk_type=DiskType.OS,
                size_gb=50.0,
                format=DiskFormat.VMDK,
                vmdk_path="/vmfs/volumes/ds1/test-vm-01/test-vm-01.vmdk",
            )
        ],
        nics=[
            VMwareNIC(
                label="Network adapter 1",
                mac_address="00:50:56:ab:cd:ef",
                network_name="VLAN-100-PROD",
                adapter_type=NetworkAdapterType.VMXNET3,
                vlan_id=100,
            )
        ],
        total_disk_gb=50.0,
    )


@pytest.fixture
def converter() -> VMConverter:
    return VMConverter({
        "qemu_img_path":   "/usr/bin/qemu-img",
        "target_format":   "qcow2",
        "compression":     True,
        "verify_checksum": False,  # disabled in tests
    })


# ════════════════════════════════════════════════════════════════
# MigrationJob
# ════════════════════════════════════════════════════════════════

class TestMigrationJob:

    def test_initial_state(self):
        job = MigrationJob(vm_name="web-01", target=OpenStackTarget.REDHAT)
        assert job.status   == MigrationStatus.PENDING
        assert job.vm_name  == "web-01"
        assert job.target   == OpenStackTarget.REDHAT
        assert job.job_id   != ""
        assert not job.is_terminal

    def test_log_appends_audit_entry(self):
        job = MigrationJob(vm_name="x")
        job.log("test event", "some detail", module="test")
        assert len(job.audit_log) == 1
        entry = job.audit_log[0]
        assert entry.event  == "test event"
        assert entry.detail == "some detail"
        assert entry.module == "test"
        assert entry.level  == LogLevel.INFO

    def test_log_error_sets_failed_status(self):
        job = MigrationJob(vm_name="x")
        job.log_error("something broke", "details here", module="converter")
        assert job.status == MigrationStatus.FAILED
        assert len(job.audit_log) == 1
        assert job.audit_log[0].level == LogLevel.ERROR

    def test_is_terminal_states(self):
        for terminal in [
            MigrationStatus.COMPLETED,
            MigrationStatus.FAILED,
            MigrationStatus.ROLLED_BACK,
            MigrationStatus.CANCELLED,
        ]:
            job = MigrationJob()
            job.status = terminal
            assert job.is_terminal

    def test_duration_none_before_completion(self):
        job = MigrationJob()
        assert job.duration_seconds is None

    def test_summary_structure(self):
        job = MigrationJob(vm_name="srv-01", target=OpenStackTarget.HUAWEI)
        s = job.summary()
        assert s["vm_name"]   == "srv-01"
        assert s["target"]    == "huawei"
        assert s["status"]    == "pending"
        assert "job_id"       in s
        assert "created_at"   in s


# ════════════════════════════════════════════════════════════════
# VMConverter — compatibility analysis
# ════════════════════════════════════════════════════════════════

class TestCompatibilityAnalysis:

    def test_clean_vm_has_no_issues(self, converter, minimal_vm):
        issues = converter.analyze_compatibility(minimal_vm)
        assert issues == []

    def test_powered_on_vm_is_blocker(self, converter, minimal_vm):
        minimal_vm.power_state = PowerState.RUNNING
        issues = converter.analyze_compatibility(minimal_vm)
        blockers = [i for i in issues if i.startswith("[BLOCKER]")]
        assert len(blockers) == 1
        assert "powered on" in blockers[0].lower()

    def test_vm_with_snapshots_is_blocker(self, converter, minimal_vm):
        minimal_vm.has_snapshots = True
        issues = converter.analyze_compatibility(minimal_vm)
        blockers = [i for i in issues if i.startswith("[BLOCKER]")]
        assert any("snapshot" in b.lower() for b in blockers)

    def test_missing_vmdk_path_is_blocker(self, converter, minimal_vm):
        minimal_vm.disks[0].vmdk_path = ""
        issues = converter.analyze_compatibility(minimal_vm)
        assert any("[BLOCKER]" in i and "vmdk path" in i.lower() for i in issues)

    def test_no_disks_is_blocker(self, converter, minimal_vm):
        minimal_vm.disks = []
        issues = converter.analyze_compatibility(minimal_vm)
        assert any("[BLOCKER]" in i for i in issues)

    def test_windows_guest_produces_warning(self, converter, minimal_vm):
        minimal_vm.guest_id = "windows9Server64Guest"
        issues = converter.analyze_compatibility(minimal_vm)
        warnings = [i for i in issues if i.startswith("[WARNING]")]
        assert any("windows" in w.lower() for w in warnings)

    def test_e1000_nic_produces_warning(self, converter, minimal_vm):
        minimal_vm.nics[0].adapter_type = NetworkAdapterType.E1000
        issues = converter.analyze_compatibility(minimal_vm)
        assert any("[WARNING]" in i and "E1000" in i for i in issues)

    def test_cpu_reservation_warning(self, converter, minimal_vm):
        minimal_vm.cpu_reservation_mhz = 2000
        issues = converter.analyze_compatibility(minimal_vm)
        assert any("reservation" in i.lower() and "[WARNING]" in i for i in issues)


# ════════════════════════════════════════════════════════════════
# VMConverter — flavor suggestion
# ════════════════════════════════════════════════════════════════

class TestFlavorSuggestion:

    def test_no_metrics_uses_vm_dimensions(self, converter, minimal_vm):
        minimal_vm.performance = None
        spec = converter.suggest_flavor(minimal_vm)
        assert spec.vcpus   >= minimal_vm.total_vcpus
        assert spec.ram_mb  >= minimal_vm.memory_mb
        assert spec.disk_gb >= minimal_vm.total_disk_gb

    def test_low_usage_suggests_smaller_flavor(self, converter, minimal_vm):
        # VM has 2 vCPU 4GB but only uses 20% CPU and 1GB RAM
        minimal_vm.num_cpus    = 8
        minimal_vm.memory_mb   = 32768
        minimal_vm.performance = VMwarePerformanceMetrics(
            cpu_usage_percent=20.0,
            memory_active_mb=1024.0,
        )
        spec = converter.suggest_flavor(minimal_vm)
        # Should NOT suggest 8 vCPU — usage-based sizing applies
        assert spec.vcpus < 8

    def test_flavor_spec_not_existing(self, converter, minimal_vm):
        spec = converter.suggest_flavor(minimal_vm)
        assert spec.is_existing is False

    def test_large_vm_gets_custom_flavor(self, converter, minimal_vm):
        minimal_vm.num_cpus      = 64
        minimal_vm.memory_mb     = 131072
        minimal_vm.total_disk_gb = 1000.0
        spec = converter.suggest_flavor(minimal_vm)
        # No standard flavor fits — should return a custom spec
        assert "custom" in spec.name.lower()


# ════════════════════════════════════════════════════════════════
# VMConverter — network mapping stubs
# ════════════════════════════════════════════════════════════════

class TestNetworkMappingStubs:

    def test_single_nic_produces_one_mapping(self, converter, minimal_vm):
        mappings = converter.build_network_mappings(minimal_vm)
        assert len(mappings) == 1
        assert mappings[0].vmware_network_name == "VLAN-100-PROD"
        assert mappings[0].vmware_vlan_id == 100
        # Neutron IDs not yet resolved at this stage
        assert mappings[0].openstack_network_id == ""

    def test_deduplicates_same_network(self, converter, minimal_vm):
        # Add a second NIC on the same portgroup
        minimal_vm.nics.append(VMwareNIC(
            label="Network adapter 2",
            network_name="VLAN-100-PROD",  # same network
            vlan_id=100,
        ))
        mappings = converter.build_network_mappings(minimal_vm)
        assert len(mappings) == 1

    def test_multiple_networks_produce_multiple_mappings(self, converter, minimal_vm):
        minimal_vm.nics.append(VMwareNIC(
            label="Network adapter 2",
            network_name="VLAN-200-MGMT",
            vlan_id=200,
        ))
        mappings = converter.build_network_mappings(minimal_vm)
        assert len(mappings) == 2
        names = {m.vmware_network_name for m in mappings}
        assert names == {"VLAN-100-PROD", "VLAN-200-MGMT"}


# ════════════════════════════════════════════════════════════════
# VMConverter — disk conversion (mocked qemu-img)
# ════════════════════════════════════════════════════════════════

class TestDiskConversion:

    def test_converts_disk_successfully(self, converter, minimal_vm, tmp_path):
        disk  = minimal_vm.disks[0]
        # Create a fake source VMDK so the path check passes
        fake_vmdk = tmp_path / "test-vm-01.vmdk"
        fake_vmdk.write_bytes(b"\x00" * 1024)
        disk.vmdk_path = str(fake_vmdk)

        with patch("subprocess.run") as mock_run, \
             patch("os.path.getsize", return_value=40 * 1024 ** 3):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = converter.convert_disk(disk, str(tmp_path), "test-vm-01")

        assert result.qcow2_path.endswith(".qcow2")
        assert result.conversion_duration_s >= 0
        assert result.format == DiskFormat.QCOW2

    def test_raises_on_missing_vmdk(self, converter, minimal_vm, tmp_path):
        minimal_vm.disks[0].vmdk_path = "/nonexistent/path.vmdk"
        with pytest.raises(FileNotFoundError, match="Source VMDK not found"):
            converter.convert_disk(minimal_vm.disks[0], str(tmp_path), "vm")

    def test_raises_on_qemu_img_failure(self, converter, minimal_vm, tmp_path):
        fake_vmdk = tmp_path / "disk.vmdk"
        fake_vmdk.write_bytes(b"\x00")
        minimal_vm.disks[0].vmdk_path = str(fake_vmdk)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="qemu-img: unsupported format")
            with pytest.raises(RuntimeError, match="qemu-img failed"):
                converter.convert_disk(minimal_vm.disks[0], str(tmp_path), "vm")
