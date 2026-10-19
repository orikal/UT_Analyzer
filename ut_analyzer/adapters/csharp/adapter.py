"""C# / .NET language adapter (Tier 2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.detection.framework_hints import detect_csharp_framework_from_project
from ...core.detection.test_content_sampler import sample_test_contents
from ...models import EnvironmentInfo, ScannedFile, TestCase, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command, tool_available
from ..base import LanguageAdapter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

_TEST_ATTR_RE = re.compile(
    r"\[(?:Fact|Theory|Test|TestMethod|TestCase)[^\]]*\]\s*(?:\[[^\]]*\]\s*)*"
    r"(?:public\s+)?(?:async\s+)?(?:Task|void)\s+(\w+)\s*\(",
    re.DOTALL,
)
_CLASS_RE = re.compile(r"\bclass\s+(\w+)")
_ASSERTION_RE = re.compile(r"\b(Assert\.\w+|\.Should\(\)|Verify\()")


class CSharpAdapter(LanguageAdapter):
    language_id = "csharp"
    display_name = "C#"
    source_extensions = (".cs",)

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        if list(root.glob("*.sln")):
            score += 40
        if list(root.rglob("*.csproj")):
            score += 40
        score += sum(1 for f in files if f.path.suffix == ".cs")
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)
        env.build_system = ".NET SDK"
        if tool_available("dotnet"):
            env.build_command = "dotnet build"
            env.test_command = "dotnet test"
        else:
            env.notes.append(".NET project detected but the dotnet CLI is not on PATH.")

        contents = sample_test_contents(
            files, self.language_id, scan_root=root, test_dir=test_dir
        )
        if any("Xunit" in c or "[Fact]" in c for c in contents):
            env.test_framework = "xUnit"
        elif any("NUnit" in c for c in contents):
            env.test_framework = "NUnit"
        elif any("Microsoft.VisualStudio.TestTools" in c for c in contents):
            env.test_framework = "MSTest"
        if env.test_framework is None:
            env.test_framework = detect_csharp_framework_from_project(root)
        if any("Moq" in c or "new Mock<" in c for c in contents):
            env.mocking_libraries.append("Moq")
        if any("NSubstitute" in c for c in contents):
            env.mocking_libraries.append("NSubstitute")
        return env

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        if path.suffix != ".cs":
            return False
        name_hint = path.stem.endswith(("Test", "Tests", "UT"))
        if content is None:
            return name_hint
        return bool(_TEST_ATTR_RE.search(content))

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        class_match = _CLASS_RE.search(content)
        suite = class_match.group(1) if class_match else None
        framework = None
        if "Xunit" in content or "[Fact]" in content:
            framework = "xUnit"
        elif "NUnit" in content:
            framework = "NUnit"
        cases: list[TestCase] = []
        for match in _TEST_ATTR_RE.finditer(content):
            name = match.group(1)
            brace = content.find("{", match.end())
            body = self._block(content, brace) if brace != -1 else ""
            cases.append(
                TestCase(
                    name=name,
                    suite=suite,
                    file=path,
                    line=content.count("\n", 0, match.start()) + 1,
                    body=body,
                    assertion_count=len(_ASSERTION_RE.findall(body)),
                )
            )
        return TestFile(
            path=path, language=self.language_id, framework=framework, cases=cases, content=content
        )

    @staticmethod
    def _block(content: str, brace: int) -> str:
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
        stem = source.stem
        pattern = conventions_file_naming or "<name>Tests.cs"
        if "<name>" in pattern:
            return pattern.replace("<name>", stem)
        return f"{stem}Tests.cs"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        if template is not None and template.preamble:
            usings = "\n".join(template.preamble)
        else:
            framework = env.test_framework or "xUnit"
            usings = {
                "xUnit": "using Xunit;",
                "NUnit": "using NUnit.Framework;",
                "MSTest": "using Microsoft.VisualStudio.TestTools.UnitTesting;",
            }.get(framework, "using Xunit;")
        class_name = test_path.stem if test_path is not None else f"{source.stem}Tests"
        return f"{usings}\n\npublic class {class_name}\n{{\n}}\n"

    def insertion_point(self, original: str) -> int:
        last_brace = original.rfind("}")
        return last_brace if last_brace != -1 else len(original)

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        if tool_available("dotnet"):
            return run_command("dotnet build --nologo -v q", cwd=root, timeout=900)
        return None

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        if tool_available("dotnet"):
            return run_command(
                f'dotnet test --nologo --filter "FullyQualifiedName~{test_name}"',
                cwd=root,
                timeout=900,
            )
        return None

    def count_assertions(self, test_body: str) -> int:
        return len(_ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        names = re.findall(
            r"\bpublic\s+(?:static\s+|async\s+|virtual\s+)*[\w<>\[\],\s?]+?\s+(\w+)\s*\([^)]*\)",
            content,
        )
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]
