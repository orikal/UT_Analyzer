"""Java language adapter (Tier 2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.detection.framework_hints import detect_java_framework_from_project
from ...core.detection.test_content_sampler import sample_test_contents
from ...models import EnvironmentInfo, ScannedFile, TestCase, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command, tool_available
from ..base import LanguageAdapter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

_TEST_METHOD_RE = re.compile(
    r"@(?:Test|ParameterizedTest)\b[^{;]*?\b(?:void|public\s+void)\s+(\w+)\s*\(", re.DOTALL
)
_CLASS_RE = re.compile(r"\bclass\s+(\w+)")
_ASSERTION_RE = re.compile(r"\b(assert\w+|Assertions\.\w+|assertThat|verify)\s*\(")


class JavaAdapter(LanguageAdapter):
    language_id = "java"
    display_name = "Java"
    source_extensions = (".java",)

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        if (root / "pom.xml").exists():
            score += 50
        if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            score += 50
        score += sum(1 for f in files if f.path.suffix == ".java")
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)
        gradle_wrapper = (root / "gradlew.bat").exists() or (root / "gradlew").exists()
        if (root / "pom.xml").exists():
            env.build_system = "Maven"
            mvn = "mvn" if tool_available("mvn") else None
            if mvn:
                env.build_command = "mvn -q compile"
                env.test_command = "mvn test"
            else:
                env.notes.append("Maven project detected but mvn is not on PATH.")
        elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            env.build_system = "Gradle"
            gradle = "gradlew" if gradle_wrapper else ("gradle" if tool_available("gradle") else None)
            if gradle:
                prefix = ".\\gradlew" if gradle == "gradlew" else "gradle"
                env.build_command = f"{prefix} compileJava"
                env.test_command = f"{prefix} test"
            else:
                env.notes.append("Gradle project detected but no wrapper or gradle on PATH.")

        contents = sample_test_contents(
            files, self.language_id, scan_root=root, test_dir=test_dir
        )
        if any("org.junit.jupiter" in c for c in contents):
            env.test_framework = "JUnit 5"
        elif any("org.junit" in c for c in contents):
            env.test_framework = "JUnit 4"
        elif any("org.testng" in c for c in contents):
            env.test_framework = "TestNG"
        if env.test_framework is None:
            env.test_framework = detect_java_framework_from_project(root)
        if any("org.mockito" in c or "Mockito." in c for c in contents):
            env.mocking_libraries.append("Mockito")
        if any("io.mockk" in c for c in contents):
            env.mocking_libraries.append("MockK")
        return env

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        if path.suffix != ".java":
            return False
        in_test_tree = "test" in (p.lower() for p in path.parts[:-1])
        name_hint = path.stem.endswith(("Test", "Tests", "IT", "UT")) or path.stem.startswith(
            "Test"
        )
        if content is None:
            return name_hint or in_test_tree
        return "@Test" in content or "@ParameterizedTest" in content

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        class_match = _CLASS_RE.search(content)
        suite = class_match.group(1) if class_match else None
        framework = "JUnit 5" if "org.junit.jupiter" in content else (
            "JUnit 4" if "org.junit" in content else None
        )
        cases: list[TestCase] = []
        for match in _TEST_METHOD_RE.finditer(content):
            name = match.group(1)
            start = match.start()
            body = self._method_body(content, match.end())
            cases.append(
                TestCase(
                    name=name,
                    suite=suite,
                    file=path,
                    line=content.count("\n", 0, start) + 1,
                    body=body,
                    assertion_count=len(_ASSERTION_RE.findall(body)),
                )
            )
        return TestFile(
            path=path, language=self.language_id, framework=framework, cases=cases, content=content
        )

    @staticmethod
    def _method_body(content: str, search_from: int) -> str:
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
        stem = source.stem
        pattern = conventions_file_naming or "<name>Test.java"
        if "<name>" in pattern:
            return pattern.replace("<name>", stem)
        return f"{stem}Test.java"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        if template is not None and template.preamble:
            imports = "\n".join(template.preamble) + "\n"
        else:
            imports = (
                "import org.junit.jupiter.api.Test;\n"
                "import static org.junit.jupiter.api.Assertions.*;\n"
                if env.test_framework != "JUnit 4"
                else "import org.junit.Test;\nimport static org.junit.Assert.*;\n"
            )
        # Java requires the public class name to match the file name.
        class_name = test_path.stem if test_path is not None else f"{source.stem}Test"
        return f"{imports}\npublic class {class_name} {{\n}}\n"

    def insertion_point(self, original: str) -> int:
        last_brace = original.rfind("}")
        return last_brace if last_brace != -1 else len(original)

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        if env.build_system == "Maven" and tool_available("mvn"):
            return run_command("mvn -q test-compile", cwd=root, timeout=900)
        if env.build_system == "Gradle" and env.build_command:
            return run_command(
                env.build_command.replace("compileJava", "compileTestJava"),
                cwd=root,
                timeout=900,
            )
        return None

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        suite = test_file.stem
        if env.build_system == "Maven" and tool_available("mvn"):
            return run_command(
                f'mvn test -Dtest="{suite}#{test_name}"', cwd=root, timeout=900
            )
        if env.build_system == "Gradle" and env.test_command:
            return run_command(
                f'{env.test_command} --tests "{suite}.{test_name}"', cwd=root, timeout=900
            )
        return None

    def count_assertions(self, test_body: str) -> int:
        return len(_ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        names = re.findall(
            r"\bpublic\s+(?:static\s+)?[\w<>\[\],\s]+?\s+(\w+)\s*\([^)]*\)\s*(?:throws[\w\s,]+)?\{",
            content,
        )
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]
