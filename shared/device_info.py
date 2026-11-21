"""Cheap, non-sensitive description of this laptop for the AR view (shared by reporter + worker).

device_info() -> {"hostname", "os", "os_family", "os_version", "arch", "vendor", "model", "chip"}
Computed once and cached. Never includes serial numbers, UUIDs or user names. Every probe is
best-effort with a short timeout; anything that fails is simply left empty.
"""
from __future__ import annotations

import json
import platform
import subprocess
from functools import lru_cache


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _mac() -> dict:
    d = {"os_family": "macos", "vendor": "Apple", "os_version": platform.mac_ver()[0]}
    d["os"] = f"macOS {d['os_version']}".strip()
    hw = {}
    try:
        items = json.loads(_run(["system_profiler", "-json", "SPHardwareDataType"], 8.0) or "{}")
        hw = (items.get("SPHardwareDataType") or [{}])[0]
    except (ValueError, IndexError, AttributeError):
        pass
    # only these fields are read (no serial_number / platform_UUID / provisioning ids)
    d["model"] = str(hw.get("machine_name") or "") or _run(["sysctl", "-n", "hw.model"])
    d["model_id"] = str(hw.get("machine_model") or "") or _run(["sysctl", "-n", "hw.model"])
    d["chip"] = str(hw.get("chip_type") or hw.get("cpu_type") or "") or _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    return d


def _windows() -> dict:
    ver = platform.version()  # e.g. 10.0.22631
    try:
        build = int(ver.split(".")[2])
    except (IndexError, ValueError):
        build = 0
    name = "Windows 11" if platform.release() == "10" and build >= 22000 else f"Windows {platform.release()}"
    d = {"os_family": "windows", "os": name, "os_version": ver, "chip": platform.processor()}
    ps = _run(["powershell", "-NoProfile", "-Command",
               "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json"], 8.0)
    try:
        cs = json.loads(ps) if ps else {}
        d["vendor"], d["model"] = str(cs.get("Manufacturer") or ""), str(cs.get("Model") or "")
    except (ValueError, AttributeError):
        pass
    return d


def _linux() -> dict:
    rel = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            rel[k] = v.strip().strip('"')
    d = {"os_family": "linux", "os": rel.get("PRETTY_NAME") or "Linux", "os_version": rel.get("VERSION_ID", "")}
    dmi = "/sys/devices/virtual/dmi/id/"
    d["vendor"] = _read(dmi + "sys_vendor")
    product, version = _read(dmi + "product_name"), _read(dmi + "product_version")
    # Lenovo keeps the marketing name ("ThinkPad X1 Carbon Gen 11") in product_version
    d["model"] = version if d["vendor"].upper().startswith("LENOVO") and version and "think" in version.lower() else product
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            d["chip"] = line.split(":", 1)[1].strip()
            break
    return d


@lru_cache(maxsize=1)
def device_info() -> dict:
    system = platform.system()
    try:
        d = _mac() if system == "Darwin" else _windows() if system == "Windows" else _linux()
    except Exception:  # device info is decoration; never break the caller
        d = {"os_family": system.lower(), "os": system}
    d.update(hostname=platform.node(), arch=platform.machine().lower().replace("amd64", "x86_64"))
    keep = ("hostname", "os", "os_family", "os_version", "arch", "vendor", "model", "model_id", "chip")
    return {k: " ".join(str(d.get(k) or "").split())[:80] for k in keep}


if __name__ == "__main__":
    print(json.dumps(device_info(), indent=2))
