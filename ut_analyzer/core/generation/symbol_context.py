"""Collects the API a source file is connected to, for grounding the LLM prompt.

The generator embeds the result in the prompt so the model knows the exact
variables, constants, enums, types, collaborators (readers, publishers, ...)
and function signatures the code under test depends on, instead of inventing
them.

Everything here is best-effort and regex based: a dependency file is resolved
against the repository scan, condensed to declarations only (function bodies
are dropped) and truncated to a character budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ...models import ScanResult
from ...utils.fs import read_text_safe

DEFAULT_MAX_CONTEXT_FILES = 10
DEFAULT_MAX_CHARS_PER_FILE = 2500
DEFAULT_MAX_CONTEXT_CHARS = 12000
_MAX_INDEX_SYMBOLS = 300

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "case", "do",
    "else", "new", "delete", "throw", "typedef", "struct", "class", "enum",
    "union", "namespace", "template", "public", "private", "protected",
    "static", "const", "constexpr", "inline", "virtual", "explicit", "using",
    "operator", "void", "int", "bool", "char", "float", "double", "auto",
    "unsigned", "signed", "long", "short", "true", "false", "null", "nullptr",
    "import", "from", "def", "self", "assert", "print", "and", "or", "not",
}

_CONTAINER_RE = re.compile(
    r"^\s*(?:template\b|namespace\b|class\b|struct\b|union\b|enum\b|interface\b"
    r"|record\b|extern\s+\"C\"|typedef\s+struct\b|typedef\s+enum\b)"
)
_SIGNATURE_END_RE = re.compile(
    r"\)\s*(?:const\b|noexcept\b|override\b|final\b|throw\s*\([^)]*\)|:\s*[^;{]*)*\s*$"
)


@dataclass
class ContextFile:
    """One dependency file, condensed to its declarations."""

    name: str
    declarations: str
    include: str = ""  # exact include/import directive that makes its symbols visible
    path: Path | None = None  # real location on disk, for the prompt's file map


@dataclass
class ConstructorInfo:
    """One constructor the tests are allowed to call."""

    type_name: str
    signature: str
    file: str = ""
    include: str = ""

    def arg_bounds(self) -> tuple[int, int]:
        """Minimum and maximum number of arguments this constructor accepts."""
        return _arg_bounds(_signature_params(self.signature))


@dataclass
class SymbolContext:
    """Declarations and symbol index gathered from a source file's dependencies."""

    files: list[ContextFile] = field(default_factory=list)
    symbols: list[tuple[str, str, str]] = field(default_factory=list)
    constructors: list[ConstructorInfo] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.files and not self.symbols

    def file_paths(self) -> list[Path]:
        """Real paths of the dependency files, in the order they were collected."""
        return [item.path for item in self.files if item.path is not None]

    def include_for(self, file_name: str) -> str:
        for item in self.files:
            if item.name == file_name:
                return item.include
        return ""

    def to_prompt_text(self) -> str:
        blocks: list[str] = []
        if self.symbols:
            lines = []
            for kind, name, file in self.symbols:
                include = self.include_for(file)
                where = f"declared in {file}"
                if include:
                    where += f" -> requires `{include}`"
                lines.append(f"- {kind} `{name}` ({where})")
            blocks.append(
                "Symbols the code under test is connected to:\n" + "\n".join(lines)
            )
        includes = [item for item in self.files if item.include]
        if includes:
            lines = [f"- `{item.name}` -> `{item.include}`" for item in includes]
            blocks.append(
                "MANDATORY include/import lines. Whenever a test (or the UT header) uses a "
                "type, constant, enum, macro or function declared in one of these files, the "
                "matching line below MUST be present in the file that uses it. Never use a "
                "symbol whose file is not included/imported:\n" + "\n".join(lines)
            )
        for item in self.files:
            blocks.append(
                f"Declarations from `{item.name}`:\n```\n{item.declarations}\n```"
            )
        return "\n\n".join(blocks)


def collect_symbol_context(
    source: Path,
    language: str,
    source_code: str,
    scan: ScanResult | None,
    *,
    related_contents: list[str] | None = None,
    skip_paths: set[Path] | None = None,
    declarations: dict[str, str] | None = None,
    max_files: int = 0,
    max_chars_per_file: int = 0,
    max_total_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
) -> SymbolContext:
    """Gather declarations from the files ``source`` depends on.

    ``max_files`` and ``max_chars_per_file`` default to 0, meaning "scale them
    with ``max_total_chars``" so a large context window pulls in more
    dependencies, each with more of its declarations.

    ``declarations`` is the condensed-declaration cache built while the
    repository was indexed; when a file is in it, it is not read or condensed
    again.
    """
    context = SymbolContext()
    if scan is None or max_total_chars <= 0:
        return context
    if max_files <= 0:
        max_files = max(
            DEFAULT_MAX_CONTEXT_FILES, max_total_chars // DEFAULT_MAX_CHARS_PER_FILE
        )
    if max_chars_per_file <= 0:
        max_chars_per_file = max(
            DEFAULT_MAX_CHARS_PER_FILE, max_total_chars // DEFAULT_MAX_CONTEXT_FILES
        )

    index = _build_index(scan)
    seed_text = "\n".join([source_code, *(related_contents or [])])
    skip = {source.resolve(), *(p.resolve() for p in (skip_paths or set()))}

    resolved = _resolve_dependencies(seed_text, language, index, skip)
    # Second level: dependencies of the direct dependencies, but only the ones
    # whose name is actually mentioned by the code under test.
    second: list[Path] = []
    for path in list(resolved)[:max_files]:
        content = _declarations_for(path, language, declarations)
        if not content:
            continue
        for candidate in _resolve_dependencies(content, language, index, skip | set(resolved)):
            if _mentions(seed_text, candidate.stem) and candidate not in second:
                second.append(candidate)
    ordered = resolved + second

    total = 0
    for path in ordered:
        if len(context.files) >= max_files or total >= max_total_chars:
            break
        declaration_text = _declarations_for(path, language, declarations)
        if not declaration_text:
            continue
        condensed = declaration_text[:max_chars_per_file]
        if not condensed.strip():
            continue
        name = _display_name(path, scan.root)
        include = _include_directive(path, language, scan.root, index)
        context.files.append(
            ContextFile(name=name, declarations=condensed, include=include, path=path)
        )
        context.symbols.extend(
            (kind, symbol, name) for kind, symbol in _extract_symbols(condensed, language)
        )
        context.constructors.extend(
            extract_constructors(condensed, language, file_name=name, include=include)
        )
        total += len(condensed)

    context.symbols = _rank_symbols(context.symbols, seed_text)
    return context


def _declarations_for(
    path: Path, language: str, cache: dict[str, str] | None
) -> str:
    """Condensed declarations of *path*, from the scan cache when available."""
    if cache is not None:
        cached = cache.get(str(path))
        if cached is not None:
            return cached
    content = read_text_safe(path)
    if not content:
        return ""
    return _condense(content, language)


# ---- dependency resolution -------------------------------------------------


def _build_index(scan: ScanResult) -> dict[str, list[Path]]:
    """Map file name and stem (lowercase) to the scanned paths that match."""
    index: dict[str, list[Path]] = {}
    for scanned in scan.files:
        if scanned.is_test:
            continue
        for key in (scanned.path.name.lower(), scanned.path.stem.lower()):
            index.setdefault(key, []).append(scanned.path)
    return index


def _resolve_dependencies(
    content: str,
    language: str,
    index: dict[str, list[Path]],
    skip: set[Path],
) -> list[Path]:
    """Resolve the imports/includes (and referenced type names) to repo files."""
    resolved: list[Path] = []
    seen: set[Path] = set(skip)
    for ref in _dependency_refs(content, language):
        path = _resolve_ref(ref, index)
        if path is None or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        resolved.append(path)
    for name in _referenced_type_names(content):
        for path in index.get(name.lower(), []):
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            resolved.append(path)
    return resolved


def _dependency_refs(content: str, language: str) -> list[str]:
    """File references declared by include/import statements."""
    refs: list[str] = []
    if language == "python":
        for match in re.finditer(
            r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", content, re.MULTILINE
        ):
            module = match.group(1) or match.group(2) or ""
            module = module.lstrip(".")
            if module:
                refs.append(module.replace(".", "/") + ".py")
        return refs
    if language in ("java", "csharp"):
        for match in re.finditer(
            r"^\s*(?:import\s+(?:static\s+)?|using\s+)([\w.]+)", content, re.MULTILINE
        ):
            refs.append(match.group(1).split(".")[-1])
        return refs
    if language == "javascript":
        for match in re.finditer(
            r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]""", content
        ):
            refs.append(match.group(1))
        return refs
    if language == "go":
        for match in re.finditer(r'"([\w./-]+)"', content):
            refs.append(match.group(1))
        return refs
    for match in re.finditer(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', content, re.MULTILINE):
        refs.append(match.group(1))
    return refs


def _referenced_type_names(content: str) -> list[str]:
    """Capitalized identifiers used as types/collaborators, most used first."""
    counts: dict[str, int] = {}
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,})\b", content):
        name = match.group(1)
        if name in _KEYWORDS:
            continue
        counts[name] = counts.get(name, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda item: -item[1])][:40]


def _resolve_ref(ref: str, index: dict[str, list[Path]]) -> Path | None:
    ref = ref.replace("\\", "/").strip()
    base = ref.rsplit("/", 1)[-1].lower()
    candidates = index.get(base) or index.get(Path(base).stem.lower()) or []
    if not candidates:
        return None
    if len(candidates) > 1 and "/" in ref:
        wanted = ref.lower()
        for candidate in candidates:
            if candidate.as_posix().lower().endswith(wanted):
                return candidate
    return min(candidates, key=lambda p: len(p.as_posix()))


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def _display_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


# ---- include / import directives -------------------------------------------

_HEADER_SUFFIXES = (".h", ".hpp", ".hxx", ".hh")


def _include_directive(
    path: Path,
    language: str,
    root: Path,
    index: dict[str, list[Path]] | None = None,
) -> str:
    """The exact line that makes the symbols of *path* visible to a test file."""
    if language in ("cpp", "c"):
        header = path
        if path.suffix.lower() not in _HEADER_SUFFIXES:
            header = _sibling_header(path, index)
            if header is None:
                return ""
        return f'#include "{header.name}"'
    if language == "python":
        module = _display_name(path, root)
        if not module.endswith(".py"):
            return ""
        module = module[: -len(".py")].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return f"from {module} import <symbol>"
    if language == "javascript":
        module = _display_name(path, root).rsplit(".", 1)[0]
        return f"import {{ <symbol> }} from '{module}'"
    if language == "java":
        return f"import <package>.{path.stem};"
    if language == "csharp":
        return f"using <namespace>;  // namespace declaring {path.stem}"
    return ""


def _sibling_header(path: Path, index: dict[str, list[Path]] | None) -> Path | None:
    """Header declaring the API of a C/C++ implementation file, if there is one."""
    for suffix in _HEADER_SUFFIXES:
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
        if index is not None:
            for known in index.get(candidate.name.lower(), []):
                return known
    return None


def include_directive_for(path: Path, language: str, root: Path | None = None) -> str:
    """Public helper: the include/import line that exposes *path*'s declarations."""
    return _include_directive(path, language, root or path.parent)


# ---- condensation ----------------------------------------------------------


def _condense(content: str, language: str) -> str:
    if language == "python":
        return _condense_python(content)
    return _condense_braces(content)


def condense_declarations(content: str, language: str) -> str:
    """Public entry point: keep declarations, drop function bodies."""
    return _condense(content, language)


def _condense_braces(content: str) -> str:
    """Keep declarations; replace function bodies with their signature."""
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    out: list[str] = []
    depth = 0
    skip_depth: int | None = None
    pending_signature = False
    for raw in content.splitlines():
        line = raw.strip()
        indent = raw[: len(raw) - len(raw.lstrip())]
        opens, closes = line.count("{"), line.count("}")
        if skip_depth is not None:
            depth += opens - closes
            if depth <= skip_depth:
                skip_depth = None
            continue
        if not line or line.startswith("//"):
            continue
        if line == "{" and pending_signature and out:
            out[-1] = out[-1].rstrip() + ";"
            skip_depth = depth
            depth += 1
            pending_signature = False
            continue
        if opens > closes and "(" in line and not _CONTAINER_RE.match(line):
            out.append(indent + line.split("{", 1)[0].rstrip() + ";")
            skip_depth = depth
            depth += opens - closes
            pending_signature = False
            continue
        out.append(raw.rstrip())
        depth += opens - closes
        pending_signature = (
            "(" in line
            and not line.endswith((";", ",", "{", "}"))
            and _SIGNATURE_END_RE.search(line) is not None
        )
    return "\n".join(out)


def _condense_python(content: str) -> str:
    """Keep module/class level declarations; drop function bodies."""
    out: list[str] = []
    skip_indent: int | None = None
    pending_def_indent: int | None = None
    for raw in content.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.expandtabs(4).lstrip())
        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        out.append(raw.rstrip())
        if pending_def_indent is not None:
            if stripped.endswith(":"):
                skip_indent = pending_def_indent
                pending_def_indent = None
            continue
        if re.match(r"(?:async\s+)?def\s+\w+", stripped):
            if stripped.endswith(":"):
                skip_indent = indent
            else:
                pending_def_indent = indent
    return "\n".join(out)


# ---- symbol index ----------------------------------------------------------

_BRACE_SYMBOL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("enum", r"\benum(?:\s+(?:class|struct))?\s+(\w+)"),
    ("type", r"\b(?:class|struct|interface|record)\s+(\w+)"),
    ("alias", r"\busing\s+(\w+)\s*="),
    ("macro", r"^\s*#\s*define\s+(\w+)"),
    (
        "constant",
        r"^\s*(?:static\s+|const\s+|constexpr\s+|readonly\s+|final\s+)+"
        r"[\w:<>,\[\]\s\*&]+?\b(\w+)\s*(?:=|\[)",
    ),
    ("function", r"^[\w:<>,\[\]\s\*&]+?\b(\w+)\s*\([^;{)]*\)\s*(?:const\b)?[^;{]*;"),
)

_PYTHON_SYMBOL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("type", r"^\s*class\s+(\w+)"),
    ("function", r"^\s*(?:async\s+)?def\s+(\w+)"),
    ("constant", r"^([A-Z_][A-Z0-9_]*)\s*[:=]"),
)

_INCLUDE_GUARD_RE = re.compile(r"^_*[A-Z0-9_]+_(?:H|HPP|HXX|INCLUDED)_*$")


def _extract_symbols(declarations: str, language: str) -> list[tuple[str, str]]:
    patterns = _PYTHON_SYMBOL_PATTERNS if language == "python" else _BRACE_SYMBOL_PATTERNS
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in patterns:
        for match in re.finditer(pattern, declarations, re.MULTILINE):
            name = match.group(1)
            if not name or name in _KEYWORDS or name in seen:
                continue
            if language != "python" and name.startswith("_"):
                continue
            if kind == "macro" and _INCLUDE_GUARD_RE.match(name):
                continue
            seen.add(name)
            found.append((kind, name))
    return found


def _rank_symbols(
    symbols: list[tuple[str, str, str]], seed_text: str
) -> list[tuple[str, str, str]]:
    """Symbols actually mentioned by the code under test come first."""
    used = [item for item in symbols if _mentions(seed_text, item[1])]
    rest = [item for item in symbols if item not in used]
    return (used + rest)[:_MAX_INDEX_SYMBOLS]


# ---- constructors ----------------------------------------------------------

_CLASS_HEAD_RE = re.compile(
    r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(?:\w+_(?:API|EXPORT)\s+)?(\w+)\b"
)
_OUT_OF_LINE_CTOR_RE = re.compile(r"^\s*(\w+)\s*::\s*(\w+)\s*\(([^;{}]*)\)")
_PY_CLASS_RE = re.compile(r"^(\s*)class\s+(\w+)")
_PY_INIT_RE = re.compile(r"^\s*def\s+__init__\s*\(([^)]*)\)")


def extract_constructors(
    declarations: str,
    language: str,
    *,
    file_name: str = "",
    include: str = "",
) -> list[ConstructorInfo]:
    """Constructors declared in *declarations* (already condensed text)."""
    if language == "python":
        found = _extract_constructors_python(declarations)
    else:
        found = _extract_constructors_braces(declarations)
    return [
        ConstructorInfo(
            type_name=type_name, signature=signature, file=file_name, include=include
        )
        for type_name, signature in found
    ]


def _extract_constructors_braces(declarations: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    stack: list[tuple[str, int]] = []  # (class name, brace depth it was opened at)
    pending: str | None = None
    depth = 0
    for raw in declarations.splitlines():
        line = raw.strip()
        if not line:
            continue
        current = stack[-1][0] if stack else None
        if current:
            match = re.match(
                r"^(?:explicit\s+|constexpr\s+|inline\s+)*"
                + re.escape(current)
                + r"\s*\(([^;{}]*)\)",
                line,
            )
            if match is not None and re.search(r"=\s*delete\b", line) is None:
                _add_constructor(found, current, match.group(1))
        out_of_line = _OUT_OF_LINE_CTOR_RE.match(line)
        if out_of_line is not None and out_of_line.group(1) == out_of_line.group(2):
            _add_constructor(found, out_of_line.group(1), out_of_line.group(3))
        head = _CLASS_HEAD_RE.match(line)
        if pending is not None and "{" in line:
            stack.append((pending, depth))
            pending = None
        elif head is not None and not line.endswith(";"):
            if "{" in line:
                stack.append((head.group(1), depth))
            else:
                pending = head.group(1)
        depth += line.count("{") - line.count("}")
        while stack and depth <= stack[-1][1]:
            stack.pop()
    return found


def _extract_constructors_python(declarations: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    current: str | None = None
    class_indent = 0
    for raw in declarations.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.expandtabs(4).lstrip())
        if current is not None and indent <= class_indent:
            current = None
        head = _PY_CLASS_RE.match(raw)
        if head is not None:
            current = head.group(2)
            class_indent = len(head.group(1))
            continue
        if current is None:
            continue
        init = _PY_INIT_RE.match(raw)
        if init is not None:
            params = split_args(init.group(1))
            if params and params[0].split(":")[0].strip() in ("self", "cls"):
                params = params[1:]
            _add_constructor(found, current, ", ".join(params))
    return found


def _add_constructor(found: list[tuple[str, str]], type_name: str, args: str) -> None:
    signature = f"{type_name}({args.strip()})"
    if (type_name, signature) not in found:
        found.append((type_name, signature))


def constructors_prompt_text(
    constructors: list[ConstructorInfo], max_types: int = 40
) -> str:
    """Prompt section listing the only constructors the tests may call."""
    if not constructors:
        return ""
    grouped: dict[str, list[ConstructorInfo]] = {}
    for item in constructors:
        grouped.setdefault(item.type_name, []).append(item)
    lines: list[str] = []
    for type_name, items in list(grouped.items())[:max_types]:
        where = next((item.file for item in items if item.file), "")
        include = next((item.include for item in items if item.include), "")
        header = f"- `{type_name}`"
        if where:
            header += f" (declared in {where}"
            header += f", requires `{include}`)" if include else ")"
        lines.append(header + ":")
        seen: set[str] = set()
        for item in items:
            if item.signature in seen:
                continue
            seen.add(item.signature)
            low, high = item.arg_bounds()
            count = f"{low}" if low == high else f"{low}-{high if high < 99 else 'N'}"
            lines.append(f"    * {item.signature}   [{count} argument(s)]")
    return (
        "Constructors that actually exist (the ONLY ways you may create these objects):\n"
        + "\n".join(lines)
        + "\n\nRules: create an object only with one of the signatures above, passing exactly "
        "that number of arguments with those exact types. Never invent a constructor, never "
        "add or drop arguments, and never construct a type whose constructor is not shown to "
        "you - if a type is not listed here and no constructor for it appears in the code "
        "above, either default-construct it (`Type obj;` / `Type()`) or obtain it the way the "
        "production code does (factory, manager, builder), and add a `TODO: ` comment on the "
        "line above when you are not sure."
    )


def _signature_params(signature: str) -> str:
    start = signature.find("(")
    end = signature.rfind(")")
    if start == -1 or end <= start:
        return ""
    return signature[start + 1: end]


def _arg_bounds(args: str) -> tuple[int, int]:
    params = split_args(args)
    if not params or params == ["void"]:
        return (0, 0)
    if any(part.startswith(("...", "*", "**")) or part == "..." for part in params):
        return (max(0, len([p for p in params if not p.startswith(("...", "*", "**"))])), 99)
    required = len([part for part in params if "=" not in part])
    return (required, len(params))


def split_args(args: str) -> list[str]:
    """Split a parameter list on top-level commas."""
    parts: list[str] = []
    current = ""
    depth = 0
    for char in args:
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth -= 1
        if char == "," and depth <= 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


# ---- grounding guards ------------------------------------------------------

_CPP_CONSTRUCTIONS = (
    # Type name(args);
    re.compile(r"^[ \t]*(?:const\s+)?([A-Z]\w*)\s+(?!\w*\s*=)\w+\s*\((?P<args>[^;]*)\)\s*;"),
    # new Type(args)
    re.compile(r"\bnew\s+([A-Z]\w*)\s*\((?P<args>[^;)]*)\)"),
    # std::make_unique<Type>(args) / make_shared<Type>(args)
    re.compile(
        r"\bmake_(?:unique|shared)\s*<\s*(?:\w+::)*([A-Z]\w*)\s*>\s*\((?P<args>[^;)]*)\)"
    ),
)
_PY_CONSTRUCTION = re.compile(r"\b([A-Z]\w*)\s*\((?P<args>[^;)]*)\)")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")
_MAX_ADDED_INCLUDES = 10


def flag_unknown_constructions(
    code: str, constructors: list[ConstructorInfo], language: str
) -> str:
    """Mark object creations whose constructor does not exist with a ``TODO:`` comment.

    Models keep inventing constructor signatures. When a construction cannot match
    any declared constructor of that type, a ``TODO`` line is inserted above it so
    the reviewer sees exactly what must be fixed (the code itself is not rewritten).
    """
    if not code or not constructors:
        return code
    known: dict[str, list[ConstructorInfo]] = {}
    for item in constructors:
        known.setdefault(item.type_name, []).append(item)
    comment = "#" if language == "python" else "//"
    patterns = (_PY_CONSTRUCTION,) if language == "python" else _CPP_CONSTRUCTIONS

    out: list[str] = []
    for line in code.splitlines():
        problem = ""
        if not line.strip().startswith(comment):
            for pattern in patterns:
                for match in pattern.finditer(line):
                    problem = _construction_problem(
                        match.group(1), match.group("args"), known
                    )
                    if problem:
                        break
                if problem:
                    break
        if problem and not (out and out[-1].strip() == f"{comment} TODO: {problem}"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{comment} TODO: {problem}")
        out.append(line)
    return "\n".join(out) + ("\n" if code.endswith("\n") else "")


def _construction_problem(
    type_name: str, args: str, known: dict[str, list[ConstructorInfo]]
) -> str:
    """Message describing why this construction cannot compile (empty = looks fine)."""
    candidates = known.get(type_name)
    if not candidates:
        return ""
    count = len(split_args(args))
    if count == 0:
        return ""  # an implicit default constructor may exist without being declared
    if count == 1 and _IDENTIFIER_RE.match(args.strip()):
        return ""  # possible copy construction
    for item in candidates:
        low, high = item.arg_bounds()
        if low <= count <= high:
            return ""
    available = " | ".join(dict.fromkeys(item.signature for item in candidates))
    return (
        f"{type_name} has no constructor taking {count} argument(s); "
        f"use one of: {available}"
    )


def ensure_symbol_includes(
    text: str, used_code: str, context: SymbolContext, language: str
) -> str:
    """Add the include/import of every dependency symbol the generated code uses."""
    if not text or context.is_empty():
        return text
    missing: list[str] = []
    for _kind, name, file_name in context.symbols:
        directive = context.include_for(file_name)
        if not directive or not _mentions(used_code, name):
            continue
        directive = directive.replace("<symbol>", name)
        if _has_directive(text, directive) or directive in missing:
            continue
        missing.append(directive)
        if len(missing) >= _MAX_ADDED_INCLUDES:
            break
    if not missing:
        return text
    return _insert_directives(text, missing, language)


def _has_directive(text: str, directive: str) -> bool:
    if directive.startswith("#include"):
        header = directive.split('"')[1] if '"' in directive else directive
        return header in text
    if directive.startswith("from "):
        module = directive.split()[1]
        symbol = directive.rsplit(" ", 1)[-1]
        return bool(
            re.search(
                rf"^\s*(?:from\s+{re.escape(module)}\s+import\b.*\b{re.escape(symbol)}\b"
                rf"|import\s+{re.escape(module)}\b)",
                text,
                re.MULTILINE,
            )
        )
    return directive.strip() in text


def _insert_directives(text: str, directives: list[str], language: str) -> str:
    """Insert directives after the existing include/import block."""
    lines = text.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#!")):
            continue
        is_directive = (
            stripped.startswith(("import ", "from "))
            if language == "python"
            else stripped.startswith("#")
        )
        if is_directive:
            insert_at = index + 1
            continue
        break
    updated = lines[:insert_at] + directives + lines[insert_at:]
    return "\n".join(updated) + ("\n" if text.endswith("\n") else "")


