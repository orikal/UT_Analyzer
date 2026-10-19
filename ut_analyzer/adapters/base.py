"""LanguageAdapter abstract base class and the adapter registry."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import EnvironmentInfo, ScannedFile, TestFile
from ..utils.parsing import check_syntax_tree_sitter
from ..utils.subprocess_runner import CommandResult

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..core.conventions.test_template import TestTemplate

_NEVER_SKIP = (
    "A non-public member is NEVER a reason to skip a test, to test something else "
    "instead, to leave an empty test body or to write a `TODO` saying it is "
    "inaccessible: use the technique above and write the real test."
)

_ACCESS_STRATEGIES: dict[str, tuple[re.Pattern[str], str]] = {
    "python": (
        re.compile(r"(?m)^\s*def\s+_{1,2}\w|^\s*_{1,2}\w+\s*[:=]|self\._{1,2}\w"),
        "Private members in Python (MANDATORY - they are testable):\n"
        "- `_name` (single underscore) is a naming convention only: call it directly, "
        "exactly like a public member.\n"
        "- `__name` (double underscore) is name-mangled, not hidden: reach it as "
        "`obj._<ClassName>__name` (e.g. `obj._Parser__buffer`) or with "
        "`getattr(obj, \"_Parser__parse\")`.\n"
        "- Module-level private helpers: import them explicitly "
        "(`from module import _helper`) or through the module object.\n"
        "- Replace or observe them with `unittest.mock.patch.object(Cls, \"_helper\")` / "
        "`monkeypatch.setattr`.\n"
        + _NEVER_SKIP,
    ),
    "java": (
        re.compile(r"(?m)^\s*(?:private|protected)\s"),
        "Non-public members in Java (MANDATORY - they are testable):\n"
        "- Declare the test class in the SAME package as the class under test: "
        "package-private and protected members are then callable directly.\n"
        "- Protected members: declare a small subclass of the class under test inside the "
        "test file and expose the member through a public method.\n"
        "- Private members: use reflection - "
        "`Method m = Cls.class.getDeclaredMethod(\"name\", ArgType.class); "
        "m.setAccessible(true); m.invoke(obj, args);` and "
        "`Field f = Cls.class.getDeclaredField(\"field\"); f.setAccessible(true);` for "
        "state. Prefer a reflection helper the project already uses (e.g. "
        "`ReflectionTestUtils.setField/getField`) when it appears in the UT library above.\n"
        + _NEVER_SKIP,
    ),
    "csharp": (
        re.compile(r"(?m)^\s*(?:private|protected|internal)\s"),
        "Non-public members in C# (MANDATORY - they are testable):\n"
        "- `internal` members: the test assembly normally has `InternalsVisibleTo`, so call "
        "them directly.\n"
        "- `protected` members: derive a small test subclass inside the test file that "
        "exposes the member, and instantiate that subclass.\n"
        "- `private` members: use reflection - "
        "`typeof(Cls).GetMethod(\"Name\", BindingFlags.NonPublic | BindingFlags.Instance)"
        ".Invoke(obj, new object[] { ... })`, and the matching `GetField` / `GetProperty` "
        "for state.\n"
        + _NEVER_SKIP,
    ),
    "go": (
        re.compile(r"(?m)^\s*(?:func|type|var|const)\s+\(?\s*\w*\s*\*?\w*\)?\s*[a-z]\w*"),
        "Unexported identifiers in Go (MANDATORY - they are testable):\n"
        "- Lower-case (unexported) functions, types, fields and methods are visible to any "
        "test file in the SAME package: declare the test file as `package <same package>` "
        "(not `<package>_test`) and call them directly.\n"
        + _NEVER_SKIP,
    ),
}


class LanguageAdapter(ABC):
    """Interface every language plugin implements.

    Adapters must degrade gracefully: any capability that requires missing
    external tooling returns ``None`` (or a result explaining why) instead of
    raising.
    """

    language_id: str = ""
    display_name: str = ""
    source_extensions: tuple[str, ...] = ()
    header_extensions: tuple[str, ...] = ()  # declaration-only; no dedicated UT expected

    # ---- detection -------------------------------------------------------

    @abstractmethod
    def detection_score(self, root: Path, files: list[ScannedFile]) -> int:
        """How strongly this project looks like this language (0 = not at all)."""

    @abstractmethod
    def detect_environment(
        self, root: Path, files: list[ScannedFile], test_dir: Path | None = None
    ) -> EnvironmentInfo:
        """Detect framework, build system, commands and mocking libraries.

        ``test_dir`` is the project's unit-test folder when it is known; it makes
        framework detection independent of which folders the user selected.
        """

    # ---- test discovery --------------------------------------------------

    @abstractmethod
    def is_test_file(self, path: Path, content: str | None = None) -> bool:
        """Whether the file is a unit-test file for this language."""

    @abstractmethod
    def parse_test_file(self, path: Path, content: str) -> TestFile:
        """Extract test suites/cases from an existing test file."""

    # ---- generation support ----------------------------------------------

    @abstractmethod
    def test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        """Conventional test file name for a given source file."""

    def new_test_file_name_for(self, source: Path, conventions_file_naming: str | None) -> str:
        """File name used when creating a brand new UT file.

        Follows the naming convention detected in the project's test folder
        (e.g. ``<name>UT.cpp`` -> ``fooUT.cpp``). When no convention could be
        detected the language's own idiomatic default is used, rather than an
        invented suffix that matches nothing the project's tooling looks for.
        """
        return self.test_file_name_for(source, conventions_file_naming)

    @abstractmethod
    def new_file_preamble(
        self,
        source: Path,
        env: EnvironmentInfo,
        test_path: Path | None = None,
        template: "TestTemplate | None" = None,
    ) -> str:
        """Imports/includes + boilerplate for a brand new test file.

        ``test_path`` is the resolved UT file (named after the project's
        convention) so includes and class names can match its name.
        ``template`` carries the structure observed in neighbouring test files;
        when it is present it takes precedence over any language default.
        """

    def new_test_header(
        self,
        test_path: Path,
        source: Path,
        env: EnvironmentInfo,
        template: "TestTemplate | None" = None,
    ) -> tuple[Path, str] | None:
        """Companion header (path, content) to create next to a brand new test file.

        Default: languages without headers return ``None``.
        """
        return None

    def insertion_point(self, original: str) -> int:
        """Character offset where new tests should be inserted in an existing file.

        Default: append at the end. Languages with class-wrapped tests override.
        """
        return len(original)

    def access_notes(self, source: Path, source_code: str) -> str:
        """Prompt text describing member visibility of the code under test.

        Lists which members are non-public and, above all, HOW the tests can
        still reach them in this language, so the model never skips a member or
        writes a ``TODO`` about it. C++ overrides this with a per-class analysis.
        """
        entry = _ACCESS_STRATEGIES.get(self.language_id)
        if entry is None:
            return ""
        pattern, text = entry
        if not pattern.search(source_code or ""):
            return ""
        return text

    # ---- validation -------------------------------------------------------

    def check_syntax(self, code: str) -> tuple[bool, str]:
        """Syntax-check a full candidate file content. Default: tree-sitter."""
        return check_syntax_tree_sitter(code, self.language_id)

    @abstractmethod
    def compile_test_target(
        self, root: Path, env: EnvironmentInfo, test_file: Path
    ) -> CommandResult | None:
        """Compile the relevant test target. None => compilation not applicable/possible."""

    @abstractmethod
    def run_single_test(
        self, root: Path, env: EnvironmentInfo, test_file: Path, test_name: str
    ) -> CommandResult | None:
        """Run one test by name. None => running not possible (missing tooling)."""

    # ---- metrics ----------------------------------------------------------

    @abstractmethod
    def count_assertions(self, test_body: str) -> int:
        """Count assertion statements in a test body."""

    # ---- helpers -----------------------------------------------------------

    def source_function_names(self, content: str) -> list[str]:
        """Best-effort extraction of public function/method names from source."""
        return []


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, LanguageAdapter] = {}

    def register(self, adapter: LanguageAdapter) -> None:
        self._adapters[adapter.language_id] = adapter

    def get(self, language_id: str) -> LanguageAdapter | None:
        return self._adapters.get(language_id)

    def all(self) -> list[LanguageAdapter]:
        return list(self._adapters.values())

    def extension_map(self) -> dict[str, str]:
        """Map file extension -> language id."""
        mapping: dict[str, str] = {}
        for adapter in self._adapters.values():
            for ext in adapter.source_extensions:
                mapping[ext] = adapter.language_id
        return mapping

    def detect_primary(
        self, root: Path, files: list[ScannedFile]
    ) -> LanguageAdapter | None:
        """The adapter with the highest detection score, if any scored > 0."""
        best: LanguageAdapter | None = None
        best_score = 0
        for adapter in self._adapters.values():
            score = adapter.detection_score(root, files)
            if score > best_score:
                best, best_score = adapter, score
        return best


registry = AdapterRegistry()


def load_default_adapters() -> None:
    """Register all built-in adapters (idempotent)."""
    if registry.all():
        return
    from .cpp.adapter import CppAdapter
    from .csharp.adapter import CSharpAdapter
    from .go.adapter import GoAdapter
    from .java.adapter import JavaAdapter
    from .javascript.adapter import JavaScriptAdapter
    from .python.adapter import PythonAdapter

    for cls in (
        CppAdapter,
        PythonAdapter,
        JavaScriptAdapter,
        JavaAdapter,
        CSharpAdapter,
        GoAdapter,
    ):
        registry.register(cls())
