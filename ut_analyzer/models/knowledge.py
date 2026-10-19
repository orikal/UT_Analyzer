"""Repository-wide knowledge: every type, enum, constant and collaborator found.

The repository index is built once per scan, written to a Markdown file next to
the repository and consulted by every generation request, so the model knows the
real structs, enums, readers, publishers and function signatures of the project
instead of inventing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KIND_ENUM = "enum"
KIND_STRUCT = "struct"
KIND_CLASS = "class"
KIND_ALIAS = "alias"
KIND_CONSTANT = "constant"
KIND_MACRO = "macro"
KIND_FUNCTION = "function"

TYPE_KINDS = (KIND_STRUCT, KIND_CLASS)

# Order used when rendering: the model needs concrete values/types first.
_KIND_ORDER = (
    KIND_ENUM,
    KIND_STRUCT,
    KIND_CLASS,
    KIND_ALIAS,
    KIND_CONSTANT,
    KIND_MACRO,
    KIND_FUNCTION,
)

_KIND_TITLES = {
    KIND_ENUM: "Enums (exact enumerator names and values)",
    KIND_STRUCT: "Structs",
    KIND_CLASS: "Classes / interfaces",
    KIND_ALIAS: "Type aliases / typedefs",
    KIND_CONSTANT: "Constants",
    KIND_MACRO: "Macros",
    KIND_FUNCTION: "Free functions",
}

# Name fragments that reveal the role an object plays in the system. Longest
# fragments first so "subscriber" is not reported as "subscribe".
ROLE_HINTS: tuple[str, ...] = (
    "subscriber", "publisher", "dispatcher", "serializer", "deserializer",
    "controller", "repository", "registry", "scheduler", "connection",
    "validator", "converter", "formatter", "container", "listener",
    "observer", "provider", "consumer", "producer", "receiver", "notifier",
    "manager", "factory", "builder", "handler", "wrapper", "adapter",
    "session", "service", "monitor", "watcher", "logger", "sender",
    "parser", "reader", "writer", "worker", "client", "server", "config",
    "socket", "channel", "stream", "buffer", "queue", "cache", "store",
    "topic", "event", "timer", "pool", "proxy", "state", "mapper", "filter",
)


def role_for_name(name: str) -> str:
    """Role this symbol plays, derived from its name ("" when unknown)."""
    lowered = name.lower()
    for hint in ROLE_HINTS:
        if hint in lowered:
            return hint
    return ""


@dataclass
class RepoUsage:
    """A verbatim snippet showing how the project creates/initializes a type."""

    file: str
    code: str


@dataclass
class RepoEntity:
    """One symbol declared somewhere in the repository."""

    kind: str
    name: str
    file: str = ""            # repo-relative path, posix style
    include: str = ""         # include/import line that makes it visible
    signature: str = ""       # declaration line (or full signature for functions)
    base: str = ""            # base classes / underlying enum type
    members: list[str] = field(default_factory=list)
    role: str = ""
    init_examples: list[RepoUsage] = field(default_factory=list)

    def location(self) -> str:
        where = self.file or "unknown file"
        return f"{where}, requires `{self.include}`" if self.include else where

    def headline(self) -> str:
        head = f"{self.kind} `{self.name}`"
        if self.base:
            head += f" {self.base}"
        return f"{head} ({self.location()})"

    def to_prompt_lines(self, max_members: int = 12, max_examples: int = 1) -> list[str]:
        lines = [f"- {self.headline()}"]
        if self.kind == KIND_ENUM and self.members:
            lines.append(f"    values: {', '.join(self.members[:max_members])}")
            return lines
        if self.kind in (KIND_CONSTANT, KIND_MACRO, KIND_ALIAS, KIND_FUNCTION):
            if self.signature:
                lines.append(f"    {self.signature}")
            return lines
        for member in self.members[:max_members]:
            lines.append(f"    * {member}")
        for example in self.init_examples[:max_examples]:
            lines.append(
                f"    this project creates and initializes it like this "
                f"(from {example.file}) - reproduce these steps:"
            )
            lines.extend(f"      | {code}" for code in example.code.splitlines())
        return lines


@dataclass
class RepoKnowledge:
    """Everything the tool learned by indexing the whole repository."""

    root: Path | None = None
    language: str = ""
    generated_at: str = ""
    indexed_files: int = 0
    skipped_files: int = 0
    entities: list[RepoEntity] = field(default_factory=list)
    markdown_path: Path | None = None
    #: Condensed declarations per file, keyed by ``str(path)``. Filled while
    #: indexing so generation does not have to read and condense the same files
    #: again for every request. Not persisted.
    declarations: dict[str, str] = field(default_factory=dict)

    # ---- queries ---------------------------------------------------------

    def is_empty(self) -> bool:
        return not self.entities

    def of_kind(self, kind: str) -> list[RepoEntity]:
        return [item for item in self.entities if item.kind == kind]

    def by_role(self) -> dict[str, list[RepoEntity]]:
        grouped: dict[str, list[RepoEntity]] = {}
        for item in self.entities:
            if item.role and item.kind in TYPE_KINDS:
                grouped.setdefault(item.role, []).append(item)
        return dict(sorted(grouped.items()))

    def find(self, name: str) -> RepoEntity | None:
        for item in self.entities:
            if item.name == name:
                return item
        return None

    def mentioned_in(self, text: str) -> list[RepoEntity]:
        """Entities whose name appears in *text*, most relevant kind first."""
        if not text:
            return []
        words = set(re.findall(r"[A-Za-z_]\w*", text))
        hits = [item for item in self.entities if item.name in words]
        hits.sort(key=lambda item: _KIND_ORDER.index(item.kind)
                  if item.kind in _KIND_ORDER else len(_KIND_ORDER))
        return hits

    def summary(self) -> str:
        """Short human-readable line for the UI/progress log."""
        counts = {kind: len(self.of_kind(kind)) for kind in _KIND_ORDER}
        return (
            f"Indexed {self.indexed_files} repository files: "
            f"{counts[KIND_STRUCT] + counts[KIND_CLASS]} types, "
            f"{counts[KIND_ENUM]} enums, "
            f"{counts[KIND_CONSTANT] + counts[KIND_MACRO]} constants, "
            f"{counts[KIND_FUNCTION]} functions"
        )

    # ---- prompt ----------------------------------------------------------

    def prompt_text_for(self, seed_text: str, max_chars: int = 4000) -> str:
        """The part of the repository index that matters for one source file."""
        if self.is_empty() or max_chars <= 0:
            return ""
        blocks: list[str] = []
        used = 0
        relevant = self.mentioned_in(seed_text)
        grouped: dict[str, list[RepoEntity]] = {}
        for item in relevant:
            grouped.setdefault(item.kind, []).append(item)
        for kind in _KIND_ORDER:
            items = grouped.get(kind)
            if not items:
                continue
            lines = [f"{_KIND_TITLES[kind]}:"]
            for item in items[:25]:
                lines.extend(item.to_prompt_lines())
            block = "\n".join(lines)
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        catalogue = self._catalogue_text(exclude={item.name for item in relevant})
        if catalogue and used + len(catalogue) <= max_chars:
            blocks.append(catalogue)
        return "\n\n".join(blocks)

    def _catalogue_text(self, exclude: set[str], limit: int = 40) -> str:
        """Compact list of other repository types the tests may need."""
        names: list[str] = []
        for item in self.entities:
            if item.kind not in (*TYPE_KINDS, KIND_ENUM) or item.name in exclude:
                continue
            names.append(f"{item.name} ({item.file})")
            if len(names) >= limit:
                break
        if not names:
            return ""
        return (
            "Other types that exist in this repository (look them up in the repository "
            "knowledge file before inventing anything):\n- " + "\n- ".join(names)
        )

    # ---- markdown --------------------------------------------------------

    def to_markdown(self, max_members: int = 25) -> str:
        """Full, human- and model-readable index of the repository."""
        root_name = self.root.name if self.root is not None else "repository"
        lines: list[str] = [
            f"# Repository knowledge - {root_name}",
            "",
            "Generated automatically by UT Analyzer when the repository was scanned.",
            "It lists the real structs, enums, constants, collaborators (readers, "
            "publishers, subscribers, managers, queues, ...) and function signatures "
            "declared in this repository.",
            "",
            "**How to use this file when writing unit tests**",
            "",
            "- Use only the names, enum values, types and signatures listed here.",
            "- Never invent a type, member, enum value or constructor that is missing here.",
            "- Include/import the file listed next to a symbol before using that symbol.",
            "- When a symbol has a 'How this project creates/initializes it' snippet, create",
            "  and initialize that object the same way (same factory/manager, same order of",
            "  calls, same configuration steps) instead of inventing your own construction.",
            "- When something you need is in none of the knowledge files, write your best",
            "  guess and put a `TODO: <what is missing and what has to be done>` comment on",
            "  the line directly above it. Never use a `TODO` for something you can look up",
            "  here, and never leave a test body unimplemented.",
            "",
            "## Overview",
            "",
            f"- Repository: `{self.root if self.root is not None else 'unknown'}`",
            f"- Primary language: {self.language or 'unknown'}",
            f"- Generated at: {self.generated_at or 'unknown'}",
            f"- Files indexed: {self.indexed_files}"
            + (f" (skipped {self.skipped_files})" if self.skipped_files else ""),
            f"- Symbols: {len(self.entities)}",
            "",
        ]
        roles = self.by_role()
        if roles:
            lines += ["## Collaborators by role", ""]
            for role, items in roles.items():
                lines.append(f"### {role.capitalize()}s")
                lines.append("")
                for item in items:
                    lines.append(f"- `{item.name}` - {item.location()}")
                lines.append("")
        for kind in _KIND_ORDER:
            items = self.of_kind(kind)
            if not items:
                continue
            lines += [f"## {_KIND_TITLES[kind]}", ""]
            for item in items:
                lines.extend(_markdown_entity(item, max_members))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _markdown_entity(item: RepoEntity, max_members: int) -> list[str]:
    lines = [f"### `{item.name}`", ""]
    lines.append(f"- Declared in: `{item.file or 'unknown'}`")
    if item.include:
        lines.append(f"- Include/import: `{item.include}`")
    if item.base:
        lines.append(f"- Inherits/underlying: `{item.base.lstrip(': ').strip()}`")
    if item.role:
        lines.append(f"- Role: {item.role}")
    if item.signature and item.kind not in TYPE_KINDS:
        lines.append(f"- Declaration: `{item.signature}`")
    if item.members:
        label = "Values" if item.kind == KIND_ENUM else "Members"
        lines.append(f"- {label}:")
        lines.extend(f"    - `{member}`" for member in item.members[:max_members])
        if len(item.members) > max_members:
            lines.append(f"    - ... {len(item.members) - max_members} more")
    for example in item.init_examples:
        lines += [
            "",
            f"- How this project creates/initializes it (from `{example.file}`):",
            "",
            "```",
            example.code,
            "```",
        ]
    lines.append("")
    return lines
