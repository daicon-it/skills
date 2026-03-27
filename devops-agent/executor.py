"""SSH command execution abstraction for different machine types."""

import subprocess
import shlex
from dataclasses import dataclass


@dataclass
class CmdResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class Executor:
    def run(self, machine: dict, command: str, timeout: int = 15) -> CmdResult:
        mtype = machine["type"]
        try:
            if mtype == "local":
                return self._run_local(command, timeout)
            elif mtype == "lxc":
                return self._run_lxc(machine, command, timeout)
            elif mtype in ("vps", "proxmox-host"):
                return self._run_ssh(machine, command, timeout)
            else:
                return CmdResult("", f"Unknown type: {mtype}", 1)
        except subprocess.TimeoutExpired:
            return CmdResult("", "Command timed out", 1, timed_out=True)
        except Exception as e:
            return CmdResult("", str(e), 1)

    def _run_local(self, command: str, timeout: int) -> CmdResult:
        r = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=timeout
        )
        return CmdResult(r.stdout, r.stderr, r.returncode)

    def _run_lxc(self, machine: dict, command: str, timeout: int) -> CmdResult:
        host = machine["ssh_host"]
        vmid = machine["vmid"]
        escaped = command.replace("'", "'\\''")
        full_cmd = f"ssh root@{host} \"pct exec {vmid} -- bash -c '{escaped}'\""
        r = subprocess.run(
            full_cmd, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return CmdResult(r.stdout, r.stderr, r.returncode)

    def _run_ssh(self, machine: dict, command: str, timeout: int) -> CmdResult:
        ssh = machine["ssh"]
        full_cmd = f'{ssh} "{command}"'
        r = subprocess.run(
            full_cmd, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return CmdResult(r.stdout, r.stderr, r.returncode)
