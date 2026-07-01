"""
src/evaluator/benchmarks.py
────────────────────────────
Benchmark runners for CPU, memory, disk I/O, and network.

Each runner:
  1. Connects via SSH to the target host
  2. Checks / installs the required tool
  3. Runs the benchmark command
  4. Parses the output into BenchmarkResult objects

Tools used:
  - sysbench  → CPU (prime numbers) + Memory (read/write bandwidth)
  - fio       → Disk I/O (sequential/random read/write, IOPS, latency)
  - iperf3    → Network bandwidth (TCP/UDP)

All runners return List[BenchmarkResult] so results are uniform
regardless of which tool produced them.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.models import BenchmarkResult
from src.evaluator.ssh_client import SSHClient, SSHClientError

logger = logging.getLogger("migration.evaluator.benchmarks")


# ════════════════════════════════════════════════════════════════
# CPU BENCHMARK — sysbench cpu
# ════════════════════════════════════════════════════════════════

class CPUBenchmark:
    """
    Measures CPU performance using sysbench prime number calculation.

    Key metric: events/sec — higher is better.
    Secondary: latency (avg, p95, p99)
    """

    def __init__(self, config: Dict[str, Any]):
        self.threads       = config.get("threads", 4)
        self.max_prime     = config.get("cpu_max_prime", 10000)
        self.duration_s    = config.get("duration_s", 30)

    def run(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        """
        Run sysbench CPU benchmark on the remote host.
        Returns list of BenchmarkResult objects.
        """
        results = []

        if not client.check_tool("sysbench"):
            logger.warning(f"sysbench not found on {client.host}, attempting install...")
            if not client.install_tool("sysbench"):
                logger.error(f"Cannot install sysbench on {client.host}")
                return results

        cmd = (
            f"sysbench cpu "
            f"--threads={self.threads} "
            f"--cpu-max-prime={self.max_prime} "
            f"--time={self.duration_s} "
            f"run"
        )

        logger.info(f"[{environment}] Running CPU benchmark on {client.host}...")
        stdout, stderr, rc = client.run(cmd, timeout=self.duration_s + 30)

        if rc != 0:
            logger.error(f"sysbench cpu failed (rc={rc}): {stderr}")
            return results

        ts = datetime.now()

        # Parse: events per second
        m = re.search(r"events per second:\s+([\d.]+)", stdout)
        if m:
            results.append(BenchmarkResult(
                tool="sysbench",
                test_name="cpu_prime",
                metric_name="events_per_sec",
                value=float(m.group(1)),
                unit="events/sec",
                environment=environment,
                duration_s=self.duration_s,
                timestamp=ts,
                raw_output=stdout,
            ))

        # Parse: average latency
        m = re.search(r"avg:\s+([\d.]+)", stdout)
        if m:
            results.append(BenchmarkResult(
                tool="sysbench",
                test_name="cpu_latency_avg",
                metric_name="latency_ms",
                value=float(m.group(1)),
                unit="ms",
                environment=environment,
                duration_s=self.duration_s,
                timestamp=ts,
            ))

        # Parse: P95 latency
        m = re.search(r"95th percentile:\s+([\d.]+)", stdout)
        if m:
            results.append(BenchmarkResult(
                tool="sysbench",
                test_name="cpu_latency_p95",
                metric_name="latency_ms",
                value=float(m.group(1)),
                unit="ms",
                environment=environment,
                duration_s=self.duration_s,
                timestamp=ts,
            ))

        logger.info(
            f"[{environment}] CPU benchmark done: "
            f"{results[0].value:.1f} events/sec" if results else "no results"
        )
        return results


# ════════════════════════════════════════════════════════════════
# MEMORY BENCHMARK — sysbench memory
# ════════════════════════════════════════════════════════════════

class MemoryBenchmark:
    """
    Measures memory bandwidth using sysbench memory.
    Key metric: transfer speed (MiB/sec) — higher is better.
    """

    def __init__(self, config: Dict[str, Any]):
        self.threads       = config.get("threads", 4)
        self.block_size_kb = config.get("memory_block_size_kb", 1)
        self.total_size_gb = config.get("memory_total_size_gb", 10)
        self.duration_s    = config.get("duration_s", 30)

    def run(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        results = []

        if not client.check_tool("sysbench"):
            return results

        for operation in ["read", "write"]:
            cmd = (
                f"sysbench memory "
                f"--threads={self.threads} "
                f"--memory-block-size={self.block_size_kb}K "
                f"--memory-total-size={self.total_size_gb}G "
                f"--memory-oper={operation} "
                f"--time={self.duration_s} "
                f"run"
            )

            logger.info(f"[{environment}] Memory {operation} benchmark on {client.host}...")
            stdout, stderr, rc = client.run(cmd, timeout=self.duration_s + 30)

            if rc != 0:
                logger.warning(f"sysbench memory {operation} failed: {stderr}")
                continue

            # Parse: MiB/sec transferred
            m = re.search(r"([\d.]+)\s+MiB/sec", stdout)
            if m:
                results.append(BenchmarkResult(
                    tool="sysbench",
                    test_name=f"memory_{operation}",
                    metric_name="bandwidth_mib_sec",
                    value=float(m.group(1)),
                    unit="MiB/sec",
                    environment=environment,
                    duration_s=self.duration_s,
                    timestamp=datetime.now(),
                    raw_output=stdout,
                ))

        return results


# ════════════════════════════════════════════════════════════════
# DISK BENCHMARK — fio
# ════════════════════════════════════════════════════════════════

class DiskBenchmark:
    """
    Measures disk I/O performance using fio.

    Tests:
      - Sequential read/write  → bandwidth (MB/s)
      - Random read/write      → IOPS + latency
    """

    def __init__(self, config: Dict[str, Any]):
        self.runtime_s    = config.get("runtime_s", 30)
        self.block_size   = f"{config.get('block_size_kb', 4)}k"
        self.jobs         = config.get("jobs", 4)
        self.iodepth      = config.get("iodepth", 16)
        self.test_file    = "/tmp/fio_migration_test"

    def run(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        results = []

        if not client.check_tool("fio"):
            logger.warning(f"fio not found on {client.host}, attempting install...")
            if not client.install_tool("fio"):
                return results

        tests = [
            ("seq_read",    "read",       "seqread"),
            ("seq_write",   "write",      "seqwrite"),
            ("rand_read",   "randread",   "randread"),
            ("rand_write",  "randwrite",  "randwrite"),
        ]

        for test_name, rw_mode, fio_name in tests:
            cmd = (
                f"fio --name={fio_name} "
                f"--rw={rw_mode} "
                f"--bs={self.block_size} "
                f"--numjobs={self.jobs} "
                f"--iodepth={self.iodepth} "
                f"--runtime={self.runtime_s} "
                f"--time_based "
                f"--filename={self.test_file} "
                f"--size=1G "
                f"--output-format=terse "
                f"--terse-version=3 "
                f"--group_reporting"
            )

            logger.info(f"[{environment}] Disk {test_name} on {client.host}...")
            stdout, stderr, rc = client.run(cmd, timeout=self.runtime_s + 60)

            if rc != 0:
                logger.warning(f"fio {test_name} failed: {stderr}")
                continue

            ts = datetime.now()
            parsed = self._parse_terse(stdout, test_name, environment, ts)
            results.extend(parsed)

        # Cleanup test file
        client.run(f"rm -f {self.test_file}")
        return results

    def _parse_terse(
        self,
        output: str,
        test_name: str,
        environment: str,
        ts: datetime,
    ) -> List[BenchmarkResult]:
        """
        Parse fio terse v3 output.
        Format: jobname;terse_ver;fio_ver;..;read_bw_kb;read_iops;...;write_bw_kb;write_iops;...
        """
        results = []
        for line in output.splitlines():
            if not line or line.startswith("jobname"):
                continue
            fields = line.split(";")
            if len(fields) < 50:
                continue
            try:
                # Field indices for terse v3:
                # 6  = read BW (KB/s)
                # 7  = read IOPS
                # 8  = read latency (usec)
                # 47 = write BW (KB/s)
                # 48 = write IOPS
                # 49 = write latency (usec)
                read_bw_mbs   = float(fields[6])  / 1024
                read_iops     = float(fields[7])
                read_lat_ms   = float(fields[40]) / 1000  # usec → ms
                write_bw_mbs  = float(fields[47]) / 1024
                write_iops    = float(fields[48])
                write_lat_ms  = float(fields[81]) / 1000

                for metric, value, unit in [
                    (f"{test_name}_read_bw",    read_bw_mbs,  "MB/s"),
                    (f"{test_name}_read_iops",  read_iops,    "IOPS"),
                    (f"{test_name}_read_lat",   read_lat_ms,  "ms"),
                    (f"{test_name}_write_bw",   write_bw_mbs, "MB/s"),
                    (f"{test_name}_write_iops", write_iops,   "IOPS"),
                    (f"{test_name}_write_lat",  write_lat_ms, "ms"),
                ]:
                    if value > 0:
                        results.append(BenchmarkResult(
                            tool="fio",
                            test_name=test_name,
                            metric_name=metric,
                            value=round(value, 2),
                            unit=unit,
                            environment=environment,
                            duration_s=self.runtime_s,
                            timestamp=ts,
                        ))
            except (IndexError, ValueError) as e:
                logger.debug(f"fio parse error: {e}")
                continue

        return results


# ════════════════════════════════════════════════════════════════
# NETWORK BENCHMARK — iperf3
# ════════════════════════════════════════════════════════════════

class NetworkBenchmark:
    """
    Measures network bandwidth using iperf3.
    Requires an iperf3 server running on a separate host.

    Tests: TCP bandwidth, UDP bandwidth + jitter + packet loss.
    """

    def __init__(self, config: Dict[str, Any]):
        self.server      = config.get("server", "")
        self.duration_s  = config.get("duration_s", 10)
        self.streams     = config.get("streams", 4)
        self.server_port = config.get("server_port", 5201)

    def run(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        results = []

        if not self.server:
            logger.warning("iperf3 server not configured — skipping network benchmark")
            return results

        if not client.check_tool("iperf3"):
            if not client.install_tool("iperf3"):
                return results

        # TCP test
        tcp_results = self._run_tcp(client, environment)
        results.extend(tcp_results)

        # UDP test
        udp_results = self._run_udp(client, environment)
        results.extend(udp_results)

        return results

    def _run_tcp(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        cmd = (
            f"iperf3 -c {self.server} -p {self.server_port} "
            f"-t {self.duration_s} -P {self.streams} -J"
        )
        stdout, stderr, rc = client.run(cmd, timeout=self.duration_s + 30)
        if rc != 0:
            logger.warning(f"iperf3 TCP failed: {stderr}")
            return []

        return self._parse_json(stdout, "tcp", environment)

    def _run_udp(self, client: SSHClient, environment: str) -> List[BenchmarkResult]:
        cmd = (
            f"iperf3 -c {self.server} -p {self.server_port} "
            f"-u -b 0 -t {self.duration_s} -J"
        )
        stdout, stderr, rc = client.run(cmd, timeout=self.duration_s + 30)
        if rc != 0:
            logger.warning(f"iperf3 UDP failed: {stderr}")
            return []

        return self._parse_json(stdout, "udp", environment)

    def _parse_json(
        self,
        output: str,
        protocol: str,
        environment: str,
    ) -> List[BenchmarkResult]:
        import json
        results = []
        try:
            data = json.loads(output)
            end  = data.get("end", {})
            ts   = datetime.now()

            if protocol == "tcp":
                sent     = end.get("sum_sent", {})
                received = end.get("sum_received", {})

                bw_sent_mbps = sent.get("bits_per_second", 0) / 1e6
                bw_recv_mbps = received.get("bits_per_second", 0) / 1e6

                results.append(BenchmarkResult(
                    tool="iperf3", test_name="tcp_send",
                    metric_name="bandwidth_mbps",
                    value=round(bw_sent_mbps, 2), unit="Mbps",
                    environment=environment, duration_s=self.duration_s, timestamp=ts,
                ))
                results.append(BenchmarkResult(
                    tool="iperf3", test_name="tcp_receive",
                    metric_name="bandwidth_mbps",
                    value=round(bw_recv_mbps, 2), unit="Mbps",
                    environment=environment, duration_s=self.duration_s, timestamp=ts,
                ))

            elif protocol == "udp":
                udp_sum = end.get("sum", {})
                jitter_ms    = udp_sum.get("jitter_ms", 0)
                lost_pct     = udp_sum.get("lost_percent", 0)
                bw_mbps      = udp_sum.get("bits_per_second", 0) / 1e6

                for name, value, unit in [
                    ("udp_bandwidth",    bw_mbps,   "Mbps"),
                    ("udp_jitter",       jitter_ms, "ms"),
                    ("udp_packet_loss",  lost_pct,  "%"),
                ]:
                    results.append(BenchmarkResult(
                        tool="iperf3", test_name=name,
                        metric_name=name,
                        value=round(value, 3), unit=unit,
                        environment=environment, duration_s=self.duration_s, timestamp=ts,
                    ))

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"iperf3 JSON parse error: {e}")

        return results
