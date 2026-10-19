"""Hardware and environment compatibility checks before AI runtime startup."""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ...utils.paths import program_data_root


@dataclass
class CompatibilityIssue:
    code: str
    message: str
    severity: str  # "error" | "warning"
    can_continue: bool = True
    affected_features: list[str] = field(default_factory=list)


@dataclass
class CompatibilityReport:
    issues: list[CompatibilityIssue] = field(default_factory=list)
    windows_version: str = ""
    architecture: str = ""
    total_ram_gb: float = 0.0
    available_ram_gb: float = 0.0
    available_disk_gb: float = 0.0
    write_permissions_ok: bool = True

    @property
    def ai_blocked(self) -> bool:
        return any(
            i.severity == "error" and not i.can_continue and "ai" in i.affected_features
            for i in self.issues
        )

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "warning" for i in self.issues)


def _get_memory_stats_gb() -> tuple[float, float]:
    """Return (total_physical_gb, available_physical_gb)."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return (
            stat.ullTotalPhys / (1024**3),
            stat.ullAvailPhys / (1024**3),
        )
    except (AttributeError, OSError):
        return 0.0, 0.0


def _get_free_disk_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024**3)
    except OSError:
        return 0.0


def run_compatibility_checks() -> CompatibilityReport:
    total_ram, available_ram = _get_memory_stats_gb()

    report = CompatibilityReport(
        windows_version=platform.platform(),
        architecture=platform.machine(),
        total_ram_gb=total_ram,
        available_ram_gb=available_ram,
        available_disk_gb=_get_free_disk_gb(program_data_root()),
    )

    if platform.system() != "Windows":
        report.issues.append(
            CompatibilityIssue(
                code="platform",
                message="UT Analyzer is designed for Windows.",
                severity="warning",
                can_continue=True,
                affected_features=["ai"],
            )
        )

    try:
        test_file = program_data_root() / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError:
        report.write_permissions_ok = False
        report.issues.append(
            CompatibilityIssue(
                code="write_permissions",
                message=f"Cannot write to application data directory: {program_data_root()}",
                severity="error",
                can_continue=False,
                affected_features=["ai", "logging"],
            )
        )

    return report
