"""C++ build system detection: CMake, Make, MSBuild."""

from __future__ import annotations

from pathlib import Path

from ...utils.subprocess_runner import tool_available


def find_cmake_build_dir(root: Path) -> Path | None:
    """Locate an already configured CMake build directory."""
    candidates = [
        root / "build",
        root / "out" / "build",
        root / "cmake-build-debug",
        root / "cmake-build-release",
    ]
    try:
        candidates.extend(p for p in root.glob("build*") if p.is_dir())
    except OSError:
        pass
    for candidate in candidates:
        if (candidate / "CMakeCache.txt").exists():
            return candidate
    return None


def detect_build_system(root: Path) -> tuple[str | None, str | None, str | None]:
    """Return (build_system, build_command, test_command) or Nones."""
    if (root / "CMakeLists.txt").exists():
        build_dir = find_cmake_build_dir(root)
        if build_dir is not None:
            rel = build_dir.relative_to(root) if build_dir.is_relative_to(root) else build_dir
            return (
                "CMake",
                f'cmake --build "{rel}"',
                f'ctest --test-dir "{rel}" --output-on-failure',
            )
        if tool_available("cmake"):
            return (
                "CMake (not configured)",
                'cmake -S . -B build && cmake --build build',
                'ctest --test-dir build --output-on-failure',
            )
        return ("CMake", None, None)

    solutions = list(root.glob("*.sln"))
    if solutions or list(root.glob("*.vcxproj")):
        target = solutions[0].name if solutions else ""
        if tool_available("msbuild"):
            return ("MSBuild", f'msbuild {target} /m'.strip(), None)
        return ("MSBuild", None, None)

    if (root / "Makefile").exists() or (root / "makefile").exists():
        if tool_available("make"):
            return ("Make", "make", "make test")
        return ("Make", None, None)

    return (None, None, None)
