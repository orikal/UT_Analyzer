"""Lightweight syntax highlighting for generated test previews.

A regex-based :class:`QSyntaxHighlighter` that covers the languages the
adapters support (``cpp``, ``python``, ``java``, ``csharp``, ``go``,
``javascript``). It is intentionally heuristic: the goal is readability of a
short test snippet, not a full parser.
"""

from __future__ import annotations

import re

from PySide6.QtGui import (
    QColor,
    QFont,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)

# ---- palette (light theme, GitHub/VS-like) ---------------------------------

COLOR_COMMENT = "#1a7f37"      # green
COLOR_TEST_MACRO = "#8250df"   # purple  (TEST_F, EXPECT_*, @pytest...)
COLOR_TYPE = "#1f7a8c"         # teal
COLOR_FUNCTION = "#8a5a00"     # amber/brown
COLOR_KEYWORD = "#0b5cd5"      # blue
COLOR_STRING = "#a31515"       # dark red
COLOR_NUMBER = "#098658"       # dark green-cyan
COLOR_PREPROC = "#af00db"      # violet
COLOR_EDITOR_BG = "#fbfbfd"
COLOR_EDITOR_FG = "#1f2328"

CODE_EDITOR_STYLE = (
    f"QPlainTextEdit {{ background: {COLOR_EDITOR_BG}; color: {COLOR_EDITOR_FG}; "
    "border: 1px solid #d8dee4; }"
)

_COMMON_KEYWORDS = (
    "if else for while do return break continue switch case default "
    "try catch finally throw throws new delete this null true false"
).split()

_KEYWORDS: dict[str, list[str]] = {
    "cpp": _COMMON_KEYWORDS
    + (
        "class struct union enum namespace template typename using typedef public "
        "private protected virtual override final explicit friend inline static "
        "const constexpr consteval mutable volatile operator sizeof nullptr "
        "static_cast dynamic_cast const_cast reinterpret_cast noexcept decltype "
        "auto goto extern register thread_local co_await co_return co_yield "
        "concept requires and or not"
    ).split(),
    "csharp": _COMMON_KEYWORDS
    + (
        "class struct interface enum namespace using public private protected "
        "internal virtual override abstract sealed static readonly const async "
        "await var out ref in is as base params partial yield lock get set event "
        "delegate where operator implicit explicit checked unchecked nameof typeof"
    ).split(),
    "java": _COMMON_KEYWORDS
    + (
        "class interface enum package import public private protected abstract "
        "final static synchronized transient volatile native strictfp implements "
        "extends super instanceof var record sealed permits yield assert"
    ).split(),
    "javascript": _COMMON_KEYWORDS
    + (
        "class extends import export from as const let var function async await "
        "yield of in instanceof typeof void delete static get set super undefined "
        "interface type enum implements readonly declare namespace module public "
        "private protected abstract satisfies keyof infer"
    ).split(),
    "go": (
        "package import func var const type struct interface map chan go defer "
        "select range if else for switch case default return break continue "
        "fallthrough goto nil true false make new len cap append copy panic recover"
    ).split(),
    "python": (
        "def class return if elif else for while break continue pass import from "
        "as with try except finally raise assert lambda yield global nonlocal del "
        "in is not and or None True False async await match case self cls"
    ).split(),
}

_TYPES: dict[str, list[str]] = {
    "cpp": (
        "void bool char short int long float double signed unsigned wchar_t "
        "char8_t char16_t char32_t size_t ssize_t ptrdiff_t int8_t int16_t "
        "int32_t int64_t uint8_t uint16_t uint32_t uint64_t std string vector "
        "map unordered_map set unordered_set pair tuple array list deque queue "
        "stack optional variant any shared_ptr unique_ptr weak_ptr function "
        "ostream istream stringstream exception"
    ).split(),
    "csharp": (
        "void bool byte sbyte char short ushort int uint long ulong float double "
        "decimal string object dynamic Task List Dictionary IEnumerable IList "
        "HashSet Nullable Span Memory Exception Guid DateTime TimeSpan"
    ).split(),
    "java": (
        "void boolean byte char short int long float double String Object List "
        "ArrayList Map HashMap Set HashSet Optional Stream Integer Long Double "
        "Boolean Character Exception RuntimeException Throwable Class"
    ).split(),
    "javascript": (
        "string number boolean object symbol bigint any unknown never void "
        "Array Object Promise Map Set WeakMap WeakSet Date RegExp Error JSON "
        "Record Partial Readonly Pick Omit"
    ).split(),
    "go": (
        "bool string int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 "
        "uintptr byte rune float32 float64 complex64 complex128 error any "
        "testing Context"
    ).split(),
    "python": (
        "int float str bool bytes bytearray list dict set frozenset tuple object "
        "type complex range Exception BaseException ValueError TypeError KeyError "
        "IndexError RuntimeError Any Optional Union List Dict Set Tuple Callable "
        "Iterable Iterator Sequence Mapping Path Mock MagicMock"
    ).split(),
}

# Test-framework entry points / macros -> purple.
_TEST_MACRO_PATTERNS = (
    # GoogleTest / GoogleMock
    r"\bTYPED_TEST(?:_SUITE|_SUITE_P|_P)?\b",
    r"\bTEST(?:_F|_P)?\b",
    r"\bINSTANTIATE_(?:TEST_SUITE_P|TEST_CASE_P|TYPED_TEST_SUITE_P)\b",
    r"\b(?:EXPECT|ASSERT)_[A-Z0-9_]+\b",
    r"\bMOCK_(?:CONST_)?METHOD[0-9A-Z_]*\b",
    r"\b(?:ON_CALL|EXPECT_CALL|SCOPED_TRACE|FRIEND_TEST|RUN_ALL_TESTS|SUCCEED"
    r"|ADD_FAILURE|GTEST_SKIP|FAIL)\b",
    # Catch2 / doctest / CppUnit
    r"\b(?:TEST_CASE|SECTION|SCENARIO|GIVEN|WHEN|THEN|REQUIRE|CHECK|SUBCASE)[A-Z0-9_]*\b",
    # xUnit-family attributes / annotations (C#, Java)
    r"\b(?:Fact|Theory|InlineData|MemberData|SetUp|TearDown|TestFixture|TestMethod"
    r"|TestClass|BeforeEach|AfterEach|BeforeAll|AfterAll)\b",
    # JS/TS runners
    r"\b(?:describe|it|test|expect|beforeEach|afterEach|beforeAll|afterAll)\b(?=\s*\()",
)

_PY_TEST_MACRO_PATTERNS = (
    r"@[A-Za-z_][\w.]*",
    r"\bself\.assert[A-Za-z]*\b",
    r"\bpytest\.[A-Za-z_]\w*\b",
    r"\b(?:setUp|tearDown|setUpClass|tearDownClass)\b",
)


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


def _word_pattern(words: list[str]) -> str:
    unique = sorted(set(words), key=len, reverse=True)
    return r"\b(?:" + "|".join(re.escape(w) for w in unique) + r")\b"


_ALIASES = {
    "c": "cpp",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "h": "cpp",
    "hpp": "cpp",
    "hxx": "cpp",
    "cs": "csharp",
    "c#": "csharp",
    "py": "python",
    "js": "javascript",
    "ts": "javascript",
    "jsx": "javascript",
    "tsx": "javascript",
    "typescript": "javascript",
    "golang": "go",
}


def normalize_language(language: str | None) -> str:
    """Map an adapter ``language_id`` / file suffix to a highlighter dialect."""
    lang = (language or "").strip().lower().lstrip(".")
    lang = _ALIASES.get(lang, lang)
    return lang if lang in _KEYWORDS else ""


class CodeHighlighter(QSyntaxHighlighter):
    """Regex syntax highlighter for short generated test snippets."""

    def __init__(self, document: QTextDocument, language: str | None) -> None:
        super().__init__(document)
        self.language = normalize_language(language)
        self._rules: list[tuple[re.Pattern[str], int, QTextCharFormat]] = []
        # (opening delimiter, closing delimiter, format) - regions may span blocks
        self._multiline: list[tuple[re.Pattern[str], re.Pattern[str], QTextCharFormat]] = []
        self._build_rules()

    # ---- rule construction -------------------------------------------------

    def _add(self, pattern: str, fmt: QTextCharFormat, *, group: int = 0) -> None:
        self._rules.append((re.compile(pattern), group, fmt))

    def _build_rules(self) -> None:
        lang = self.language
        if not lang:
            return

        comment_fmt = _fmt(COLOR_COMMENT, italic=True)
        string_fmt = _fmt(COLOR_STRING)
        keyword_fmt = _fmt(COLOR_KEYWORD, bold=True)
        type_fmt = _fmt(COLOR_TYPE)
        func_fmt = _fmt(COLOR_FUNCTION)
        macro_fmt = _fmt(COLOR_TEST_MACRO, bold=True)
        number_fmt = _fmt(COLOR_NUMBER)

        # Order matters: later rules repaint over earlier ones.
        types = _TYPES.get(lang, [])
        if types:
            self._add(_word_pattern(types), type_fmt)
        # PascalCase and `*_t` identifiers read as user-defined types.
        self._add(r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*\b", type_fmt)
        self._add(r"\b[a-z_]\w*_t\b", type_fmt)

        self._add(_word_pattern(_KEYWORDS[lang]), keyword_fmt)

        # Calls and definitions: identifier directly followed by "(".
        self._add(r"\b([A-Za-z_]\w*)\s*(?=\()", func_fmt, group=1)
        if lang == "python":
            self._add(r"\bdef\s+([A-Za-z_]\w*)", func_fmt, group=1)
        elif lang == "go":
            self._add(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", func_fmt, group=1)

        macros = _PY_TEST_MACRO_PATTERNS if lang == "python" else _TEST_MACRO_PATTERNS
        for pattern in macros:
            self._add(pattern, macro_fmt)
        if lang == "python":
            self._add(r"\bdef\s+(test\w*)", macro_fmt, group=1)

        self._add(
            r"\b(?:0[xXbB][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)[uUlLfF]*\b",
            number_fmt,
        )

        if lang == "cpp":
            self._add(r"^\s*#\s*\w+", _fmt(COLOR_PREPROC))

        # Strings after identifiers so quoted text is not mistaken for code.
        if lang == "python":
            self._add(r"[rRbBfFuU]{0,2}'(?:\\.|[^'\\])*'", string_fmt)
            self._add(r'[rRbBfFuU]{0,2}"(?:\\.|[^"\\])*"', string_fmt)
        else:
            self._add(r"'(?:\\.|[^'\\])*'", string_fmt)
            self._add(r'(?:u8|[LuU])?"(?:\\.|[^"\\])*"', string_fmt)
            if lang in {"javascript", "go"}:
                self._add(r"`[^`]*`", string_fmt)

        # Line comments win over everything else on the line.
        if lang == "python":
            self._add(r"#[^\n]*", comment_fmt)
            self._multiline.append((re.compile('"""'), re.compile('"""'), string_fmt))
            self._multiline.append((re.compile("'''"), re.compile("'''"), string_fmt))
        else:
            self._add(r"//[^\n]*", comment_fmt)
            self._multiline.append((re.compile(r"/\*"), re.compile(r"\*/"), comment_fmt))

    # ---- highlighting ------------------------------------------------------

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
        self.setCurrentBlockState(-1)
        if not self.language:
            return

        for pattern, group, fmt in self._rules:
            for match in pattern.finditer(text):
                start, end = match.span(group)
                if start >= 0 and end > start:
                    self.setFormat(start, end - start, fmt)

        self._highlight_multiline(text)

    def _highlight_multiline(self, text: str) -> None:
        """Format ``/* */`` comments and Python triple-quoted strings."""
        if not self._multiline:
            return

        previous = self.previousBlockState()
        active = previous if 0 <= previous < len(self._multiline) else -1
        pos = 0
        length = len(text)

        while pos <= length:
            if active >= 0:
                closer, fmt = self._multiline[active][1], self._multiline[active][2]
                end_match = closer.search(text, pos)
                if end_match is None:
                    self.setFormat(pos, length - pos, fmt)
                    self.setCurrentBlockState(active)
                    return
                self.setFormat(pos, end_match.end() - pos, fmt)
                pos = end_match.end()
                active = -1
                continue

            best: tuple[int, re.Match[str]] | None = None
            for index, (opener, _closer, _fmt_) in enumerate(self._multiline):
                match = opener.search(text, pos)
                if match is not None and (best is None or match.start() < best[1].start()):
                    best = (index, match)
            if best is None:
                return
            active, match = best
            fmt = self._multiline[active][2]
            self.setFormat(match.start(), match.end() - match.start(), fmt)
            pos = match.end()


def attach_code_highlighter(editor, language: str | None) -> CodeHighlighter | None:
    """Attach a highlighter to ``editor`` (a ``QPlainTextEdit``) if supported.

    The highlighter is parented to the editor's document, so it lives as long as
    the editor does.
    """
    if not normalize_language(language):
        return None
    highlighter = CodeHighlighter(editor.document(), language)
    editor.setStyleSheet(CODE_EDITOR_STYLE)
    return highlighter
