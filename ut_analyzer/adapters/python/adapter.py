"""Python language adapter (Tier 1), based on the stdlib ast module."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.detection.framework_hints import detect_python_framework_from_project
from ...core.detection.test_content_sampler import sample_test_contents
from ...models import EnvironmentInfo, ScannedFile, TestCase, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command, tool_available
from ..base import LanguageAdapter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

_ASSERTION_RE = re.compile(
    r"(^\s*assert\b|self\.assert\w+\(|self\.fail\w*\(|pytest\.raises|assertRaises)", re.MULTILINE
)

_MOCK_MARKERS = {
    "unittest.mock": r"\bfrom unittest import mock\b|\bfrom unittest\.mock import\b|\bimport unittest\.mock\b|\bmock\.patch\b|@patch\b",
    "pytest-mock": r"\bmocker\b",
    "MagicMock": r"\bMagicMock\b",
}


class PythonAdapter(LanguageAdapter):
    language_id = "python"
    display_name = "Python"
    source_extensions = (".py",)

    # ---- detection -------------------------------------------------------

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        for marker in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"):
            if (root / marker).exists():
                score += 25
        score += sum(1 for f in files if f.path.suffix == ".py")
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)

        pyproject = read_text_safe(root / "pyproject.toml") or ""
        if (root / "pyproject.toml").exists():
            env.build_system = "pyproject.toml"
            env.build_command = f'"{sys.executable}" -m pip install -e .'
        elif (root / "setup.py").exists():
            env.build_system = "setuptools"
            env.build_command = f'"{sys.executable}" -m pip install -e .'

        test_contents = sample_test_contents(
            files, self.language_id, scan_root=root, test_dir=test_dir
        )
        uses_pytest = (
            "pytest" in pyproject
            or (root / "pytest.ini").exists()
            or (root / "conftest.py").exists()
            or any("import pytest" in c or "pytest.raises" in c for c in test_contents)
        )
        uses_unittest = any(
            re.search(r"\bimport unittest\b|\bunittest\.TestCase\b", c) for c in test_contents
        )
        if uses_pytest:
            env.test_framework = "pytest"
            env.test_command = f'"{sys.executable}" -m pytest'
        elif uses_unittest:
            env.test_framework = "unittest"
            env.test_command = f'"{sys.executable}" -m unittest discover'
        if env.test_framework is None:
            env.test_framework = detect_python_framework_from_project(root)
            if env.test_framework == "pytest" and env.test_command is None:
                env.test_command = f'"{sys.executable}" -m pytest'
            elif env.test_framework == "unittest" and env.test_command is None:
                env.test_command = f'"{sys.executable}" -m unittest discover'
        if env.test_framework is None and test_contents:
            env.notes.append("Test files found but the framework could not be identified.")

        for name, marker in _MOCK_MARKERS.items():
            if any(re.search(marker, c) for c in test_contents):
                env.mocking_libraries.append(name)
        return env

    # ---- test discovery --------------------------------------------------

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        if path.suffix != ".py":
            return False
        name = path.stem
        if (
            name.startswith("test_")
            or name.endswith(("_test", "UT"))
            or name == "conftest"
        ):
            return name != "conftest"
        if content is not None and re.search(
            r"\bclass\s+\w*Test\w*\s*\(\s*(unittest\.)?TestCase", content
        ):
            return True
        return False

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        framework = None
        if "pytest" in content:
            framework = "pytest"
        elif "unittest" in content:
            framework = "unittest"

        cases: list[TestCase] = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return TestFile(path=path, language=self.language_id, framework=framework, content=content)

        lines = content.splitlines()

        def body_of(node: ast.AST) -> str:
            start = getattr(node, "lineno", 1) - 1
            end = getattr(node, "end_lineno", start + 1)
            return "\n".join(lines[start:end])

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                body = body_of(node)
                cases.append(
                    TestCase(
                        name=node.name,
                        suite=None,
                        file=path,
                        line=node.lineno,
                        body=body,
                        assertion_count=len(_ASSERTION_RE.findall(body)),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        body = body_of(item)
                        cases.append(
                            TestCase(
                                name=item.name,
                                suite=node.name,
                                file=path,
                                line=item.lineno,
                                body=body,
                                assertion_count=len(_ASSERTION_RE.findall(body)),
                            )
                        )
        return TestFile(
            path=path, language=self.language_id, framework=framework, cases=cases, content=content
        )

    # ---- generation support ----------------------------------------------

    def test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        stem = source.stem
        pattern = conventions_file_naming or "test_<name>.py"
        if "<name>" in pattern:
            return pattern.replace("<name>", stem)
        return f"test_{stem}.py"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        module = source.stem
        if template is not None and template.preamble:
            lines = list(template.preamble)
        else:
            lines = []
            if env.test_framework == "pytest":
                lines.append("import pytest")
            elif env.test_framework == "unittest":
                lines.append("import unittest")
        if not any(module in line for line in lines):
            lines.append(f"from {module} import *  # adjust import to the project layout")
        return "\n".join(lines) + "\n"

    # ---- validation ----------------------------------------------------------

    def check_syntax(self, code: str) -> tuple[bool, str]:
        try:
            ast.parse(code)
            return True, ""
        except SyntaxError as exc:
            return False, f"syntax error at line {exc.lineno}: {exc.msg}"

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        # Python has no compile step; byte-compile the file as a sanity check.
        return run_command(
            [sys.executable, "-m", "py_compile", str(test_file)], cwd=root, timeout=60
        )

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        if env.test_framework == "pytest" or tool_available("pytest"):
            return run_command(
                [sys.executable, "-m", "pytest", f"{test_file}", "-k", test_name, "-x", "-q"],
                cwd=root,
                timeout=300,
            )
        if env.test_framework == "unittest":
            return run_command(
                [sys.executable, "-m", "unittest", str(test_file)], cwd=root, timeout=300
            )
        return None

    # ---- metrics ----------------------------------------------------------------

    def count_assertions(self, test_body: str) -> int:
        return len(_ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    names.append(node.name)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not item.name.startswith("_"):
                            names.append(f"{node.name}.{item.name}")
        return names
