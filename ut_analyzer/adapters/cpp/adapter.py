"""C++ language adapter (Tier 1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.detection.framework_hints import detect_cpp_framework_from_project
from ...core.detection.test_content_sampler import sample_test_contents
from ...models import EnvironmentInfo, ScannedFile, TestFile
from ...utils.fs import read_text_safe
from ...utils.subprocess_runner import CommandResult, run_command
from ..base import LanguageAdapter
from . import access, build_systems, frameworks

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...core.conventions.test_template import TestTemplate

#: Fallback framework header, used only when the project's own tests reveal none.
_FRAMEWORK_HEADERS = {
    "gtest": '#include "gtest/gtest.h"',
    "googletest": '#include "gtest/gtest.h"',
    "catch2": "#include <catch2/catch_test_macros.hpp>",
    "doctest": "#include <doctest/doctest.h>",
    "boost.test": "#include <boost/test/unit_test.hpp>",
}

_TEST_NAME_HINT = re.compile(r"(^test_|_test$|_tests$|^tests?$|Test[A-Z_]|Tests?\b|UT$)")

_FUNC_DEF_RE = re.compile(
    r"^[ \t]*(?:[\w:<>,~&*\s]+?)\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{",
    re.MULTILINE,
)
_CPP_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "else", "do", "new", "delete",
}

# `class Foo {` / `struct Foo : public Bar {` (definitions, not forward declarations)
_CLASS_DEF_RE = re.compile(
    r"^[ \t]*(?:class|struct)\s+(?:\w+_(?:API|EXPORT)\s+)?([A-Za-z_]\w*)"
    r"\s*(?:final\s*)?(?::[^;{]*)?\{",
    re.MULTILINE,
)
# `void Foo::bar(...)` out-of-line member definitions
_METHOD_DEF_RE = re.compile(r"^[ \t]*[\w:<>,&*\s]+?\b([A-Za-z_]\w*)::[A-Za-z_~]", re.MULTILINE)


@dataclass(frozen=True)
class _LifecyclePlan:
    """Which lifecycle members a new fixture gets, and where their bodies go.

    The default is GoogleTest's own style; it is replaced by whatever the
    project's existing unit tests do as soon as we manage to learn it.
    """

    constructor: bool = False
    destructor: bool = False
    setup: bool = True
    teardown: bool = True
    #: Bodies live in the test source file (``SeekUT::SeekUT() {...}``).
    out_of_line: bool = False
    init_methods: tuple[str, ...] = ()


def _lifecycle_plan(template: TestTemplate | None) -> _LifecyclePlan:
    """The fixture lifecycle learned from the neighbouring tests, if any."""
    lifecycle = template.lifecycle if template is not None else None
    if lifecycle is None or lifecycle.is_empty:
        return _LifecyclePlan()
    plan = _LifecyclePlan(
        constructor=lifecycle.uses_constructor,
        destructor=lifecycle.uses_destructor,
        setup=lifecycle.uses_setup,
        teardown=lifecycle.uses_teardown,
        out_of_line=lifecycle.defines_out_of_line,
        init_methods=lifecycle.init_methods,
    )
    if not (plan.constructor or plan.destructor or plan.setup or plan.teardown):
        return _LifecyclePlan()
    return plan


class CppAdapter(LanguageAdapter):
    language_id = "cpp"
    display_name = "C++"
    source_extensions = (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx")
    header_extensions = (".h", ".hpp", ".hxx")

    # ---- detection -------------------------------------------------------

    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        score = 0
        if (root / "CMakeLists.txt").exists():
            score += 50
        if list(root.glob("*.sln")) or list(root.glob("*.vcxproj")):
            score += 40
        score += sum(
            1 for f in files if f.path.suffix in (".cpp", ".cc", ".cxx")
        )
        return score

    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        env = EnvironmentInfo(language=self.language_id, language_name=self.display_name)

        build_system, build_cmd, test_cmd = build_systems.detect_build_system(root)
        env.build_system = build_system
        env.build_command = build_cmd
        env.test_command = test_cmd
        if build_system and build_cmd is None:
            env.notes.append(
                f"{build_system} project detected but the build tool is not on PATH."
            )

        test_contents = sample_test_contents(
            files, self.language_id, scan_root=root, test_dir=test_dir
        )
        env.test_framework = frameworks.detect_framework(test_contents)
        if env.test_framework is None:
            env.test_framework = detect_cpp_framework_from_project(root)
        env.mocking_libraries = frameworks.detect_mocking(test_contents)
        return env

    # ---- test discovery --------------------------------------------------

    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        if path.suffix not in (".cpp", ".cc", ".cxx"):
            return False
        name_hint = bool(_TEST_NAME_HINT.search(path.stem)) or "test" in [
            p.lower() for p in path.parts[:-1]
        ]
        if content is None:
            return name_hint
        has_macros = bool(
            re.search(r"\b(TEST|TEST_F|TEST_P|TEST_CASE|BOOST_AUTO_TEST_CASE)\s*\(", content)
        )
        return has_macros or (name_hint and "gtest" in content)

    def parse_test_file(self, path: Path, content: str) -> TestFile:
        framework = frameworks.detect_framework([content])
        return TestFile(
            path=path,
            language=self.language_id,
            framework=framework,
            cases=frameworks.parse_cases(path, content),
            content=content,
        )

    # ---- generation support ----------------------------------------------

    def test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        stem = source.stem
        pattern = conventions_file_naming or "<name>_test.cpp"
        if "<name>" in pattern:
            return pattern.replace("<name>", stem)
        return f"{stem}_test.cpp"

    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: TestTemplate | None = None,
    ) -> str:
        ut_stem = test_path.stem if test_path is not None else Path(
            self.new_test_file_name_for(source, None)
        ).stem
        definitions = self._lifecycle_definitions(
            self._fixture_name(source), _lifecycle_plan(template)
        )
        return f'#include "{ut_stem}.h"\n{definitions}'

    def new_test_header(
        self,
        test_path: Path,
        source: Path,
        env: EnvironmentInfo,
        template: TestTemplate | None = None,
    ) -> tuple[Path, str] | None:
        # Named after the UT file itself (seekUT.cpp -> seekUT.h), next to it.
        header_path = test_path.with_suffix(".h")
        guard = re.sub(r"\W", "_", header_path.name).upper()
        class_name = self._class_under_test(source)
        fixture = self._fixture_name(source)
        includes = self._fixture_includes(source, env, template)
        base = (template.fixture_base if template else None) or "::testing::Test"
        content = (
            f"#ifndef {guard}\n"
            f"#define {guard}\n\n"
            f"{includes}\n"
            f"class {fixture} : public {base}\n"
            "{\n"
            "protected:\n"
            f"{self._test_accessor_block(source, class_name)}"
            f"{self._lifecycle_declarations(fixture, _lifecycle_plan(template))}"
            "};\n\n"
            f"#endif  // {guard}\n"
        )
        return header_path, content

    def _fixture_name(self, source: Path) -> str:
        """Name of the fixture class generated for *source*."""
        return f"{self._class_under_test(source) or source.stem}UT"

    def _lifecycle_declarations(self, fixture: str, plan: _LifecyclePlan) -> str:
        """Fixture lifecycle members, declared the way this project declares them."""
        lines: list[str] = []
        if plan.constructor:
            lines.append(f"    {fixture}();\n")
        if plan.destructor:
            lines.append(f"    ~{fixture}();\n")
        for name in plan.init_methods:
            lines.append(f"    void {name}();\n")
        for hook in ("SetUp", "TearDown"):
            if not (plan.setup if hook == "SetUp" else plan.teardown):
                continue
            lines.append(
                f"    void {hook}() override;\n"
                if plan.out_of_line
                else f"    void {hook}() override {{}}\n"
            )
        return "".join(lines)

    def _lifecycle_definitions(self, fixture: str, plan: _LifecyclePlan) -> str:
        """Out-of-line bodies for the members declared by ``_lifecycle_declarations``."""
        blocks: list[str] = []
        if plan.constructor:
            blocks.append(f"{fixture}::{fixture}()\n{{\n}}\n")
        if plan.destructor:
            blocks.append(f"{fixture}::~{fixture}()\n{{\n}}\n")
        for name in plan.init_methods:
            blocks.append(f"void {fixture}::{name}()\n{{\n}}\n")
        if plan.out_of_line:
            for hook in ("SetUp", "TearDown"):
                if plan.setup if hook == "SetUp" else plan.teardown:
                    blocks.append(f"void {fixture}::{hook}()\n{{\n}}\n")
        if not blocks:
            return ""
        return "\n" + "\n".join(blocks)

    def _fixture_includes(
        self, source: Path, env: EnvironmentInfo, template: TestTemplate | None
    ) -> str:
        """Includes for a new fixture header.

        Taken from the project's own test files when available. Only the header
        of the unit under test is added unconditionally - guessing at
        project-specific support headers produces files that do not compile in
        any repository that lacks them.
        """
        lines: list[str] = []
        if template is not None and template.preamble:
            lines = [line for line in template.preamble if line.startswith("#include")]
        if not lines:
            framework_header = _FRAMEWORK_HEADERS.get(
                (env.test_framework or "").lower(), '#include "gtest/gtest.h"'
            )
            lines = [framework_header]
        own = f'#include "{self._source_header_name(source)}"'
        if own not in lines:
            lines.append(own)
        return "".join(f"{line}\n" for line in lines)

    def access_notes(self, source: Path, source_code: str) -> str:
        classes = self._class_access(source, source_code)
        return access.access_prompt_text(classes, self._class_under_test(source))

    def _class_access(
        self, source: Path, source_code: str | None = None
    ) -> list[access.ClassAccess]:
        """Member visibility of the classes declared by *source* and its header."""
        contents: list[str] = []
        if source_code is not None:
            contents.append(source_code)
        else:
            contents.append(read_text_safe(source) or "")
        for extension in self.header_extensions:
            candidate = source.with_suffix(extension)
            if candidate == source or not candidate.is_file():
                continue
            contents.append(read_text_safe(candidate) or "")

        merged: dict[str, access.ClassAccess] = {}
        for content in contents:
            for item in access.analyze_access(content):
                existing = merged.get(item.name)
                merged[item.name] = item if existing is None else existing.merge(item)
        return list(merged.values())

    def _test_accessor_block(self, source: Path, class_name: str | None) -> str:
        """Nested class that legally exposes the protected members to the tests."""
        target = access.find_class(self._class_access(source), class_name)
        if target is None or target.is_final:
            return ""
        members = target.inheritable_protected()
        if not members:
            return ""
        accessor = access.accessor_name(target.name)
        usings = "".join(
            f"        using {target.name}::{member};\n" for member in members
        )
        return (
            f"    // Grants the tests access to {target.name}'s protected members through\n"
            f"    // inheritance; protected members must never be called on a plain\n"
            f"    // {target.name} object. Use {accessor} in those tests.\n"
            f"    class {accessor} : public {target.name}\n"
            "    {\n"
            "    public:\n"
            f"        using {target.name}::{target.name};  // inherit the constructors\n"
            f"{usings}"
            "    };\n\n"
        )

    def _source_header_name(self, source: Path) -> str:
        """Header file that declares *source*'s API (falls back to ``<stem>.h``)."""
        for ext in self.header_extensions:
            candidate = source.with_suffix(ext)
            if candidate.is_file():
                return candidate.name
        return source.with_suffix(".h").name

    def _class_under_test(self, source: Path) -> str | None:
        """Name of the primary class defined by the source file (or its header)."""
        for candidate in (source, *(source.with_suffix(ext) for ext in self.header_extensions)):
            content = read_text_safe(candidate)
            if not content:
                continue
            match = _CLASS_DEF_RE.search(content)
            if match:
                return match.group(1)
            match = _METHOD_DEF_RE.search(content)
            if match:
                return match.group(1)
        return None

    # ---- validation --------------------------------------------------------

    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        if not env.build_command:
            return None
        return run_command(env.build_command, cwd=root, timeout=900)

    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        build_dir = build_systems.find_cmake_build_dir(root)
        if build_dir is not None:
            pattern = re.escape(test_name)
            result = run_command(
                f'ctest --test-dir "{build_dir}" -R "{pattern}" --output-on-failure',
                cwd=root,
                timeout=600,
            )
            if "No tests were found" in result.output and env.test_command:
                # ctest could not match the name; fall back to the whole suite.
                return run_command(env.test_command, cwd=root, timeout=900)
            return result
        if env.test_command:
            return run_command(env.test_command, cwd=root, timeout=900)
        return None

    # ---- metrics -------------------------------------------------------------

    def count_assertions(self, test_body: str) -> int:
        return len(frameworks.ASSERTION_RE.findall(test_body))

    def source_function_names(self, content: str) -> list[str]:
        names: list[str] = []
        for match in _FUNC_DEF_RE.finditer(content):
            name = match.group(1)
            if name not in _CPP_KEYWORDS and not name.startswith("_"):
                names.append(name)
        # de-duplicate, preserve order
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]
