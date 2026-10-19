"""Detect test frameworks from project/build configuration files (fallback only)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...utils.fs import read_text_safe

# Strict patterns — package names only, no generic macros like TEST_CASE.
_CPP_FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "GoogleTest": (
        r"find_package\s*\(\s*GTest\b",
        r"\bGTest::gtest\b",
        r"\bgoogletest\b",
        r"gtest_discover_tests\s*\(",
    ),
    "Catch2": (
        r"find_package\s*\(\s*Catch2\b",
        r"\bCatch2::Catch2\b",
    ),
    "doctest": (
        r"find_package\s*\(\s*doctest\b",
        r"\bdoctest::doctest\b",
    ),
    "Boost.Test": (
        r"find_package\s*\(\s*Boost\b[^\n]*test",
        r"\bBoost::unit_test_framework\b",
    ),
}

_JAVA_FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "JUnit 5": (r"junit-jupiter", r"org\.junit\.jupiter"),
    "JUnit 4": (r"junit:junit\b", r"org\.junit\.(?!jupiter)"),
    "TestNG": (r"testng", r"org\.testng"),
}

_CSHARP_FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "xUnit": (r'PackageReference\s+Include="xunit', r"\bxunit\b"),
    "NUnit": (r'PackageReference\s+Include="NUnit', r"\bnunit\b"),
    "MSTest": (
        r'PackageReference\s+Include="MSTest',
        r"Microsoft\.VisualStudio\.TestTools\.UnitTesting",
    ),
}


def _score_markers(text: str, markers: dict[str, tuple[str, ...]]) -> str | None:
    scores: dict[str, int] = {}
    for name, patterns in markers.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                scores[name] = scores.get(name, 0) + 1
    if not scores:
        return None
    return max(scores, key=lambda key: scores[key])


def _read_config_files(root: Path, names: tuple[str, ...], limit: int = 12) -> list[str]:
    texts: list[str] = []
    for name in names:
        direct = read_text_safe(root / name)
        if direct:
            texts.append(direct)
    if len(texts) >= limit:
        return texts[:limit]
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name not in names:
                continue
            if path.parent == root:
                continue
            text = read_text_safe(path)
            if text:
                texts.append(text)
            if len(texts) >= limit:
                break
    except OSError:
        pass
    return texts


def detect_cpp_framework_from_project(root: Path) -> str | None:
    texts = _read_config_files(
        root,
        ("CMakeLists.txt", "Makefile", "conanfile.txt", "vcpkg.json"),
    )
    scores: dict[str, int] = {}
    for text in texts:
        detected = _score_markers(text, _CPP_FRAMEWORK_MARKERS)
        if detected:
            scores[detected] = scores.get(detected, 0) + 1
    if not scores:
        return None
    return max(scores, key=lambda key: scores[key])


def detect_python_framework_from_project(root: Path) -> str | None:
    pyproject = read_text_safe(root / "pyproject.toml") or ""
    requirements = read_text_safe(root / "requirements.txt") or ""
    setup_cfg = read_text_safe(root / "setup.cfg") or ""
    combined = "\n".join((pyproject, requirements, setup_cfg))

    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return "pytest"
    if re.search(r"\bpytest\b", combined, re.IGNORECASE):
        return "pytest"
    if re.search(r"\bunittest\b", combined, re.IGNORECASE):
        return "unittest"
    return None


def detect_java_framework_from_project(root: Path) -> str | None:
    texts = _read_config_files(
        root,
        ("pom.xml", "build.gradle", "build.gradle.kts"),
    )
    combined = "\n".join(texts)
    if not combined:
        return None
    return _score_markers(combined, _JAVA_FRAMEWORK_MARKERS)


def detect_csharp_framework_from_project(root: Path) -> str | None:
    texts: list[str] = []
    try:
        for path in root.rglob("*.csproj"):
            text = read_text_safe(path)
            if text:
                texts.append(text)
            if len(texts) >= 20:
                break
    except OSError:
        pass
    combined = "\n".join(texts)
    if not combined:
        return None
    return _score_markers(combined, _CSHARP_FRAMEWORK_MARKERS)


def detect_javascript_framework_from_project(root: Path) -> str | None:
    raw = read_text_safe(root / "package.json")
    if not raw:
        return None
    try:
        package_json = json.loads(raw)
    except json.JSONDecodeError:
        return None
    deps = {
        **package_json.get("dependencies", {}),
        **package_json.get("devDependencies", {}),
    }
    for framework in ("jest", "vitest", "mocha", "ava", "jasmine"):
        if framework in deps:
            return framework
    return None
