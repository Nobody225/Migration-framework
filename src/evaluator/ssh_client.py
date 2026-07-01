"""
src/evaluator/ssh_client.py
────────────────────────────
SSH client wrapper for remote benchmark execution.

Connects to VMs (VMware source or OpenStack instance) via Paramiko
and executes benchmark commands remotely.

Supports:
  - Password authentication
  - SSH key authentication
  - Command execution with timeout
  - File upload (for benchmark scripts)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import paramiko

logger = logging.getLogger("migration.evaluator.ssh")


class SSHClientError(Exception):
    """Raised when SSH connection or command execution fails."""
    pass


class SSHClient:
    """
    Thin wrapper around Paramiko for remote command execution.

    Usage:
        client = SSHClient("192.168.1.10", "root", key_path="~/.ssh/id_rsa")
        client.connect()
        stdout, stderr, rc = client.run("sysbench cpu run")
        client.disconnect()

    Or as context manager:
        with SSHClient("192.168.1.10", "root", key_path="~/.ssh/id_rsa") as c:
            stdout, stderr, rc = c.run("uname -a")
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        port: int = 22,
        timeout: int = 30,
        connect_timeout: int = 60,
    ):
        self.host            = host
        self.username        = username
        self.password        = password
        self.key_path        = key_path
        self.port            = port
        self.timeout         = timeout
        self.connect_timeout = connect_timeout

        self._client: Optional[paramiko.SSHClient] = None

    # ── Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Establish SSH connection.
        Tries key auth first, falls back to password if provided.
        """
        try:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: Dict[str, Any] = {
                "hostname":  self.host,
                "port":      self.port,
                "username":  self.username,
                "timeout":   self.connect_timeout,
                "banner_timeout": 60,
                "auth_timeout":   60,
            }

            if self.key_path:
                import os
                key_path = os.path.expanduser(self.key_path)
                connect_kwargs["key_filename"] = key_path
            elif self.password:
                connect_kwargs["password"] = self.password
            else:
                # Try agent / default keys
                connect_kwargs["look_for_keys"] = True
                connect_kwargs["allow_agent"]   = True

            self._client.connect(**connect_kwargs)
            logger.info(f"SSH connected: {self.username}@{self.host}:{self.port}")
            return True

        except paramiko.AuthenticationException as exc:
            raise SSHClientError(f"Authentication failed for {self.username}@{self.host}: {exc}")
        except paramiko.NoValidConnectionsError as exc:
            raise SSHClientError(f"Cannot connect to {self.host}:{self.port}: {exc}")
        except Exception as exc:
            raise SSHClientError(f"SSH connection error ({self.host}): {exc}")

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    # ── Command execution ────────────────────────────────────────

    def run(
        self,
        command: str,
        timeout: Optional[int] = None,
        raise_on_error: bool = False,
    ) -> Tuple[str, str, int]:
        """
        Execute a command on the remote host.

        Returns:
            (stdout, stderr, return_code)

        Args:
            command:        Shell command to execute
            timeout:        Override default timeout (seconds)
            raise_on_error: Raise SSHClientError if return_code != 0
        """
        if not self._client:
            raise SSHClientError("Not connected — call connect() first")

        cmd_timeout = timeout or self.timeout
        logger.debug(f"SSH run [{self.host}]: {command[:80]}...")

        try:
            stdin, stdout, stderr = self._client.exec_command(
                command, timeout=cmd_timeout
            )
            stdout_str = stdout.read().decode("utf-8", errors="replace").strip()
            stderr_str = stderr.read().decode("utf-8", errors="replace").strip()
            rc         = stdout.channel.recv_exit_status()

            if raise_on_error and rc != 0:
                raise SSHClientError(
                    f"Command failed (rc={rc}) on {self.host}: {command}\n"
                    f"stderr: {stderr_str}"
                )

            return stdout_str, stderr_str, rc

        except paramiko.SSHException as exc:
            raise SSHClientError(f"SSH exec error on {self.host}: {exc}")

    def run_sudo(
        self,
        command: str,
        timeout: Optional[int] = None,
    ) -> Tuple[str, str, int]:
        """Run a command with sudo (assumes passwordless sudo or password set)."""
        sudo_cmd = f"sudo -n {command}"
        stdout, stderr, rc = self.run(sudo_cmd, timeout=timeout)
        if rc == 1 and "sudo" in stderr.lower():
            # Try with password
            if self.password:
                sudo_cmd = f"echo '{self.password}' | sudo -S {command}"
                return self.run(sudo_cmd, timeout=timeout)
        return stdout, stderr, rc

    def wait_for_ssh(self, max_wait_s: int = 300, poll_interval_s: int = 10) -> bool:
        """
        Wait until SSH becomes available on the host.
        Useful after instance boot — OS may take time to start sshd.
        """
        logger.info(f"Waiting for SSH on {self.host} (max {max_wait_s}s)...")
        elapsed = 0
        while elapsed < max_wait_s:
            try:
                self.connect()
                logger.info(f"SSH available on {self.host} after {elapsed}s")
                return True
            except SSHClientError:
                time.sleep(poll_interval_s)
                elapsed += poll_interval_s
        raise SSHClientError(f"SSH not available on {self.host} after {max_wait_s}s")

    def check_tool(self, tool: str) -> bool:
        """Check if a tool is installed on the remote host."""
        _, _, rc = self.run(f"which {tool}")
        return rc == 0

    def install_tool(self, tool: str) -> bool:
        """Attempt to install a missing benchmark tool via package manager."""
        # Detect package manager
        for pm in ["apt-get", "yum", "dnf"]:
            _, _, rc = self.run(f"which {pm}")
            if rc == 0:
                stdout, stderr, rc = self.run_sudo(
                    f"{pm} install -y {tool}", timeout=120
                )
                if rc == 0:
                    logger.info(f"Installed {tool} on {self.host} via {pm}")
                    return True
        logger.warning(f"Could not install {tool} on {self.host}")
        return False

    # ── Context manager ──────────────────────────────────────────

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        return f"<SSHClient {self.username}@{self.host}:{self.port} connected={self.is_connected()}>"
