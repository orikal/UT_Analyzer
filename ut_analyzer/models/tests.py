"""Models describing existing unit tests and their conventions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestCase:
    """A single unit test found in an existing test file."""

    name: str
    suite: str | None = None
    file: Path | None = None
    line: int = 0
    body: str = ""
    assertion_count: int = 0


@dataclass
class TestFile:
    """A test file with its parsed test cases."""

    path: Path
    language: str
    framework: str | None = None
    cases: list[TestCase] = field(default_factory=list)
    content: str = ""


@dataclass
class TestConventions:
    """Conventions extracted from existing tests, fed to the generation prompt."""

    file_naming: str | None = None          # e.g. "test_<name>.py", "<name>_test.cpp"
    suite_naming: str | None = None         # e.g. "PascalCase class Test<Name>"
    case_naming: str | None = None          # e.g. "snake_case, prefix test_"
    uses_fixtures: bool = False
    fixture_style: str | None = None        # e.g. "pytest fixtures", "TEST_F fixtures"
    setup_teardown: str | None = None       # e.g. "setUp/tearDown methods"
    mocking: str | None = None              # e.g. "unittest.mock.patch", "gMock"
    assertion_style: str | None = None      # e.g. "plain assert", "EXPECT_* macros"
    aaa_structure: bool = False             # arrange-act-assert with blank lines/comments
    common_imports: list[str] = field(default_factory=list)
    helper_utilities: list[str] = field(default_factory=list)
    example_test: str = ""                  # a representative existing test, verbatim
    indent: str = "    "

    def to_prompt_text(self) -> str:
        """Render conventions as instructions for the LLM prompt."""
        lines: list[str] = []
        if self.file_naming:
            lines.append(f"- Test file naming: {self.file_naming}")
        if self.suite_naming:
            lines.append(f"- Test suite naming: {self.suite_naming}")
        if self.case_naming:
            lines.append(f"- Test case naming: {self.case_naming}")
        if self.uses_fixtures and self.fixture_style:
            lines.append(f"- Fixtures: {self.fixture_style}")
        if self.setup_teardown:
            lines.append(f"- Setup/teardown: {self.setup_teardown}")
        if self.mocking:
            lines.append(f"- Mocking: {self.mocking}")
        if self.assertion_style:
            lines.append(f"- Assertion style: {self.assertion_style}")
        if self.aaa_structure:
            lines.append("- Tests follow Arrange-Act-Assert structure")
        if self.common_imports:
            lines.append("- Common imports/includes used by tests:")
            lines.extend(f"    {imp}" for imp in self.common_imports[:10])
        if self.helper_utilities:
            lines.append(
                "- Existing test helpers available: " + ", ".join(self.helper_utilities[:10])
            )
        return "\n".join(lines) if lines else "- No existing conventions detected."


@dataclass
class UTHelperFile:
    """A non-test file living in the UT folder (fixture base, mock, helper, header)."""

    name: str
    role: str = "helper"          # "fixture" | "mock" | "helper" | "header"
    declarations: str = ""        # condensed content: declarations only


@dataclass
class UTCodeSample:
    """A verbatim snippet copied from the existing unit tests."""

    title: str                    # e.g. "SetUp() of CalculatorTest" or "Reader"
    code: str = ""
    file: str = ""


@dataclass
class FixtureLifecycle:
    """How this project's test fixtures initialize and release themselves.

    Some teams put everything in ``SetUp()``/``TearDown()``, others give the
    fixture a real constructor/destructor (often defined out-of-line in the test
    source file) or a hand-written ``Init()`` method. Counts are *file* counts,
    so a style that only one file uses never wins over the house style.
    """

    constructor_files: int = 0
    destructor_files: int = 0
    setup_files: int = 0
    teardown_files: int = 0
    #: Files that use any lifecycle hook at all - the base of the majority rule.
    files_with_hooks: int = 0
    #: Files where a constructor/destructor was defined out-of-line (``X::X()``).
    out_of_line_files: int = 0
    #: Hand-written initialization methods, e.g. ``Init``, ``InitTest``.
    init_methods: tuple[str, ...] = ()
    #: Verbatim bodies, so the model can imitate them.
    samples: list[UTCodeSample] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.constructor_files
            or self.destructor_files
            or self.setup_files
            or self.teardown_files
            or self.init_methods
        )

    @property
    def uses_constructor(self) -> bool:
        return self._dominant(self.constructor_files)

    @property
    def uses_destructor(self) -> bool:
        return self._dominant(self.destructor_files)

    @property
    def uses_setup(self) -> bool:
        return self._dominant(self.setup_files)

    @property
    def uses_teardown(self) -> bool:
        return self._dominant(self.teardown_files)

    @property
    def defines_out_of_line(self) -> bool:
        """True when the fixture bodies live in the test source file, not the header."""
        if not self.out_of_line_files:
            return False
        return self.out_of_line_files * 2 >= max(
            self.constructor_files, self.destructor_files, 1
        )

    def _dominant(self, count: int) -> bool:
        """A hook is the house style when at least half the fixtures use it."""
        total = max(
            self.files_with_hooks,
            self.constructor_files,
            self.destructor_files,
            self.setup_files,
            self.teardown_files,
        )
        return bool(count) and count * 2 >= total

    def describe(self) -> str:
        """One line for the prompt/trace, e.g. 'constructor + destructor, out-of-line'."""
        if self.is_empty:
            return "no fixture lifecycle learned"
        parts: list[str] = []
        if self.uses_constructor:
            parts.append("a fixture constructor")
        if self.uses_destructor:
            parts.append("a fixture destructor")
        if self.uses_setup:
            parts.append("SetUp()")
        if self.uses_teardown:
            parts.append("TearDown()")
        if self.init_methods:
            parts.append("init method(s) " + ", ".join(f"{n}()" for n in self.init_methods))
        if not parts:
            return "no dominant fixture lifecycle"
        where = (
            " defined in the test source file, not inline in the header"
            if self.defines_out_of_line
            else ""
        )
        return "this project's fixtures use " + ", ".join(parts) + where


@dataclass
class UTKnowledge:
    """What the tool learned by reading every file of the project's UT folder.

    Fed to the generation prompt so the model reuses the fixtures, mocks and
    helpers the team already wrote instead of inventing new ones.
    """

    test_dir: Path | None = None
    test_file_count: int = 0
    test_case_count: int = 0
    helper_files: list[UTHelperFile] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    mocks: list[str] = field(default_factory=list)
    helpers: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    shared_imports: list[str] = field(default_factory=list)
    example_tests: list[str] = field(default_factory=list)
    setup_patterns: list[UTCodeSample] = field(default_factory=list)
    construction_recipes: list[UTCodeSample] = field(default_factory=list)
    member_declarations: list[str] = field(default_factory=list)
    helper_usages: list[str] = field(default_factory=list)
    lifecycle: FixtureLifecycle | None = None

    def is_empty(self) -> bool:
        return not (
            self.helper_files
            or self.fixtures
            or self.mocks
            or self.helpers
            or self.constants
            or self.shared_imports
            or self.example_tests
            or self.setup_patterns
            or self.construction_recipes
            or self.member_declarations
            or self.helper_usages
            or (self.lifecycle is not None and not self.lifecycle.is_empty)
        )

    def summary(self) -> str:
        """Short human-readable line for the UI/progress log."""
        folder = self.test_dir.name if self.test_dir is not None else "unit tests"
        lifecycle = (
            f", {len(self.lifecycle.samples)} fixture lifecycle bodies"
            if self.lifecycle is not None and self.lifecycle.samples
            else ""
        )
        return (
            f"Studied {folder}: {self.test_file_count} test files, "
            f"{self.test_case_count} test cases, {len(self.helper_files)} helper files, "
            f"{len(self.fixtures)} fixtures, {len(self.mocks)} mocks, "
            f"{len(self.setup_patterns)} setup/fixture bodies, "
            f"{len(self.construction_recipes)} construction recipes, "
            f"{len(self.example_tests)} example tests" + lifecycle
        )

    def to_prompt_text(
        self,
        *,
        skip_examples: tuple[str, ...] = (),
        max_chars: int | None = None,
    ) -> str:
        """Render the learned UT folder knowledge as prompt instructions.

        Blocks are emitted most-useful-first and a block is dropped as a whole
        when it no longer fits ``max_chars``; nothing is ever cut mid-snippet.
        """
        examples = [item for item in self.example_tests if item not in skip_examples]
        blocks: list[str] = []
        symbols = self._symbol_lines()
        if symbols:
            blocks.append(symbols)
        blocks.extend(self._example_blocks(examples[:2]))
        if self.lifecycle is not None and self.lifecycle.samples:
            blocks.append(
                "How this project's test fixtures are initialized and destroyed - "
                f"{self.lifecycle.describe()}. Give your fixture the SAME lifecycle "
                "hooks, declared and defined in the same files as below, and build its "
                "members in the same order with the same calls:\n\n"
                + "\n\n".join(_render_samples(self.lifecycle.samples))
            )
        if self.setup_patterns:
            blocks.append(
                "How this project's tests set up the object under test "
                "(SetUp/TearDown/fixture bodies, verbatim). Build your fixture and your "
                "objects the same way - same members, same construction calls, same "
                "initialization order:\n\n"
                + "\n\n".join(_render_samples(self.setup_patterns))
            )
        if self.construction_recipes:
            blocks.append(
                "How this project's tests create and initialize variables, collaborators, "
                "adapters and mocks (real code from the UT folder). Reproduce these "
                "recipes instead of inventing your own:\n\n"
                + "\n\n".join(_render_samples(self.construction_recipes))
            )
        if self.member_declarations:
            blocks.append(
                "Members the existing test fixtures declare (exact types and names - "
                "reuse them instead of declaring your own):\n"
                + "\n".join(f"    {item}" for item in self.member_declarations[:40])
            )
        if self.helper_usages:
            blocks.append(
                "How the existing tests call the shared helpers/mocks (copy these call "
                "styles and argument shapes):\n"
                + "\n".join(f"    {item}" for item in self.helper_usages[:30])
            )
        for helper in self.helper_files:
            if not helper.declarations:
                continue
            blocks.append(
                f"Declarations from the existing UT {helper.role} file "
                f"`{helper.name}`:\n```\n{helper.declarations}\n```"
            )
        blocks.extend(self._example_blocks(examples[2:]))
        return _join_within_budget(blocks, max_chars)

    def _symbol_lines(self) -> str:
        lines: list[str] = []
        if self.fixtures:
            lines.append("- Existing test fixtures / base classes you should reuse:")
            lines.extend(f"    {item}" for item in self.fixtures[:20])
        if self.mocks:
            lines.append("- Existing mocks/fakes you should reuse:")
            lines.extend(f"    {item}" for item in self.mocks[:20])
        if self.helpers:
            lines.append("- Existing test helper functions you should reuse:")
            lines.extend(f"    {item}" for item in self.helpers[:30])
        if self.constants:
            lines.append("- Test constants/data already defined in the UT folder:")
            lines.extend(f"    {item}" for item in self.constants[:20])
        if self.shared_imports:
            lines.append("- Includes/imports the UT folder shares:")
            lines.extend(f"    {item}" for item in self.shared_imports[:15])
        return "\n".join(lines)

    @staticmethod
    def _example_blocks(examples: list[str]) -> list[str]:
        return [
            "Existing test from the UT folder (match this style):\n```\n"
            + example
            + "\n```"
            for example in examples
        ]


def _render_samples(samples: list[UTCodeSample]) -> list[str]:
    rendered: list[str] = []
    for sample in samples:
        where = f" (in `{sample.file}`)" if sample.file else ""
        rendered.append(f"{sample.title}{where}:\n```\n{sample.code}\n```")
    return rendered


def _join_within_budget(blocks: list[str], max_chars: int | None) -> str:
    if max_chars is None:
        return "\n\n".join(block for block in blocks if block)
    kept: list[str] = []
    used = 0
    for block in blocks:
        if not block:
            continue
        cost = len(block) + (2 if kept else 0)
        if used + cost > max_chars:
            continue
        kept.append(block)
        used += cost
    return "\n\n".join(kept)
