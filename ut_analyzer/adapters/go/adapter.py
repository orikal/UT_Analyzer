"""Go language adapter (Tier 2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import EnvironmentInfo, ScannedFile, TestCase, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command, tool_available
from ..base import LanguageAdapter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

_TEST_FUNC_RE = re.compile(r"^func\s+(Test\w+)\s*\(\s*\w+\s+\*testing\.T\s*\)", re.MULTILINE)
_ASSERTION_RE = re.compile(
    r"\bt\.(Error\w*|Fatal\w*|Fail\w*)\b|\bassert\.\w+\(|\brequire\.\w+\("
)


class GoAdapter(LanguageAdapter):
    language_id = "go"
    display_name = "Go"
    source_extensions = (".go",)

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        if (root / "go.mod").exists():
            score += 60
        score += sum(1 for f in files if f.path.suffix == ".go")
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)
        env.build_system = "go modules" if (root / "go.mod").exists() else None
        if tool_available("go"):
            env.build_command = "go build ./..."
            env.test_command = "go test ./..."
        else:
            env.notes.append("Go project detected but the go tool is not on PATH.")
        env.test_framework = "go testing"
        contents = self._test_contents(files)
        if any("github.com/stretchr/testify" in c for c in contents):
            env.mocking_libraries.append("testify")
        if any("gomock" in c or "go.uber.org/mock" in c for c in contents):
            env.mocking_libraries.append("gomock")
        return env

    def _test_contents(self, files: list[ScannedFile], limit: int = 40) -> list[str]:
        out = []
        for f in files:
            if f.language == self.language_id and f.is_test:
                text = read_text_safe(f.path)
                if text:
                    out.append(text)
                if len(out) >= limit:
                    break
        return out

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        return path.name.endswith("_test.go")

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        cases: list[TestCase] = []
        for match in _TEST_FUNC_RE.finditer(content):
            name = match.group(1)
            body = self._func_body(content, match.end())
            cases.append(
                TestCase(
                    name=name,
                    suite=None,
                    file=path,
                    line=content.count("\n", 0, match.start()) + 1,
                    body=body,
                    assertion_count=len(_ASSERTION_RE.findall(body)),
                )
            )
        return TestFile(
            path=path, language=self.language_id, framework="go testing", cases=cases, content=content
        )

    @staticmethod
    def _func_body(content: str, search_from: int) -> str:
        brace = content.find("{", search_from)
        if brace == -1:
            return ""
        depth = 0
        for i in range(brace, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    return content[brace : i + 1]
        return content[brace:]

    def test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        return f"{source.stem}_test.go"

    def new_test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        # The Go toolchain only compiles files ending in `_test.go` as tests.
        return f"{source.stem}_test.go"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        package = "main"
        content = read_text_safe(source)
        if content:
            match = re.search(r"^package\s+(\w+)", content, re.MULTILINE)
            if match:
                package = match.group(1)
        if template is not None and template.preamble:
            imports = "\n".join(template.preamble)
            if "testing" not in imports:
                imports += '\nimport "testing"'
            return f"package {package}\n\n{imports}\n"
        return f'package {package}\n\nimport "testing"\n'

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        if tool_available("go"):
            return run_command(
                ["go", "vet", "./" + str(test_file.parent.relative_to(root)).replace("\\", "/")]
                if test_file.is_relative_to(root)
                else ["go", "vet", "./..."],
                cwd=root,
                timeout=300,
            )
        return None

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        if not tool_available("go"):
            return None
        pkg = "./..."
        if test_file.is_relative_to(root):
            pkg = "./" + str(test_file.parent.relative_to(root)).replace("\\", "/")
        return run_command(
            ["go", "test", "-run", f"^{test_name}$", pkg], cwd=root, timeout=600
        )

    def count_assertions(self, test_body: str) -> int:
        return len(_ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        names = re.findall(r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)\s*\(", content, re.MULTILINE)
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]
