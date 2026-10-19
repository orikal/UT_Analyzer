"""How the project's test fixtures initialize and release themselves.

Some teams do everything in ``SetUp()``/``TearDown()``. Others give the fixture
a real constructor/destructor - very often *declared* in the UT header and
*defined* out-of-line in the UT source file - or a hand-written ``Init()``
method. A generated fixture that ignores the house style does not look like the
rest of the folder and usually does not compile against it either.

This module reads the real files and reports which hooks are used, where their
bodies live, and what they look like. It is used twice: by the UT-folder study
(to teach the model) and by the test template (to scaffold the new fixture).

Everything here is best-effort and regex based.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ...models import FixtureLifecycle, UTCodeSample

#: How many verbatim lifecycle bodies to keep for the prompt.
DEFAULT_MAX_LIFECYCLE_SAMPLES = 8
#: Longest function body we copy verbatim.
MAX_BODY_LINES = 60
#: Longest single line we copy verbatim.
MAX_LINE_CHARS = 200

_BRACE_CLASS_HEAD_RE = re.compile(r"^\s*(?:class|struct)\s+(\w+)")
_PY_CLASS_HEAD_RE = re.compile(r"^\s*class\s+(\w+)")

# `SeekUT::SeekUT(` / `SeekUT::~SeekUT(` - a body defined outside the class.
_CPP_OUT_OF_LINE_RE = re.compile(
    r"^\s*(?P<cls>[A-Za-z_]\w*)\s*::\s*(?P<tilde>~?)(?P=cls)\s*\("
)
# `SeekUT(` / `~SeekUT(` / `explicit SeekUT(` inside the class body.
_CPP_INLINE_RE = re.compile(
    r"^\s*(?:explicit\s+|virtual\s+)*(?P<tilde>~?)(?P<name>[A-Za-z_]\w*)\s*\("
)
# `void SetUp()` / `void SeekUT::TearDown()`, with or without `override`.
_CPP_SETUP_RE = re.compile(
    r"^\s*(?:virtual\s+)?void\s+(?:(?P<owner>[A-Za-z_]\w*)\s*::\s*)?"
    r"(?P<name>SetUp|TearDown|SetUpTestSuite|TearDownTestSuite|"
    r"SetUpTestCase|TearDownTestCase)\s*\("
)
# `void Init()`, `bool InitTest()`, `void SeekUT::Initialize()`.
_CPP_INIT_RE = re.compile(
    r"^\s*(?:virtual\s+)?[\w:<>,\*&]+\s+(?:(?P<owner>[A-Za-z_]\w*)\s*::\s*)?"
    r"(?P<name>Init(?:iali[sz]e)?\w*)\s*\("
)
_PY_INIT_RE = re.compile(r"^\s*def\s+(?P<name>__init__)\s*\(")
_PY_SETUP_RE = re.compile(r"^\s*def\s+(?P<name>setUp|tearDown)\s*\(")

#: Class names that look like a test fixture even when we never saw the base class.
_FIXTURE_NAME_RE = re.compile(r"(?:UT|Test|Tests|Fixture|TestCase)$")

_SETUP_HOOKS = {"SetUp", "SetUpTestSuite", "SetUpTestCase", "setUp"}


def analyze_fixture_lifecycle(
    sources: Sequence[tuple[str, str]],
    language: str,
    fixture_names: Sequence[str] = (),
    *,
    limit: int = DEFAULT_MAX_LIFECYCLE_SAMPLES,
) -> FixtureLifecycle:
    """Learn the fixture lifecycle style from ``(file name, content)`` pairs."""
    known = {name for name in fixture_names if name}
    lifecycle = FixtureLifecycle()
    init_methods: list[str] = []
    seen_bodies: set[str] = set()
    for file_name, content in sources:
        if not content:
            continue
        found = _hooks_in_file(content, language, known)
        if not found:
            continue
        kinds = {hook.kind for hook in found}
        lifecycle.files_with_hooks += 1
        lifecycle.constructor_files += "constructor" in kinds
        lifecycle.destructor_files += "destructor" in kinds
        lifecycle.setup_files += "setup" in kinds
        lifecycle.teardown_files += "teardown" in kinds
        lifecycle.out_of_line_files += any(
            hook.out_of_line and hook.kind in ("constructor", "destructor")
            for hook in found
        )
        for hook in found:
            if hook.kind == "init" and hook.name not in init_methods:
                init_methods.append(hook.name)
            if len(lifecycle.samples) >= limit or not hook.code:
                continue
            if hook.code in seen_bodies:
                continue
            seen_bodies.add(hook.code)
            lifecycle.samples.append(
                UTCodeSample(title=hook.title, code=hook.code, file=file_name)
            )
    lifecycle.init_methods = tuple(init_methods[:4])
    return lifecycle


class _Hook:
    """One lifecycle function found in a file."""

    __slots__ = ("kind", "name", "owner", "out_of_line", "code")

    def __init__(
        self, kind: str, name: str, owner: str, out_of_line: bool, code: str
    ) -> None:
        self.kind = kind
        self.name = name
        self.owner = owner
        self.out_of_line = out_of_line
        self.code = code

    @property
    def title(self) -> str:
        label = {
            "constructor": "the fixture constructor",
            "destructor": "the fixture destructor",
            "setup": "the fixture setup",
            "teardown": "the fixture teardown",
            "init": "a fixture init method",
        }[self.kind]
        if self.out_of_line and self.owner:
            return f"{self.owner}::{self.name}() - {label}"
        if self.owner:
            return f"{self.name}() of {self.owner} - {label}"
        return f"{self.name}() - {label}"


def _hooks_in_file(content: str, language: str, known: set[str]) -> list[_Hook]:
    lines = content.splitlines()
    hooks: list[_Hook] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        hook = (
            _python_hook(lines, index, known)
            if language == "python"
            else _brace_hook(lines, index, known)
        )
        if hook is not None:
            hooks.append(hook)
    return hooks


def _brace_hook(lines: list[str], index: int, known: set[str]) -> _Hook | None:
    line = lines[index]
    if "(" not in line or line.rstrip().endswith(";"):
        return None

    match = _CPP_OUT_OF_LINE_RE.match(line)
    if match is not None and _is_fixture(match.group("cls"), known):
        owner = match.group("cls")
        kind = "destructor" if match.group("tilde") else "constructor"
        return _make_hook(lines, index, kind, owner, owner, out_of_line=True)

    match = _CPP_SETUP_RE.match(line)
    if match is not None:
        owner = match.group("owner") or enclosing_class(lines, index, "cpp")
        if _is_fixture(owner, known) or not owner:
            name = match.group("name")
            kind = "setup" if name in _SETUP_HOOKS else "teardown"
            return _make_hook(
                lines, index, kind, name, owner, out_of_line=bool(match.group("owner"))
            )

    match = _CPP_INLINE_RE.match(line)
    if match is not None:
        name = match.group("name")
        owner = enclosing_class(lines, index, "cpp")
        if name == owner and _is_fixture(owner, known):
            kind = "destructor" if match.group("tilde") else "constructor"
            return _make_hook(lines, index, kind, name, owner, out_of_line=False)

    match = _CPP_INIT_RE.match(line)
    if match is not None:
        owner = match.group("owner") or enclosing_class(lines, index, "cpp")
        if _is_fixture(owner, known):
            return _make_hook(
                lines,
                index,
                "init",
                match.group("name"),
                owner,
                out_of_line=bool(match.group("owner")),
            )
    return None


def _python_hook(lines: list[str], index: int, known: set[str]) -> _Hook | None:
    match = _PY_INIT_RE.match(lines[index]) or _PY_SETUP_RE.match(lines[index])
    if match is None:
        return None
    owner = enclosing_class(lines, index, "python")
    if not _is_fixture(owner, known):
        return None
    name = match.group("name")
    kind = {"__init__": "constructor", "setUp": "setup", "tearDown": "teardown"}[name]
    return _Hook(kind, name, owner, False, dedent(python_body(lines, index)))


def _make_hook(
    lines: list[str],
    index: int,
    kind: str,
    name: str,
    owner: str,
    *,
    out_of_line: bool,
) -> _Hook | None:
    """A hook with its verbatim body; a bodyless declaration teaches nothing."""
    body = brace_body(lines, index)
    if not body:
        return None
    return _Hook(kind, name, owner, out_of_line, dedent(body))


def _is_fixture(name: str, known: set[str]) -> bool:
    if not name:
        return False
    return name in known or _FIXTURE_NAME_RE.search(name) is not None


# ---- shared body readers ---------------------------------------------------
# Also used by ut_study.py; kept here so both learners cut bodies identically.


def enclosing_class(lines: list[str], index: int, language: str) -> str:
    pattern = _PY_CLASS_HEAD_RE if language == "python" else _BRACE_CLASS_HEAD_RE
    for previous in reversed(lines[max(0, index - 120): index]):
        match = pattern.match(previous)
        if match is not None:
            return match.group(1)
    return ""


def brace_body(lines: list[str], index: int) -> list[str]:
    """The `{ ... }` block starting at (or just after) ``index``."""
    picked: list[str] = []
    depth = 0
    opened = False
    for line in lines[index: index + MAX_BODY_LINES]:
        picked.append(line)
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            break
    return picked if opened else []


def python_body(lines: list[str], index: int) -> list[str]:
    """The indented block introduced by the ``def`` line at ``index``."""
    header = lines[index]
    indent = len(header) - len(header.lstrip())
    picked = [header]
    for line in lines[index + 1: index + MAX_BODY_LINES]:
        if not line.strip():
            picked.append("")
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        picked.append(line)
    while picked and not picked[-1].strip():
        picked.pop()
    return picked


def opens_block(lines: list[str], index: int) -> bool:
    """True when the next non-empty line is the opening brace of a body."""
    for follow in lines[index + 1: index + 3]:
        stripped = follow.strip()
        if not stripped:
            continue
        return stripped.startswith("{")
    return False


def dedent(lines: list[str]) -> str:
    kept = [line.rstrip()[:MAX_LINE_CHARS] for line in lines if line.strip()]
    if not kept:
        return ""
    margin = min(len(line) - len(line.lstrip()) for line in kept)
    return "\n".join(line[margin:] for line in kept)
