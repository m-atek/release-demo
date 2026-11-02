import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))  # this demo's check, reset and connect before the ones in scripts/
sys.path.append(str(HERE.parents[1]))  # scripts/

# These first: on_host imports k3s, which puts scripts/ at the front of the path.
import check  # noqa: E402, F401
import connect  # noqa: E402, F401
import demo  # noqa: E402, F401
import remote  # noqa: E402, F401
import reset  # noqa: E402, F401
import who  # noqa: E402, F401
import on_host  # noqa: E402, F401

# Pod Security Standards, Baseline: what a pod may not do. The capabilities it may add.
BASELINE_CAPABILITIES = {"AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE",
                         "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"}
SAFE_SYSCTLS = {"kernel.shm_rmid_forced", "net.ipv4.ip_local_port_range", "net.ipv4.ip_unprivileged_port_start",
                "net.ipv4.tcp_syncookies", "net.ipv4.ping_group_range", "net.ipv4.ip_local_reserved_ports",
                "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_fin_timeout", "net.ipv4.tcp_keepalive_intvl",
                "net.ipv4.tcp_keepalive_probes"}


def baseline_violations(spec):
    """Why a pod spec wouldn't pass Pod Security baseline, by the standard's own list."""
    found = []
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field):
            found.append(field)
    found += [f"hostPath volume {volume['name']}" for volume in spec.get("volumes") or [] if "hostPath" in volume]
    security = spec.get("securityContext") or {}
    found += [f"sysctl {s['name']}" for s in security.get("sysctls") or [] if s["name"] not in SAFE_SYSCTLS]
    contexts = [security]
    containers = [*spec.get("containers", []), *(spec.get("initContainers") or []), *(spec.get("ephemeralContainers") or [])]
    for container in containers:
        context = container.get("securityContext") or {}
        contexts.append(context)
        if context.get("privileged"):
            found.append(f"privileged {container['name']}")
        added = set((context.get("capabilities") or {}).get("add") or [])
        if added - BASELINE_CAPABILITIES:
            found.append(f"capabilities {sorted(added - BASELINE_CAPABILITIES)}")
        found += [f"host port on {container['name']}" for port in container.get("ports") or [] if port.get("hostPort")]
        if context.get("procMount", "Default") != "Default":
            found.append(f"procMount on {container['name']}")
    for context in contexts:
        if (context.get("windowsOptions") or {}).get("hostProcess"):
            found.append("hostProcess")
        if (context.get("seccompProfile") or {}).get("type") == "Unconfined":
            found.append("seccomp Unconfined")
        if (context.get("appArmorProfile") or {}).get("type") == "Unconfined":
            found.append("AppArmor Unconfined")
        selinux = context.get("seLinuxOptions") or {}
        if selinux.get("user") or selinux.get("role"):
            found.append("SELinux user or role")
    return found
