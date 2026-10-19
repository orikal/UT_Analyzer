"""Thin wrapper around subprocess for running external build/test tools."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.cancellation import OperationCancelled, raise_if_cancelled

_COMMUNICATE_GRACE_SECONDS = 5.0


@dataclass
class CommandResult:
    command: str
    exit_code: int | None       # None when the command could not be started
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    error: str = ""             # launch/timeout error description

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts)


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Kill a process and its children (required on Windows for tool wrappers)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
            creationflags=flags,
        )
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def _communicate_safe(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=_COMMUNICATE_GRACE_SECONDS)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_COMMUNICATE_GRACE_SECONDS)
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            return "", ""


def run_command(
    command: str | list[str],
    cwd: Path | None = None,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CommandResult:
    """Run a command, capturing output.

    Returns a ``CommandResult`` for launch/timeout failures. Raises
    ``OperationCancelled`` when ``cancelled`` reports true while the process
    is running (the child process is terminated first).
    """
    if isinstance(command, str):
        display = command
        args: str | list[str] = command
        shell = True
    else:
        display = shlex.join(command)
        args = command
        shell = False

    start = time.monotonic()
    proc_env = _augmented_env(env)
    try:
        proc = subprocess.Popen(
            args,
            shell=shell,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=proc_env,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(
            command=display,
            exit_code=None,
            duration=time.monotonic() - start,
            error=f"Could not start command: {exc}",
        )

    try:
        while proc.poll() is None:
            raise_if_cancelled(cancelled)
            if time.monotonic() - start > timeout:
                _terminate_process_tree(proc)
                stdout, stderr = _communicate_safe(proc)
                return CommandResult(
                    command=display,
                    exit_code=None,
                    stdout=stdout,
                    stderr=stderr,
                    duration=time.monotonic() - start,
                    error=f"Timed out after {timeout:.0f}s",
                )
            time.sleep(0.1)

        stdout, stderr = _communicate_safe(proc)
        return CommandResult(
            command=display,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration=time.monotonic() - start,
        )
    except OperationCancelled:
        if proc.poll() is None:
            _terminate_process_tree(proc)
            _communicate_safe(proc)
        raise


def _iter_python_scripts_dirs() -> list[Path]:
    """Common pip script install locations, including Windows --user installs."""
    import os
    import sys
    import sysconfig

    dirs: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | str | None) -> None:
        if not path:
            return
        resolved = Path(path).resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        dirs.append(resolved)

    try:
        add(sysconfig.get_path("scripts"))
    except KeyError:
        pass

    for scheme in ("nt_user", "posix_user", "venv"):
        try:
            add(sysconfig.get_path("scripts", scheme=scheme))
        except KeyError:
            pass

    appdata = os.environ.get("APPDATA")
    if appdata:
        add(
            Path(appdata)
            / "Python"
            / f"Python{sys.version_info.major}{sys.version_info.minor}"
            / "Scripts"
        )

    return dirs


def _iter_python_executables() -> list[str]:
    """Python interpreters usable for `python -m <tool>` probes."""
    import shutil
    import sys

    candidates: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        resolved = str(Path(path).resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    if not getattr(sys, "frozen", False):
        add(sys.executable)
    for name in ("python", "python3", "py"):
        add(shutil.which(name))
    return candidates


def _iter_well_known_tool_dirs() -> list[Path]:
    """Common install locations for native CLI tools when they are not on PATH."""
    import os
    import sys

    dirs: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | str | None) -> None:
        if not path:
            return
        resolved = Path(path)
        if resolved in seen or not resolved.is_dir():
            return
        seen.add(resolved)
        dirs.append(resolved)

    if sys.platform == "win32":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if base:
                add(Path(base) / "CMake" / "bin")

        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if not base:
                continue
            vs_root = Path(base) / "Microsoft Visual Studio"
            if not vs_root.is_dir():
                continue
            try:
                for cmake_bin in vs_root.glob(
                    "*/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin"
                ):
                    add(cmake_bin)
            except OSError:
                pass

        for candidate in (
            Path("C:/msys64/mingw64/bin"),
            Path("C:/msys64/ucrt64/bin"),
            Path("C:/MinGW/bin"),
            Path("C:/Program Files/Git/mingw64/bin"),
        ):
            add(candidate)

        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if base:
                add(Path(base) / "LLVM" / "bin")

    return dirs


def _resolve_from_well_known_dirs(tool: str) -> str | None:
    for tools_dir in _iter_well_known_tool_dirs():
        for name in (f"{tool}.exe", tool):
            candidate = tools_dir / name
            if candidate.is_file():
                return str(candidate)
    return None


def _augmented_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess env with well-known tool directories prepended to PATH."""
    import os

    env = dict(os.environ)
    if base:
        env.update(base)

    extra_dirs = [str(path) for path in _iter_well_known_tool_dirs()]
    if not extra_dirs:
        return env

    current = env.get("PATH", "")
    current_parts = current.split(os.pathsep) if current else []
    missing = [path for path in extra_dirs if path not in current_parts]
    if missing:
        env["PATH"] = os.pathsep.join(missing + current_parts)
    return env


def resolve_tool_command(tool: str) -> list[str] | None:
    """Resolve an external tool to a command argv prefix."""
    import shutil

    found = shutil.which(tool)
    if found:
        return [found]

    for scripts_dir in _iter_python_scripts_dirs():
        for name in (f"{tool}.exe", tool):
            candidate = scripts_dir / name
            if candidate.is_file():
                return [str(candidate)]

    well_known = _resolve_from_well_known_dirs(tool)
    if well_known:
        return [well_known]

    if tool == "ctest":
        cmake = _resolve_from_well_known_dirs("cmake")
        if cmake:
            sibling = Path(cmake).with_name("ctest.exe")
            if sibling.is_file():
                return [str(sibling)]

    for python in _iter_python_executables():
        probe = run_command([python, "-m", tool, "--version"], timeout=10)
        if probe.ok:
            return [python, "-m", tool]

    return None


def tool_available(tool: str) -> bool:
    """Check whether an external tool can be launched."""
    return resolve_tool_command(tool) is not None
