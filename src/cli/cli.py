"""
src/cli/cli.py
───────────────
Command-line interface for the migration framework.

Commands:
  migrate     → Migrate one or more VMs
  list-vms    → List available VMs in vSphere
  list-jobs   → List all migration jobs
  status      → Show status of a specific job
  report      → Print performance report for a job
  dry-run     → Simulate migration without touching OpenStack

Usage:
  python -m src.cli.cli migrate --vm web-server-01 --target redhat
  python -m src.cli.cli list-vms
  python -m src.cli.cli status --job-id <uuid>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.core.models import MigrationMode, MigrationStatus, OpenStackTarget
from src.utils.config_loader import load_config
from src.utils.framework_factory import build_framework

console = Console()


# ════════════════════════════════════════════════════════════════
# CLI GROUP
# ════════════════════════════════════════════════════════════════

@click.group()
@click.option(
    "--config", "-c",
    default="config/config.local.yaml",
    help="Path to config file (default: config/config.local.yaml)",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, config: str):
    """
    Migration Framework — VMware → OpenStack

    Orange Group / Telecom IT context.
    Supports RedHat RHOSP, Huawei FusionSphere, Custom OpenStack.
    """
    ctx.ensure_object(dict)

    # Load config — fall back to example config if local not found
    if not os.path.exists(config):
        fallback = "config/config.yaml"
        if os.path.exists(fallback):
            console.print(
                f"[yellow]Warning:[/yellow] '{config}' not found, using '{fallback}'.\n"
                "Create config/config.local.yaml with your real credentials."
            )
            config = fallback
        else:
            console.print(f"[red]Error:[/red] Config file not found: {config}")
            sys.exit(1)

    ctx.obj["config"]     = load_config(config)
    ctx.obj["config_path"] = config


# ════════════════════════════════════════════════════════════════
# migrate
# ════════════════════════════════════════════════════════════════

@cli.command()
@click.option("--vm",      "-v", multiple=True, required=True,  help="VM name(s) to migrate")
@click.option("--target",  "-t", required=True,
              type=click.Choice(["redhat", "huawei", "custom"], case_sensitive=False),
              help="Target OpenStack environment")
@click.option("--mode",    "-m", default="cold",
              type=click.Choice(["cold", "warm"]),
              help="Migration mode (default: cold)")
@click.option("--operator", default=os.environ.get("USER", "cli-user"),
              help="Operator name for audit trail")
@click.option("--dry-run", is_flag=True, default=False,
              help="Simulate migration without creating resources")
@click.option("--skip-eval", is_flag=True, default=False,
              help="Skip performance evaluation phase")
@click.option("--parallel", is_flag=True, default=False,
              help="Migrate multiple VMs in parallel")
@click.pass_context
def migrate(
    ctx: click.Context,
    vm: tuple,
    target: str,
    mode: str,
    operator: str,
    dry_run: bool,
    skip_eval: bool,
    parallel: bool,
):
    """Migrate one or more VMs from VMware to OpenStack."""
    config = ctx.obj["config"]

    if dry_run:
        config["migration"]["dry_run"] = True
        console.print("[yellow]DRY-RUN mode — no resources will be created[/yellow]")

    orchestrator = build_framework(config)

    target_enum = OpenStackTarget(target.lower())
    mode_enum   = MigrationMode(mode.lower())

    requests = [
        {
            "vm_name":          vm_name,
            "target":           target_enum,
            "mode":             mode_enum,
            "operator":         operator,
            "skip_evaluation":  skip_eval,
        }
        for vm_name in vm
    ]

    console.print(
        Panel(
            f"Migrating [bold]{len(vm)}[/bold] VM(s) → [cyan]{target.upper()}[/cyan]\n"
            f"Mode: {mode} | Operator: {operator} | Dry-run: {dry_run}",
            title="Migration started",
        )
    )

    jobs = orchestrator.migrate_batch(requests, parallel=parallel)

    # Print results table
    table = Table(title="Migration Results", box=box.ROUNDED)
    table.add_column("VM Name",    style="cyan")
    table.add_column("Status",     style="bold")
    table.add_column("Instance",   style="green")
    table.add_column("Score",      justify="right")
    table.add_column("Duration",   justify="right")

    all_passed = True
    for job in jobs:
        status_color = {
            "completed":   "green",
            "failed":      "red",
            "rolled_back": "yellow",
        }.get(job.status.value, "white")

        score = (
            f"{job.performance_report.overall_score:.1f}/100"
            if job.performance_report else "—"
        )
        duration = (
            f"{job.duration_seconds:.1f}s"
            if job.duration_seconds else "—"
        )
        instance_id = job.instance.instance_id[:12] + "..." if job.instance else "—"

        table.add_row(
            job.vm_name,
            f"[{status_color}]{job.status.value}[/{status_color}]",
            instance_id,
            score,
            duration,
        )

        if job.status != MigrationStatus.COMPLETED:
            all_passed = False

    console.print(table)

    if not all_passed:
        console.print("[red]One or more migrations failed. Check logs for details.[/red]")
        sys.exit(1)
    else:
        console.print("[green]All migrations completed successfully.[/green]")


# ════════════════════════════════════════════════════════════════
# list-vms
# ════════════════════════════════════════════════════════════════

@cli.command("list-vms")
@click.option("--datacenter", "-d", default=None, help="Filter by datacenter")
@click.option("--powered-off-only", is_flag=True, default=False)
@click.pass_context
def list_vms(ctx: click.Context, datacenter: Optional[str], powered_off_only: bool):
    """List all VMs available in vSphere."""
    config = ctx.obj["config"]

    from src.extractor.vmware_extractor import VMwareExtractor
    extractor = VMwareExtractor(config["vmware"])

    console.print("Connecting to vCenter...", end=" ")
    try:
        extractor.connect()
        console.print("[green]OK[/green]")
    except Exception as e:
        console.print(f"[red]FAILED[/red]: {e}")
        sys.exit(1)

    vms = extractor.list_vms(datacenter)
    extractor.disconnect()

    if powered_off_only:
        vms = [v for v in vms if v.power_state.value == "poweredOff"]

    table = Table(title=f"VMs in vSphere ({len(vms)} found)", box=box.ROUNDED)
    table.add_column("Name",        style="cyan", min_width=20)
    table.add_column("Power",       justify="center")
    table.add_column("vCPU",        justify="right")
    table.add_column("RAM (GB)",    justify="right")
    table.add_column("Disk (GB)",   justify="right")
    table.add_column("OS")
    table.add_column("Snapshots",   justify="center")

    for vm in vms:
        power_str = (
            "[green]ON[/green]" if vm.power_state.value == "poweredOn"
            else "[gray]OFF[/gray]"
        )
        snap_str = "[yellow]YES[/yellow]" if vm.has_snapshots else "—"

        table.add_row(
            vm.name,
            power_str,
            str(vm.total_vcpus),
            f"{vm.memory_gb:.1f}",
            f"{vm.total_disk_gb:.1f}",
            vm.guest_full_name[:30] if vm.guest_full_name else "—",
            snap_str,
        )

    console.print(table)


# ════════════════════════════════════════════════════════════════
# list-jobs
# ════════════════════════════════════════════════════════════════

@cli.command("list-jobs")
@click.option("--status", default=None,
              type=click.Choice([s.value for s in MigrationStatus]))
@click.option("--target", default=None,
              type=click.Choice([t.value for t in OpenStackTarget]))
@click.pass_context
def list_jobs(ctx: click.Context, status: Optional[str], target: Optional[str]):
    """List all migration jobs in the current session."""
    config = ctx.obj["config"]
    orchestrator = build_framework(config)

    status_filter = MigrationStatus(status) if status else None
    target_filter = OpenStackTarget(target) if target else None

    jobs = orchestrator.list_jobs(status=status_filter, target=target_filter)

    if not jobs:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    table = Table(title=f"Migration Jobs ({len(jobs)})", box=box.ROUNDED)
    table.add_column("Job ID",      style="dim", max_width=12)
    table.add_column("VM Name",     style="cyan")
    table.add_column("Target")
    table.add_column("Status",      style="bold")
    table.add_column("Score",       justify="right")
    table.add_column("Created")

    for job in jobs:
        color = {"completed": "green", "failed": "red"}.get(job.status.value, "white")
        table.add_row(
            job.job_id[:8] + "...",
            job.vm_name,
            job.target.value,
            f"[{color}]{job.status.value}[/{color}]",
            f"{job.performance_report.overall_score:.1f}" if job.performance_report else "—",
            job.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)


# ════════════════════════════════════════════════════════════════
# status
# ════════════════════════════════════════════════════════════════

@cli.command()
@click.option("--job-id", "-j", required=True)
@click.option("--show-logs", is_flag=True, default=False)
@click.pass_context
def status(ctx: click.Context, job_id: str, show_logs: bool):
    """Show detailed status of a migration job."""
    config = ctx.obj["config"]
    orchestrator = build_framework(config)

    job = orchestrator.get_job(job_id)
    if not job:
        console.print(f"[red]Job not found: {job_id}[/red]")
        sys.exit(1)

    color = {"completed": "green", "failed": "red", "rolled_back": "yellow"}.get(
        job.status.value, "white"
    )

    info = (
        f"[bold]VM:[/bold] {job.vm_name}\n"
        f"[bold]Target:[/bold] {job.target.value}\n"
        f"[bold]Mode:[/bold] {job.mode.value}\n"
        f"[bold]Status:[/bold] [{color}]{job.status.value}[/{color}]\n"
        f"[bold]Operator:[/bold] {job.operator}\n"
        f"[bold]Created:[/bold] {job.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    if job.duration_seconds:
        info += f"[bold]Duration:[/bold] {job.duration_seconds:.1f}s\n"

    if job.instance:
        info += f"[bold]Instance ID:[/bold] {job.instance.instance_id}\n"

    if job.performance_report:
        info += f"[bold]Perf score:[/bold] {job.performance_report.overall_score:.1f}/100\n"

    console.print(Panel(info, title=f"Job {job_id[:12]}..."))

    if show_logs:
        console.print("\n[bold]Audit log:[/bold]")
        for entry in job.audit_log:
            level_color = {"ERROR": "red", "WARNING": "yellow", "INFO": "white"}.get(
                entry.level.value, "white"
            )
            console.print(
                f"  [{level_color}]{entry.timestamp.strftime('%H:%M:%S')} "
                f"[{entry.module}] {entry.event}[/{level_color}]"
                + (f": {entry.detail}" if entry.detail else "")
            )


# ════════════════════════════════════════════════════════════════
# report
# ════════════════════════════════════════════════════════════════

@cli.command()
@click.option("--job-id", "-j", required=True)
@click.option("--format", "-f", default="table",
              type=click.Choice(["table", "json"]))
@click.pass_context
def report(ctx: click.Context, job_id: str, format: str):
    """Print performance report for a completed migration job."""
    config = ctx.obj["config"]
    orchestrator = build_framework(config)

    job = orchestrator.get_job(job_id)
    if not job:
        console.print(f"[red]Job not found: {job_id}[/red]")
        sys.exit(1)

    perf = job.performance_report
    if not perf:
        console.print("[yellow]No performance report available for this job.[/yellow]")
        return

    if format == "json":
        data = {
            "job_id":    job_id,
            "vm_name":   perf.vm_name,
            "score":     perf.overall_score,
            "passed":    perf.passed,
            "regressions": perf.regressions,
            "recommendations": perf.recommendations,
            "comparisons": [
                {
                    "metric":     c.metric_name,
                    "vmware":     c.vmware_value,
                    "openstack":  c.openstack_value,
                    "unit":       c.unit,
                    "delta_pct":  c.delta_percent,
                    "regression": c.is_regression,
                }
                for c in perf.comparisons
            ],
        }
        console.print(json.dumps(data, indent=2))
        return

    # Table format
    score_color = "green" if perf.overall_score >= 80 else "yellow" if perf.overall_score >= 60 else "red"
    console.print(
        Panel(
            f"[bold]VM:[/bold] {perf.vm_name}\n"
            f"[bold]Score:[/bold] [{score_color}]{perf.overall_score:.1f}/100[/{score_color}]\n"
            f"[bold]Result:[/bold] {'[green]PASS[/green]' if perf.passed else '[red]FAIL[/red]'}\n"
            f"[bold]Regressions:[/bold] {len(perf.regressions)}",
            title="Performance Report",
        )
    )

    if perf.comparisons:
        table = Table(title="Metric Comparisons", box=box.ROUNDED)
        table.add_column("Metric",      style="cyan")
        table.add_column("VMware",      justify="right")
        table.add_column("OpenStack",   justify="right")
        table.add_column("Unit")
        table.add_column("Delta",       justify="right")
        table.add_column("Status",      justify="center")

        for c in perf.comparisons:
            delta_str  = f"{c.delta_percent:+.1f}%"
            status_str = "[red]REGRESSION[/red]" if c.is_regression else "[green]OK[/green]"
            delta_color = "red" if c.is_regression else "green" if c.delta_percent >= 0 else "white"
            table.add_row(
                c.metric_name,
                f"{c.vmware_value:.2f}",
                f"{c.openstack_value:.2f}",
                c.unit,
                f"[{delta_color}]{delta_str}[/{delta_color}]",
                status_str,
            )
        console.print(table)

    if perf.recommendations:
        console.print("\n[bold]Recommendations:[/bold]")
        for rec in perf.recommendations:
            console.print(f"  [yellow]→[/yellow] {rec}")


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cli()
