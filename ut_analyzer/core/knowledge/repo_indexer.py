"""Repository indexer: reads the whole repo and catalogues what it declares.

When the user picks a repository, every source/header file is read once and
condensed to its declarations, so the tool ends up knowing the real structs,
enums (with their exact enumerator names), constants, macros, type aliases,
free functions and collaborator classes (readers, publishers, subscribers,
managers, queues, ...) of the project.

The result is written to a Markdown file inside the repository
(``.ut_analyzer/REPO_KNOWLEDGE.md``) and the relevant part of it is embedded in
every generation prompt, so the model stops inventing types and enum values.

Everything here is best-effort and regex based.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...models import (
    KIND_ALIAS,
    KIND_CLASS,
    KIND_CONSTANT,
    KIND_ENUM,
    KIND_FUNCTION,
    KIND_MACRO,
    KIND_STRUCT,
    TYPE_KINDS,
    RepoEntity,
    RepoKnowledge,
    RepoUsage,
    ScanResult,
    role_for_name,
)
from ...utils.fs import read_text_safe, write_text_atomic
from ...utils.paths import cache_dir
from ..cancellation import raise_if_cancelled
from ..generation.symbol_context import condense_declarations, include_directive_for

KNOWLEDGE_DIR_NAME = ".ut_analyzer"
KNOWLEDGE_FILE_NAME = "REPO_KNOWLEDGE.md"

DEFAULT_MAX_FILES = 1500
MAX_ENTITIES_PER_FILE = 120
MAX_MEMBERS_PER_TYPE = 40
MAX_INIT_EXAMPLES = 2
MAX_INIT_LINES = 8
# Upper bound for the in-memory declaration cache handed to the generator.
MAX_CACHED_DECLARATION_CHARS = 8_000_000
_MAX_INIT_LOOKAHEAD = 14
_PROGRESS_EVERY = 40

_HEADER_SUFFIXES = (".h", ".hpp", ".hxx", ".hh")

_SKIP_MEMBER_WORDS = {"return", "if", "else", "for", "while", "switch", "case", "delete"}
_INCLUDE_GUARD_RE = re.compile(r"^_*[A-Z0-9_]+_(?:H|HPP|HXX|INCLUDED)_*$")

# ---- brace languages -------------------------------------------------------

_ACCESS_RE = re.compile(r"^(public|private|protected)\s*:")
_MACRO_RE = re.compile(r"^#\s*define\s+(\w+)(.*)$")
_NAMESPACE_RE = re.compile(r'^(?:namespace\b|extern\s+"C")')
_ENUM_HEAD_RE = re.compile(
    r"^(?:typedef\s+)?enum(?:\s+(?:class|struct))?\s+(\w+)\s*(:\s*[\w:\s]+?)?\s*(?=\{|;|$)"
)
_CLASS_HEAD_RE = re.compile(
    r"^(?:template\s*<[^>]*>\s*)?(class|struct|union|interface|record)\s+"
    r"(?:\w+_(?:API|EXPORT|DECL)\s+)?(\w+)\b\s*(?:final\b\s*)?(:[^{;]*)?"
)
_USING_ALIAS_RE = re.compile(r"^using\s+(\w+)\s*=\s*([^;]+);")
_TYPEDEF_RE = re.compile(r"^typedef\s+.+?\b(\w+)\s*(?:\[[^\]]*\])?\s*;")
_CONSTANT_RE = re.compile(
    r"^(?:static\s+|const\s+|constexpr\s+|inline\s+|extern\s+|readonly\s+|final\s+)+"
    r"[\w:<>,\[\]\s\*&]*?\b(\w+)\s*(?:=|\{|\[)"
)
_FUNC_DECL_RE = re.compile(
    r"^[\w:<>,\[\]\s\*&]+?\b(\w+)\s*\(([^;{]*)\)\s*(?:const\b)?[^;{]*;$"
)

# ---- python ----------------------------------------------------------------

_PY_CLASS_RE = re.compile(r"^(\s*)class\s+(\w+)\s*(\(([^)]*)\))?\s*:")
_PY_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)([^:]*):")
_PY_ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(.+)$")
_PY_ENUM_BASES = ("Enum", "IntEnum", "StrEnum", "IntFlag", "Flag")


@dataclass
class _Frame:
    entity: RepoEntity | None
    depth: int
    access: str = "public"


def build_repo_knowledge(
    scan: ScanResult | None,
    language: str,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RepoKnowledge:
    """Index every non-test source file of *language* found by the scan."""
    knowledge = RepoKnowledge(
        root=scan.root if scan is not None else None,
        language=language,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if scan is None:
        return knowledge

    files = [item.path for item in scan.source_files(language or None)]
    files = _ordered(files)
    if len(files) > max_files:
        knowledge.skipped_files = len(files) - max_files
        files = files[:max_files]

    merged: dict[tuple[str, str], RepoEntity] = {}
    cached_chars = 0
    for position, path in enumerate(files):
        raise_if_cancelled(cancelled)
        if progress is not None and position % _PROGRESS_EVERY == 0:
            progress(f"Indexing repository ({position}/{len(files)}): {path.name}")
        content = read_text_safe(path)
        if not content:
            continue
        knowledge.indexed_files += 1
        file_name = _display_name(path, scan.root)
        include = include_directive_for(path, language, scan.root)
        declarations = condense_declarations(content, language)
        if cached_chars < MAX_CACHED_DECLARATION_CHARS:
            knowledge.declarations[str(path)] = declarations
            cached_chars += len(declarations)
        for entity in _extract_entities(declarations, language, file_name, include):
            _merge(merged, entity)

    knowledge.entities = list(merged.values())
    collect_init_examples(
        files,
        knowledge.entities,
        language,
        scan.root,
        progress=progress,
        cancelled=cancelled,
    )
    return knowledge


def write_knowledge_markdown(knowledge: RepoKnowledge) -> Path | None:
    """Write the Markdown knowledge file, preferring a folder inside the repo."""
    if knowledge.root is None:
        return None
    content = knowledge.to_markdown()
    folder = ensure_knowledge_dir(knowledge.root)
    if folder is None:
        return None
    target = folder / KNOWLEDGE_FILE_NAME
    try:
        write_text_atomic(target, content, backup=False)
    except OSError:
        return None
    knowledge.markdown_path = target
    return target


def knowledge_markdown_path(root: Path) -> Path:
    """Canonical location of the knowledge file for a repository."""
    return root / KNOWLEDGE_DIR_NAME / KNOWLEDGE_FILE_NAME


def ensure_knowledge_dir(root: Path) -> Path | None:
    """The writable folder that holds every generated Markdown document.

    Prefers ``<root>/.ut_analyzer``; falls back to the user cache directory when
    the repository itself cannot be written to.
    """
    for folder in knowledge_dir_candidates(root):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _write_gitignore(folder)
        except OSError:
            continue
        return folder
    return None


def knowledge_dir_candidates(root: Path) -> list[Path]:
    """Folders that may hold the generated documents for ``root``, best first."""
    fallback = cache_dir() / "knowledge" / f"{root.name}_{abs(hash(str(root))) % 10**8}"
    return [root / KNOWLEDGE_DIR_NAME, fallback]


def _write_gitignore(folder: Path) -> None:
    """Keep the generated folder out of the user's version control."""
    path = folder / ".gitignore"
    if path.exists():
        return
    path.write_text("# Created by UT Analyzer\n*\n", encoding="utf-8")


def _ordered(files: list[Path]) -> list[Path]:
    """Headers first: they carry the declarations the tests must match."""
    return sorted(
        files,
        key=lambda path: (
            0 if path.suffix.lower() in _HEADER_SUFFIXES else 1,
            path.as_posix().lower(),
        ),
    )


def _display_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _merge(merged: dict[tuple[str, str], RepoEntity], entity: RepoEntity) -> None:
    """Keep the richest declaration of a symbol (headers win over sources)."""
    key = (entity.kind, entity.name)
    existing = merged.get(key)
    if existing is None:
        merged[key] = entity
        return
    if len(entity.members) > len(existing.members):
        merged[key] = entity


# ---- extraction ------------------------------------------------------------


def _extract_entities(
    declarations: str, language: str, file_name: str, include: str
) -> list[RepoEntity]:
    if language == "python":
        return _extract_python(declarations, file_name, include)
    return _extract_braces(declarations, file_name, include)


def _new_entity(kind: str, name: str, file_name: str, include: str, **kwargs) -> RepoEntity:
    return RepoEntity(
        kind=kind,
        name=name,
        file=file_name,
        include=include,
        role=role_for_name(name),
        **kwargs,
    )


def _extract_braces(text: str, file_name: str, include: str) -> list[RepoEntity]:
    """Catalogue a C/C++/Java/C#-like file already condensed to declarations."""
    entities: list[RepoEntity] = []
    stack: list[_Frame] = []
    pending: RepoEntity | None = None
    depth = 0

    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        if len(entities) >= MAX_ENTITIES_PER_FILE:
            break
        opens, closes = line.count("{"), line.count("}")
        frame = stack[-1] if stack else None

        if pending is not None and "{" in line:
            stack.append(_Frame(pending, depth, _default_access(pending)))
            frame = stack[-1]
            pending = None
            depth += opens - closes
            continue

        macro = _MACRO_RE.match(line)
        if macro is not None:
            name = macro.group(1)
            if not _INCLUDE_GUARD_RE.match(name):
                entities.append(
                    _new_entity(
                        KIND_MACRO, name, file_name, include, signature=line.lstrip("#").strip()
                    )
                )
            continue

        access = _ACCESS_RE.match(line)
        if access is not None and frame is not None:
            frame.access = access.group(1)
            depth += opens - closes
            continue

        if frame is not None and frame.entity is not None and frame.entity.kind == KIND_ENUM:
            _collect_enum_values(frame.entity, line)
            depth += opens - closes
            stack, depth = _close(stack, depth)
            continue

        entity, pending_new = _match_declaration(line, file_name, include)
        if entity is not None:
            # Nested types are catalogued on their own; their members are
            # attached to them and not to the enclosing type.
            entities.append(entity)
            if entity.kind == KIND_ENUM and "{" in line:
                _collect_enum_values(entity, line.split("{", 1)[1])
            if pending_new and "{" not in line:
                pending = entity
            elif pending_new and "}" not in line:
                stack.append(_Frame(entity, depth, _default_access(entity)))
            depth += opens - closes
            stack, depth = _close(stack, depth)
            continue

        container = _container(stack)
        if container is not None and depth == _frame_of(stack, container).depth + 1:
            _add_member(container, line, _frame_of(stack, container).access)
        elif container is None and line.endswith(";"):
            outer = _match_outer_declaration(line, file_name, include)
            if outer is not None:
                entities.append(outer)

        depth += opens - closes
        stack, depth = _close(stack, depth)
    return entities


def _match_declaration(
    line: str, file_name: str, include: str
) -> tuple[RepoEntity | None, bool]:
    """Type declarations. Returns (entity, opens_a_body)."""
    if _NAMESPACE_RE.match(line):
        return (None, False)
    enum_head = _ENUM_HEAD_RE.match(line)
    if enum_head is not None:
        entity = _new_entity(
            KIND_ENUM,
            enum_head.group(1),
            file_name,
            include,
            base=(enum_head.group(2) or "").strip(),
            signature=line.split("{", 1)[0].strip(),
        )
        return (entity, "}" not in line)
    class_head = _CLASS_HEAD_RE.match(line)
    if class_head is not None and not line.rstrip().endswith(";"):
        keyword, name = class_head.group(1), class_head.group(2)
        kind = KIND_STRUCT if keyword in ("struct", "union") else KIND_CLASS
        entity = _new_entity(
            kind,
            name,
            file_name,
            include,
            base=(class_head.group(3) or "").strip(),
            signature=line.split("{", 1)[0].strip(),
        )
        return (entity, "}" not in line)
    return (None, False)


def _match_outer_declaration(
    line: str, file_name: str, include: str
) -> RepoEntity | None:
    """File-level aliases, constants and free function declarations."""
    alias = _USING_ALIAS_RE.match(line)
    if alias is not None:
        return _new_entity(
            KIND_ALIAS, alias.group(1), file_name, include, signature=line
        )
    typedef = _TYPEDEF_RE.match(line)
    if typedef is not None:
        return _new_entity(
            KIND_ALIAS, typedef.group(1), file_name, include, signature=line
        )
    constant = _CONSTANT_RE.match(line)
    if constant is not None and _usable(constant.group(1)):
        return _new_entity(
            KIND_CONSTANT, constant.group(1), file_name, include, signature=line
        )
    function = _FUNC_DECL_RE.match(line)
    if function is not None and _usable(function.group(1)):
        return _new_entity(
            KIND_FUNCTION, function.group(1), file_name, include, signature=line
        )
    return None


def _add_member(entity: RepoEntity, line: str, access: str) -> None:
    if len(entity.members) >= MAX_MEMBERS_PER_TYPE:
        return
    text = line.rstrip(";").strip()
    if not text or text.startswith(("#", "using namespace")):
        return
    first = re.match(r"^\w+", text)
    if first is not None and first.group(0) in _SKIP_MEMBER_WORDS:
        return
    if access in ("protected", "private"):
        text = f"[{access}] {text}"
    if text not in entity.members:
        entity.members.append(text)


def _collect_enum_values(entity: RepoEntity, line: str) -> None:
    body = line.replace("{", " ").replace("}", " ").replace(";", " ")
    for part in body.split(","):
        match = re.match(r"^\s*([A-Za-z_]\w*)\s*(?:=\s*(.+?))?\s*$", part)
        if match is None:
            continue
        name = match.group(1)
        value = (match.group(2) or "").strip()
        _add_enum_value(entity, f"{name} = {value}" if value else name)


def _add_enum_value(entity: RepoEntity, text: str) -> None:
    if len(entity.members) < MAX_MEMBERS_PER_TYPE and text not in entity.members:
        entity.members.append(text)


def _container(stack: list[_Frame]) -> RepoEntity | None:
    for frame in reversed(stack):
        if frame.entity is not None and frame.entity.kind in TYPE_KINDS:
            return frame.entity
    return None


def _frame_of(stack: list[_Frame], entity: RepoEntity) -> _Frame:
    for frame in reversed(stack):
        if frame.entity is entity:
            return frame
    return stack[-1]


def _close(stack: list[_Frame], depth: int) -> tuple[list[_Frame], int]:
    while stack and depth <= stack[-1].depth:
        stack.pop()
    return stack, max(depth, 0)


def _default_access(entity: RepoEntity) -> str:
    return "private" if entity.kind == KIND_CLASS else "public"


def _clean(raw: str) -> str:
    line = raw.strip()
    if line.startswith("//"):
        return ""
    return line.split("//", 1)[0].strip()


def _usable(name: str) -> bool:
    return bool(name) and not name.startswith("_") and name not in _SKIP_MEMBER_WORDS


# ---- python ----------------------------------------------------------------


def _extract_python(text: str, file_name: str, include: str) -> list[RepoEntity]:
    entities: list[RepoEntity] = []
    current: RepoEntity | None = None
    class_indent = 0

    for raw in text.splitlines():
        if not raw.strip() or len(entities) >= MAX_ENTITIES_PER_FILE:
            continue
        indent = len(raw) - len(raw.expandtabs(4).lstrip())
        if current is not None and indent <= class_indent and not _PY_CLASS_RE.match(raw):
            current = None

        class_head = _PY_CLASS_RE.match(raw)
        if class_head is not None:
            bases = (class_head.group(4) or "").strip()
            kind = (
                KIND_ENUM
                if any(base in bases for base in _PY_ENUM_BASES)
                else KIND_CLASS
            )
            current = _new_entity(
                kind,
                class_head.group(2),
                file_name,
                include,
                base=bases,
                signature=_strip_py_comment(raw).rstrip(":"),
            )
            class_indent = len(class_head.group(1))
            entities.append(current)
            continue

        definition = _PY_DEF_RE.match(raw)
        if definition is not None:
            name = definition.group(2)
            signature = _strip_py_comment(raw).rstrip(":")
            if current is not None and indent > class_indent:
                if not name.startswith("__") or name == "__init__":
                    _add_member(current, signature, "public")
            elif indent == 0 and _usable(name):
                entities.append(
                    _new_entity(KIND_FUNCTION, name, file_name, include, signature=signature)
                )
            continue

        assignment = _PY_ASSIGN_RE.match(raw)
        if assignment is None:
            continue
        name = assignment.group(2)
        value = _strip_py_comment(assignment.group(3))
        if current is not None and indent > class_indent:
            if current.kind == KIND_ENUM:
                _add_enum_value(current, f"{name} = {value}")
            else:
                _add_member(current, f"{name} = {value}", "public")
        elif indent == 0 and name.isupper() and _usable(name):
            entities.append(
                _new_entity(
                    KIND_CONSTANT, name, file_name, include, signature=f"{name} = {value}"
                )
            )
    return entities


def _strip_py_comment(value: str) -> str:
    """Drop a trailing ``# comment`` that is not inside a string literal."""
    quote = ""
    for position, char in enumerate(value):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#":
            return value[:position].strip()
    return value.strip()


# ---- initialization recipes ------------------------------------------------


def collect_init_examples(
    files: list[Path],
    entities: list[RepoEntity],
    language: str,
    root: Path,
    *,
    max_examples: int = MAX_INIT_EXAMPLES,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Second pass: record how the project really creates each indexed type.

    Declarations alone do not tell the model whether an object is built with its
    constructor, handed out by a factory/manager or initialized in several steps.
    Here the repository is searched for real construction sites and the verbatim
    snippet (construction line + the lines that keep initializing the object) is
    attached to the type.
    """
    targets = {item.name: item for item in entities if item.kind in TYPE_KINDS}
    pending = set(targets)
    if not pending:
        return
    cache: dict[str, list[re.Pattern[str]]] = {}
    # Implementation files first: that is where objects are actually created.
    ordered = sorted(
        files,
        key=lambda path: (
            1 if path.suffix.lower() in _HEADER_SUFFIXES else 0,
            path.as_posix().lower(),
        ),
    )
    for position, path in enumerate(ordered):
        raise_if_cancelled(cancelled)
        if not pending:
            return
        content = read_text_safe(path)
        if not content:
            continue
        names = pending & set(re.findall(r"[A-Za-z_]\w*", content))
        if not names:
            continue
        if progress is not None and position % _PROGRESS_EVERY == 0:
            progress(f"Learning how objects are created ({path.name})...")
        lines = content.splitlines()
        file_name = _display_name(path, root)
        for name in sorted(names):
            entity = targets[name]
            snippet = _find_init_snippet(lines, name, language, cache)
            if snippet is None:
                continue
            if any(example.code == snippet for example in entity.init_examples):
                continue
            entity.init_examples.append(RepoUsage(file=file_name, code=snippet))
            if len(entity.init_examples) >= max_examples:
                pending.discard(name)


def _init_patterns(
    name: str, language: str, cache: dict[str, list[re.Pattern[str]]]
) -> list[re.Pattern[str]]:
    known = cache.get(name)
    if known is not None:
        return known
    escaped = re.escape(name)
    if language == "python":
        patterns = [
            re.compile(rf"^\s*(?P<var>[A-Za-z_]\w*)\s*(?::[^=]+)?=\s*[^=]*\b{escaped}\s*\("),
        ]
    else:
        patterns = [
            re.compile(
                rf"\b(?:const\s+)?{escaped}\s*[\*&]?\s*(?P<var>[A-Za-z_]\w*)\s*(?:\(|=|\{{)"
            ),
            re.compile(
                rf"<\s*(?:\w+::)*{escaped}\s*>\s*[\*&]?\s*(?P<var>[A-Za-z_]\w*)\s*(?:\(|=|\{{)"
            ),
            re.compile(rf"(?P<var>[A-Za-z_]\w*)\s*=\s*[^;=]*\b{escaped}\b\s*[({{<]"),
        ]
    cache[name] = patterns
    return patterns


def _find_init_snippet(
    lines: list[str],
    name: str,
    language: str,
    cache: dict[str, list[re.Pattern[str]]],
) -> str | None:
    for pattern in _init_patterns(name, language, cache):
        for index, line in enumerate(lines):
            if name not in line or _clean(line).startswith(("#", "//", "*")):
                continue
            match = pattern.search(line)
            if match is None:
                continue
            variable = match.group("var")
            if not variable or variable == name or variable in _SKIP_MEMBER_WORDS:
                continue
            snippet = _init_snippet(lines, index, variable)
            if snippet is not None:
                return snippet
    return None


def _init_snippet(lines: list[str], index: int, variable: str) -> str | None:
    """The construction line plus the lines that keep initializing the object."""
    picked = [lines[index]]
    open_parens = _paren_balance(lines[index])
    misses = 0
    mentions = re.compile(rf"\b{re.escape(variable)}\b")
    for follow in lines[index + 1: index + 1 + _MAX_INIT_LOOKAHEAD]:
        if len(picked) >= MAX_INIT_LINES:
            break
        if open_parens > 0:  # multi-line construction: keep it whole
            picked.append(follow)
            open_parens += _paren_balance(follow)
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
    text = _dedent(picked)
    if len(picked) == 1 and not re.search(r"[(=]", picked[0]):
        return None  # a plain member/variable declaration teaches nothing
    return text or None


def _paren_balance(line: str) -> int:
    return line.count("(") - line.count(")")


def _dedent(lines: list[str]) -> str:
    kept = [line.rstrip()[:200] for line in lines if line.strip()]
    if not kept:
        return ""
    indent = min(len(line) - len(line.lstrip()) for line in kept)
    return "\n".join(line[indent:] for line in kept)

