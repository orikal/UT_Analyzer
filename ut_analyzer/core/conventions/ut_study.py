"""Study of the project's UT folder.

Once the UT (unit-test) directory is recognised, every file inside it is read
so the model can *learn* the existing tests: which fixtures, mocks, helper
functions and constants the team already wrote, which includes/imports the
tests share, and how a real test in this project looks. The result is embedded
in the generation prompt, so new tests reuse the existing test library instead
of reinventing it.

Everything here is best-effort and regex based.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from ...adapters.base import LanguageAdapter
from ...models import TestFile, UTCodeSample, UTHelperFile, UTKnowledge
from ...utils.fs import is_excluded_dir, read_text_safe
from ..cancellation import raise_if_cancelled
from ..generation.symbol_context import condense_declarations
from .fixture_lifecycle import (
    MAX_LINE_CHARS,
    analyze_fixture_lifecycle,
    brace_body,
    dedent,
    enclosing_class,
    opens_block,
    python_body,
)

DEFAULT_MAX_HELPER_FILES = 20
DEFAULT_MAX_CHARS_PER_FILE = 6000
DEFAULT_MAX_EXAMPLE_TESTS = 6
DEFAULT_MAX_SETUP_PATTERNS = 8
DEFAULT_MAX_CONSTRUCTION_RECIPES = 16
_MAX_FILES_READ = 400
_MAX_RECIPE_LINES = 8
_MAX_RECIPE_LOOKAHEAD = 12
_MAX_MEMBER_DECLARATIONS = 40
_MAX_HELPER_USAGES = 30
_MAX_LINE_CHARS = MAX_LINE_CHARS

_IMPORT_RES: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^(?:from\s+\S+\s+import\s+.+|import\s+\S+.*)$", re.MULTILINE),
    "cpp": re.compile(r"^#include\s*[<\"].+[>\"]$", re.MULTILINE),
    "javascript": re.compile(
        r"^(?:import\s.+|const\s+.+=\s*require\(.+\).*)$", re.MULTILINE
    ),
    "java": re.compile(r"^import\s+.+;$", re.MULTILINE),
    "csharp": re.compile(r"^using\s+.+;$", re.MULTILINE),
    "go": re.compile(r"^import\s+(?:\(|\").*$", re.MULTILINE),
}

_HELPER_NAME_HINTS = ("helper", "util", "fixture", "common", "base", "mock", "fake", "stub")
_MOCK_NAME_RE = re.compile(r"(mock|fake|stub|dummy|spy)", re.IGNORECASE)

# ---- symbol patterns -------------------------------------------------------

_GTEST_FIXTURE_RE = re.compile(
    r"\bclass\s+(\w+)\s*:\s*public\s+(?:::)?(?:testing|Test)\w*::\w*Test\w*\b"
)
_BRACE_CLASS_RE = re.compile(r"\bclass\s+(\w+)\b")
_PY_FIXTURE_RE = re.compile(r"@pytest\.fixture[^\n]*\n(?:@[^\n]*\n)*\s*def\s+(\w+)\s*\(")
_PY_TESTCASE_RE = re.compile(r"\bclass\s+(\w+)\s*\(\s*[\w.]*TestCase\s*\)")
_PY_CLASS_RE = re.compile(r"^class\s+(\w+)", re.MULTILINE)
_PY_FUNC_RE = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
_PY_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=", re.MULTILINE)
_BRACE_FUNC_RE = re.compile(
    r"^[\w:<>,\s\*&\[\]]*?\b(\w+)\s*\([^;{)]*\)\s*(?:const\s*)?[;{]", re.MULTILINE
)
_BRACE_CONST_RE = re.compile(
    r"^\s*(?:static\s+|const\s+|constexpr\s+|readonly\s+|final\s+)+[\w:<>,\s\*&]*?"
    r"\b([A-Za-z_]\w*)\s*(?:=|\{)",
    re.MULTILINE,
)
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)", re.MULTILINE)

# ---- "how the tests do it" patterns ----------------------------------------

_PY_SETUP_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(setUp|tearDown|setUpClass|tearDownClass|"
    r"setup_method|teardown_method|setup_class|teardown_class|setup|teardown)\s*\("
)
_PY_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_PY_DECORATOR_RE = re.compile(r"^\s*@")
_PY_FIXTURE_DECORATOR_RE = re.compile(r"^\s*@(?:pytest\.)?fixture\b")
_BRACE_SETUP_RE = re.compile(
    r"^\s*(?:\[[A-Za-z]+\]\s*)?(?:virtual\s+|public\s+|protected\s+|private\s+|"
    r"static\s+|override\s+|async\s+)*\w[\w:<>,\s\*&]*\b"
    r"(SetUp|TearDown|setUp|tearDown|setup|teardown|BeforeEach|AfterEach|"
    r"beforeEach|afterEach|Setup|Cleanup|Initialize)\s*\([^;)]*\)"
)

# `Type var(...)`, `Type var{...}`, `Type var = ...`, `auto var = make_unique<Type>(...)`
_BRACE_NEW_RE = re.compile(
    r"\b(?P<var>[A-Za-z_]\w*)\s*=\s*(?:new\s+|std::make_(?:unique|shared)\s*<\s*)?"
    r"(?P<type>[A-Z]\w*)\s*[<(\{]"
)
_BRACE_DECL_RE = re.compile(
    r"^\s*(?:const\s+|static\s+|auto\s+)*(?:\w+::)*(?P<type>[A-Z]\w*)"
    r"(?:\s*<[^;=]*>)?\s*[\*&]?\s+(?P<var>[a-z_]\w*)\s*[({=]"
)
_PY_NEW_RE = re.compile(
    r"^\s*(?:self\.)?(?P<var>[A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?:[\w.]+\.)?"
    r"(?P<type>[A-Z]\w*)\s*\("
)
_MEMBER_DECL_RE = re.compile(
    r"^\s*(?:mutable\s+|static\s+|const\s+|constexpr\s+|readonly\s+|private\s+|"
    r"protected\s+|public\s+)*"
    r"(?P<decl>(?:\w+::)*[A-Za-z_]\w*(?:\s*<[^;{}]*>)?(?:\s*[\*&]+\s*|\s+)"
    r"[A-Za-z_]\w*(?:\[[^\]]*\])?)"
    r"\s*(?:=[^;{}]*)?;\s*(?://.*)?$"
)
_PY_MEMBER_RE = re.compile(r"^\s*self\.(?P<name>\w+)\s*(?::\s*(?P<hint>[^=]+))?=\s*(?P<value>.+)$")
_SKIP_WORDS = {
    "return", "if", "else", "for", "while", "switch", "case", "delete", "new",
    "sizeof", "throw", "catch", "do", "using", "typedef", "public", "private",
    "protected", "virtual", "static", "const", "friend", "template", "operator",
}
_CONTROL_LINE_RE = re.compile(r"^\s*(?:if|for|while|switch|catch|return|else)\b")


def study_test_folder(
    test_dir: Path | None,
    test_files: list[TestFile],
    adapter: LanguageAdapter,
    *,
    max_helper_files: int = DEFAULT_MAX_HELPER_FILES,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_example_tests: int = DEFAULT_MAX_EXAMPLE_TESTS,
    max_setup_patterns: int = DEFAULT_MAX_SETUP_PATTERNS,
    max_construction_recipes: int = DEFAULT_MAX_CONSTRUCTION_RECIPES,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> UTKnowledge:
    """Read every file of the recognised UT folder and learn its test library."""
    language = adapter.language_id
    folder = test_dir if test_dir is not None else _common_parent(test_files)
    knowledge = UTKnowledge(test_dir=folder)

    parsed_by_path = {tf.path.resolve(): tf for tf in test_files}
    folder_files = _folder_files(folder, adapter) if folder is not None else []

    # Test files that belong to the studied folder (fall back to all parsed ones).
    in_folder = [
        parsed_by_path[path] for path in folder_files if path in parsed_by_path
    ]
    studied_tests = in_folder or test_files
    knowledge.test_file_count = len(studied_tests)
    knowledge.test_case_count = sum(len(tf.cases) for tf in studied_tests)

    # (file name, full content) of every UT file we read: tests first, helpers next.
    sources: list[tuple[str, str]] = [
        (tf.path.name, tf.content) for tf in studied_tests if tf.content
    ]

    for path in _rank_helpers(folder_files, parsed_by_path):
        raise_if_cancelled(cancelled)
        if len(knowledge.helper_files) >= max_helper_files:
            break
        content = read_text_safe(path)
        if not content:
            continue
        if progress is not None:
            progress(f"Learning {path.name}...")
        sources.append((path.name, content))
        declarations = condense_declarations(content, language).strip()
        knowledge.helper_files.append(
            UTHelperFile(
                name=path.name,
                role=_helper_role(path, content, adapter),
                declarations=declarations[:max_chars_per_file],
            )
        )

    sources = sources[:_MAX_FILES_READ]
    texts = [content for _, content in sources]
    joined = "\n".join(texts)
    knowledge.fixtures = _find_fixtures(joined, language)
    knowledge.mocks = _find_mocks(joined, language)
    knowledge.helpers = _find_helpers(knowledge.helper_files, language)
    knowledge.constants = _find_constants(knowledge.helper_files, language)
    knowledge.shared_imports = _shared_imports(texts, language)
    knowledge.example_tests = _example_tests(studied_tests, max_example_tests)

    raise_if_cancelled(cancelled)
    if progress is not None:
        progress("Learning how the existing tests build their objects...")
    knowledge.setup_patterns = _find_setup_patterns(
        sources, language, max_setup_patterns
    )
    knowledge.construction_recipes = _find_construction_recipes(
        sources, language, max_construction_recipes
    )
    knowledge.member_declarations = _find_member_declarations(sources, language)
    knowledge.helper_usages = _find_helper_usages(sources, knowledge, language)
    knowledge.lifecycle = analyze_fixture_lifecycle(
        sources, language, knowledge.fixtures
    )
    return knowledge


# ---- file collection -------------------------------------------------------


def _common_parent(test_files: list[TestFile]) -> Path | None:
    """Directory holding most of the discovered test files."""
    counter: Counter[Path] = Counter(tf.path.parent.resolve() for tf in test_files)
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _folder_files(folder: Path, adapter: LanguageAdapter) -> list[Path]:
    """All code files of the adapter's language inside the UT folder."""
    extensions = {
        ext.lower() for ext in (*adapter.source_extensions, *adapter.header_extensions)
    }
    found: list[Path] = []
    stack = [folder]
    while stack and len(found) < _MAX_FILES_READ:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if not is_excluded_dir(child.name):
                    stack.append(child)
            elif child.suffix.lower() in extensions:
                found.append(child.resolve())
                if len(found) >= _MAX_FILES_READ:
                    break
    return found


def _rank_helpers(
    folder_files: list[Path], parsed_by_path: dict[Path, TestFile]
) -> list[Path]:
    """Non-test files of the UT folder, most helper-like first."""
    candidates = [path for path in folder_files if path not in parsed_by_path]

    def score(path: Path) -> int:
        name = path.name.lower()
        value = 0
        if any(hint in name for hint in _HELPER_NAME_HINTS):
            value += 20
        if name == "conftest.py":
            value += 30
        if path.suffix.lower() in (".h", ".hpp", ".hxx", ".hh"):
            value += 10
        return -value

    return sorted(candidates, key=lambda p: (score(p), p.name.lower()))


def _helper_role(path: Path, content: str, adapter: LanguageAdapter) -> str:
    name = path.name.lower()
    if "MOCK_METHOD" in content or _MOCK_NAME_RE.search(name):
        return "mock"
    if "fixture" in name or "conftest" in name or "@pytest.fixture" in content:
        return "fixture"
    if path.suffix.lower() in {ext.lower() for ext in adapter.header_extensions}:
        return "header"
    return "helper"


# ---- symbol extraction -----------------------------------------------------


def _find_fixtures(text: str, language: str) -> list[str]:
    names: list[str] = []
    if language == "python":
        names.extend(_PY_FIXTURE_RE.findall(text))
        names.extend(_PY_TESTCASE_RE.findall(text))
    else:
        names.extend(_GTEST_FIXTURE_RE.findall(text))
        names.extend(
            name
            for name in _BRACE_CLASS_RE.findall(text)
            if re.search(r"(fixture|testbase|basetest)", name, re.IGNORECASE)
        )
    return _unique(names)


def _find_mocks(text: str, language: str) -> list[str]:
    if language == "python":
        candidates = _PY_CLASS_RE.findall(text)
    else:
        candidates = _BRACE_CLASS_RE.findall(text)
    return _unique(name for name in candidates if _MOCK_NAME_RE.search(name))


def _find_helpers(helper_files: list[UTHelperFile], language: str) -> list[str]:
    entries: list[str] = []
    pattern = _PY_FUNC_RE if language == "python" else _BRACE_FUNC_RE
    for helper in helper_files:
        for name in pattern.findall(helper.declarations):
            if name.lower().startswith("test") or name in {"if", "for", "while", "switch"}:
                continue
            entries.append(f"{name} (in {helper.name})")
    return _unique(entries)


def _find_constants(helper_files: list[UTHelperFile], language: str) -> list[str]:
    entries: list[str] = []
    for helper in helper_files:
        if language == "python":
            names = _PY_CONST_RE.findall(helper.declarations)
        else:
            names = _DEFINE_RE.findall(helper.declarations) + [
                name
                for name in _BRACE_CONST_RE.findall(helper.declarations)
                if name.isupper() or name.startswith("k")
            ]
        entries.extend(f"{name} (in {helper.name})" for name in names)
    return _unique(entries)


def _shared_imports(texts: list[str], language: str) -> list[str]:
    pattern = _IMPORT_RES.get(language)
    if pattern is None or not texts:
        return []
    counter: Counter[str] = Counter()
    for text in texts[:60]:
        for match in pattern.findall(text):
            counter[match.strip()] += 1
    threshold = 2 if len(texts) > 1 else 1
    return [imp for imp, count in counter.most_common(10) if count >= threshold]


def _example_tests(test_files: list[TestFile], limit: int) -> list[str]:
    """Pick representative tests, preferring one per file."""
    if limit <= 0:
        return []
    picked: list[str] = []
    for tf in test_files:
        best = [c for c in tf.cases if c.body and c.assertion_count > 0]
        if not best:
            continue
        best.sort(key=lambda c: abs(len(c.body) - 600))
        picked.append(best[0].body[:4000])
        if len(picked) >= limit:
            break
    return picked


# ---- how the existing tests build things -----------------------------------


def _find_setup_patterns(
    sources: list[tuple[str, str]], language: str, limit: int
) -> list[UTCodeSample]:
    """SetUp/TearDown/fixture bodies, verbatim: how the team builds test objects."""
    samples: list[UTCodeSample] = []
    seen: set[str] = set()
    for file_name, content in sources:
        if len(samples) >= limit:
            break
        lines = content.splitlines()
        for index, line in enumerate(lines):
            if len(samples) >= limit:
                break
            name = _setup_name(lines, index, language)
            if name is None:
                continue
            body = (
                python_body(lines, index)
                if language == "python"
                else brace_body(lines, index)
            )
            code = dedent(body)
            if not code or code in seen:
                continue
            seen.add(code)
            owner = enclosing_class(lines, index, language)
            title = f"{name}() of {owner}" if owner else f"{name}()"
            samples.append(UTCodeSample(title=title, code=code, file=file_name))
    return samples


def _setup_name(lines: list[str], index: int, language: str) -> str | None:
    """Name of the setup/teardown/fixture function starting at `index`, if any."""
    line = lines[index]
    if language == "python":
        match = _PY_SETUP_RE.match(line)
        if match is not None:
            return match.group(1)
        definition = _PY_DEF_RE.match(line)
        if definition is None:
            return None
        for previous in reversed(lines[max(0, index - 4): index]):
            if not previous.strip():
                continue
            if _PY_FIXTURE_DECORATOR_RE.match(previous):
                return definition.group(1)
            if not _PY_DECORATOR_RE.match(previous):
                break
        return None
    if "(" not in line or line.lstrip().startswith(("//", "*", "#")):
        return None
    match = _BRACE_SETUP_RE.match(line)
    if match is None:
        return None
    if "{" not in line and not opens_block(lines, index):
        return None  # a declaration without a body teaches nothing
    return match.group(1)


def _find_construction_recipes(
    sources: list[tuple[str, str]], language: str, limit: int
) -> list[UTCodeSample]:
    """Real code showing how the tests create variables, collaborators and mocks."""
    samples: list[UTCodeSample] = []
    seen_types: set[str] = set()
    for file_name, content in sources:
        if len(samples) >= limit:
            break
        lines = content.splitlines()
        for index, line in enumerate(lines):
            if len(samples) >= limit:
                break
            found = _construction_at(line, language)
            if found is None:
                continue
            type_name, variable = found
            if type_name in seen_types:
                continue
            code = _recipe_snippet(lines, index, variable)
            if not code:
                continue
            seen_types.add(type_name)
            samples.append(
                UTCodeSample(title=type_name, code=code, file=file_name)
            )
    return samples


def _construction_at(line: str, language: str) -> tuple[str, str] | None:
    """(type, variable) constructed on this line, ignoring comments and control flow."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "#", "*", "/*")):
        return None
    if _CONTROL_LINE_RE.match(line):
        return None
    patterns = (
        (_PY_NEW_RE,) if language == "python" else (_BRACE_DECL_RE, _BRACE_NEW_RE)
    )
    for pattern in patterns:
        match = pattern.search(line)
        if match is None:
            continue
        type_name = match.group("type")
        variable = match.group("var")
        if type_name in _SKIP_WORDS or variable in _SKIP_WORDS:
            continue
        if type_name == variable or len(type_name) < 3:
            continue
        return type_name, variable
    return None


def _recipe_snippet(lines: list[str], index: int, variable: str) -> str:
    """Construction line plus the follow-up lines that keep initializing the object."""
    picked = [lines[index]]
    open_parens = lines[index].count("(") - lines[index].count(")")
    mentions = re.compile(rf"\b{re.escape(variable)}\b")
    misses = 0
    for follow in lines[index + 1: index + 1 + _MAX_RECIPE_LOOKAHEAD]:
        if len(picked) >= _MAX_RECIPE_LINES:
            break
        if open_parens > 0:  # multi-line construction: keep it whole
            picked.append(follow)
            open_parens += follow.count("(") - follow.count(")")
            continue
        stripped = follow.strip()
        if stripped and mentions.search(follow) is not None:
            picked.append(follow)
            misses = 0
            if stripped.startswith("return"):
                break
            continue
        misses += 1
        if misses >= 2:
            break
    return dedent(picked)


def _find_member_declarations(
    sources: list[tuple[str, str]], language: str
) -> list[str]:
    """Members the existing fixtures declare, with their exact types."""
    entries: list[str] = []
    for file_name, content in sources:
        for line in content.splitlines():
            if len(entries) >= _MAX_MEMBER_DECLARATIONS:
                return _unique(entries)
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "#", "*", "/*")):
                continue
            if language == "python":
                match = _PY_MEMBER_RE.match(line)
                if match is None:
                    continue
                value = match.group("value").strip()[:_MAX_LINE_CHARS]
                entries.append(f"self.{match.group('name')} = {value}  (in {file_name})")
                continue
            if "(" in stripped:
                continue  # a call or a function declaration, not a member
            match = _MEMBER_DECL_RE.match(line)
            if match is None:
                continue
            declaration = " ".join(match.group("decl").split())
            if declaration.split()[0] in _SKIP_WORDS:
                continue
            entries.append(f"{declaration};  (in {file_name})")
    return _unique(entries)


def _find_helper_usages(
    sources: list[tuple[str, str]], knowledge: UTKnowledge, language: str
) -> list[str]:
    """One real call site per known helper/fixture/mock, so the model copies the style."""
    names = _unique(
        [
            *(entry.split(" (")[0] for entry in knowledge.helpers),
            *knowledge.mocks,
            *knowledge.fixtures,
        ]
    )
    if not names:
        return []
    wanted = {name for name in names if len(name) > 2}
    entries: list[str] = []
    used: set[str] = set()
    for file_name, content in sources:
        for line in content.splitlines():
            if len(entries) >= _MAX_HELPER_USAGES:
                return entries
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "#", "*", "/*")):
                continue
            for name in wanted - used:
                if f"{name}(" not in stripped and f"{name}<" not in stripped:
                    continue
                if re.search(rf"\b(?:def|class)\s+{re.escape(name)}\b", stripped):
                    continue  # this is the declaration, not a call
                used.add(name)
                entries.append(f"{stripped[:_MAX_LINE_CHARS]}  (in {file_name})")
                break
    return entries


def _unique(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
