"""JavaScript / TypeScript language adapter (Tier 2)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.detection.framework_hints import detect_javascript_framework_from_project
from ...models import EnvironmentInfo, ScannedFile, TestCase, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command, tool_available
from ..base import LanguageAdapter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

_TEST_FILE_RE = re.compile(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$")
_CASE_RE = re.compile(r"\b(?:it|test)\s*\(\s*(['\"`])(.+?)\1", re.DOTALL)
_SUITE_RE = re.compile(r"\bdescribe\s*\(\s*(['\"`])(.+?)\1")
_ASSERTION_RE = re.compile(r"\b(expect\s*\(|assert\.|\.should\b|t\.(is|deepEqual|truthy))")


class JavaScriptAdapter(LanguageAdapter):
    language_id = "javascript"
    display_name = "JavaScript/TypeScript"
    source_extensions = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        if (root / "package.json").exists():
            score += 50
        if (root / "tsconfig.json").exists():
            score += 20
        score += sum(1 for f in files if f.path.suffix in self.source_extensions)
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)
        package_json: dict = {}
        raw = read_text_safe(root / "package.json")
        if raw:
            try:
                package_json = json.loads(raw)
            except json.JSONDecodeError:
                pass

        deps = {
            **package_json.get("dependencies", {}),
            **package_json.get("devDependencies", {}),
        }
        scripts = package_json.get("scripts", {})

        env.build_system = "npm" if (root / "package-lock.json").exists() else None
        if (root / "yarn.lock").exists():
            env.build_system = "yarn"
        elif (root / "pnpm-lock.yaml").exists():
            env.build_system = "pnpm"
        elif env.build_system is None and raw:
            env.build_system = "npm"

        runner = {"npm": "npm run", "yarn": "yarn", "pnpm": "pnpm run"}.get(
            env.build_system or "npm", "npm run"
        )
        if "build" in scripts:
            env.build_command = f"{runner} build"
        if "test" in scripts:
            env.test_command = (
                "npm test" if env.build_system in (None, "npm") else f"{runner} test"
            )

        for framework in ("jest", "vitest", "mocha", "ava", "jasmine"):
            if framework in deps:
                env.test_framework = framework
                break
        if env.test_framework is None:
            env.test_framework = detect_javascript_framework_from_project(root)
        for mock_lib in ("sinon", "testdouble", "jest-mock-extended"):
            if mock_lib in deps:
                env.mocking_libraries.append(mock_lib)
        if env.test_framework == "jest":
            env.mocking_libraries.insert(0, "jest built-in mocks")
        if env.test_framework == "vitest":
            env.mocking_libraries.insert(0, "vitest built-in mocks (vi.*)")
        return env

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        if _TEST_FILE_RE.search(path.name):
            return True
        if path.stem.endswith("UT") and path.suffix in self.source_extensions:
            return True
        parts_lower = [p.lower() for p in path.parts[:-1]]
        if ("__tests__" in parts_lower or "test" in parts_lower or "tests" in parts_lower) and (
            path.suffix in self.source_extensions
        ):
            if content is None:
                return True
            return bool(_CASE_RE.search(content) or _SUITE_RE.search(content))
        return False

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        suites = _SUITE_RE.findall(content)
        suite_name = suites[0][1] if suites else None
        cases: list[TestCase] = []
        for match in _CASE_RE.finditer(content):
            name = match.group(2)
            start = match.start()
            end = self._case_end(content, match.end())
            body = content[start:end]
            cases.append(
                TestCase(
                    name=name,
                    suite=suite_name,
                    file=path,
                    line=content.count("\n", 0, start) + 1,
                    body=body,
                    assertion_count=len(_ASSERTION_RE.findall(body)),
                )
            )
        framework = None
        if "vitest" in content:
            framework = "vitest"
        elif re.search(r"\bjest\b", content):
            framework = "jest"
        return TestFile(
            path=path, language=self.language_id, framework=framework, cases=cases, content=content
        )

    @staticmethod
    def _case_end(content: str, search_from: int) -> int:
        brace = content.find("{", search_from)
        if brace == -1:
            return min(len(content), search_from + 400)
        depth = 0
        for i in range(brace, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        return len(content)

    def test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        stem = source.stem
        ext = source.suffix if source.suffix in self.source_extensions else ".js"
        pattern = conventions_file_naming or f"<name>.test{ext}"
        if "<name>" in pattern:
            return pattern.replace("<name>", stem)
        return f"{stem}.test{ext}"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        rel = f"./{source.stem}"
        if template is not None and template.preamble:
            lines = list(template.preamble)
        else:
            lines = []
            if env.test_framework == "vitest":
                lines.append("import { describe, it, expect, vi } from 'vitest';")
        if not any(source.stem in line for line in lines):
            lines.append(f"// import the unit under test from '{rel}' (adjust to project layout)")
        return "\n".join(lines) + "\n"

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        if test_file.suffix in (".ts", ".tsx") and tool_available("npx"):
            return run_command(
                f'npx tsc --noEmit "{test_file}"', cwd=root, timeout=300
            )
        if tool_available("node") and test_file.suffix in (".js", ".mjs", ".cjs"):
            return run_command(["node", "--check", str(test_file)], cwd=root, timeout=60)
        return None

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        if not tool_available("npx"):
            return None
        safe_name = test_name.replace('"', '\\"')
        if env.test_framework == "jest":
            return run_command(
                f'npx jest "{test_file}" -t "{safe_name}"', cwd=root, timeout=600
            )
        if env.test_framework == "vitest":
            return run_command(
                f'npx vitest run "{test_file}" -t "{safe_name}"', cwd=root, timeout=600
            )
        if env.test_framework == "mocha":
            return run_command(
                f'npx mocha "{test_file}" --grep "{safe_name}"', cwd=root, timeout=600
            )
        return None

    def count_assertions(self, test_body: str) -> int:
        return len(_ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        names = re.findall(
            r"\bexport\s+(?:async\s+)?function\s+(\w+)|\bexport\s+const\s+(\w+)\s*=", content
        )
        flat = [a or b for a, b in names]
        seen: set[str] = set()
        return [n for n in flat if n and not (n in seen or seen.add(n))]
