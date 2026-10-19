"""Best-effort C++ access-control analysis (public / protected / private members).

Generated unit tests must not call protected or private members on a plain
object of the class under test - that does not compile. This module extracts
the visibility of every member so the generator can (a) declare a legal
test-accessor subclass in the UT header and (b) tell the model exactly which
technique to use to reach each member (accessor subclass, an existing ``friend``
declaration, the ``#define private public`` include shim, or the public API).
Visibility must never make the model skip a test.

Everything here is regex based and best effort: it never raises and simply
returns less information when the code cannot be parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_CLASS_HEAD_RE = re.compile(
    r"\b(class|struct)\s+(?:\w+_(?:API|EXPORT)\s+)?([A-Za-z_]\w*)"
    r"\s*(final\b\s*)?(?::[^;{]*)?\{"
)
_ACCESS_RE = re.compile(r"^(public|protected|private)\s*:")
_FRIEND_RE = re.compile(r"\bfriend\s+(?:class|struct)?\s*([A-Za-z_][\w:]*)")
_FRIEND_TEST_RE = re.compile(r"\bFRIEND_TEST\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)")
_SKIP_PREFIXES = (
    "#",
    "using ",
    "typedef ",
    "friend ",
    "template",
    "static_assert",
    "namespace ",
    "enum ",
    "class ",
    "struct ",
    "union ",
)
_NON_MEMBER_WORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "else", "do",
    "new", "delete", "operator", "class", "struct", "enum", "union", "public",
    "private", "protected", "using", "friend", "typedef", "template", "static",
    "const", "constexpr", "virtual", "explicit", "inline", "namespace", "throw",
}

_MAX_MEMBERS = 40


@dataclass
class ClassAccess:
    """Member names of one class, grouped by access specifier."""

    name: str
    is_struct: bool = False
    is_final: bool = False
    public: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)
    friends: list[str] = field(default_factory=list)

    def inheritable_protected(self) -> list[str]:
        """Protected members a derived test accessor can re-export via ``using``."""
        return [
            name
            for name in self.protected
            if not name.startswith("~") and name != self.name
        ]

    def merge(self, other: ClassAccess) -> ClassAccess:
        """Combine two views of the same class (e.g. header + .cpp)."""
        return ClassAccess(
            name=self.name,
            is_struct=self.is_struct or other.is_struct,
            is_final=self.is_final or other.is_final,
            public=_dedup(self.public + other.public),
            protected=_dedup(self.protected + other.protected),
            private=_dedup(self.private + other.private),
            friends=_dedup(self.friends + other.friends),
        )


def accessor_name(class_name: str) -> str:
    """Name of the derived class that exposes ``class_name``'s protected members."""
    return f"{class_name}TestAccess"


def analyze_access(content: str) -> list[ClassAccess]:
    """Extract per-class member visibility from C++ source or header text."""
    text = _COMMENT_RE.sub(" ", content)
    classes: list[ClassAccess] = []
    for match in _CLASS_HEAD_RE.finditer(text):
        body = _class_body(text, match.end() - 1)
        if body is None:
            continue
        is_struct = match.group(1) == "struct"
        groups = _members(body, is_struct)
        classes.append(
            ClassAccess(
                name=match.group(2),
                is_struct=is_struct,
                is_final=bool(match.group(3)),
                public=groups["public"],
                protected=groups["protected"],
                private=groups["private"],
                friends=_friends(body),
            )
        )
    return classes


def find_class(classes: list[ClassAccess], name: str | None) -> ClassAccess | None:
    """The entry for ``name``, merging duplicate definitions."""
    if not name:
        return None
    found: ClassAccess | None = None
    for item in classes:
        if item.name != name:
            continue
        found = item if found is None else found.merge(item)
    return found


def access_prompt_text(classes: list[ClassAccess], primary: str | None = None) -> str:
    """Prompt section telling the model which members it may call directly."""
    ordered = sorted(classes, key=lambda c: (c.name != primary, c.name))
    described = [c for c in ordered if c.protected or c.private][:3]
    if not described:
        return ""

    blocks: list[str] = [
        "C++ access control of the code under test (MANDATORY - the tests must compile):"
    ]
    for item in described:
        lines = [f"- class `{item.name}`" + (" (final)" if item.is_final else "")]
        lines.append(f"  * public (call directly on the object): {_names(item.public)}")
        lines.append(f"  * protected (reach through the test accessor): {_names(item.protected)}")
        lines.append(f"  * private (reach through a friend or the access shim): "
                     f"{_names(item.private)}")
        if item.friends:
            lines.append(
                f"  * already declared friends of this class: {_names(item.friends)}"
            )
        blocks.append("\n".join(lines))

    rules = [
        "How to test a non-public member - a protected or private member is NEVER a reason "
        "to skip a test, to test something else instead, or to write a `TODO`. Work out the "
        "access yourself, using the FIRST technique below that applies:",
        "1. Public member -> call it directly on the object under test.",
        "2. Protected member -> instantiate the test-accessor class declared in the UT "
        "header (it derives from the class under test and re-exports its protected members "
        "with `using`) instead of the class itself, and call the member on that object. If "
        "the accessor does not re-export the member you need, ADD the missing "
        "`using <Class>::<member>;` line to it in the UT header.",
        "3. Private member of a class that already declares a `friend` (`friend class X;` "
        "or `FRIEND_TEST(Suite, Case)`) -> name your fixture/test exactly like that friend "
        "so the private members become directly accessible, and use it.",
        "4. Private member with no friend, or any member of a `final` class -> in the UT "
        "HEADER only, include the header of the class under test through the access shim, "
        "placed BEFORE every other #include in that header:\n"
        "```\n"
        "#define private public\n"
        "#define protected public\n"
        '#include "<header of the class under test>"\n'
        "#undef protected\n"
        "#undef private\n"
        "```\n"
        "   After that shim every member is public, so the tests call private and "
        "protected members directly on a plain object. The production code is never "
        "modified, and the macros are undefined again immediately.",
        "5. Only if none of the above is possible: drive the member through the public API "
        "that uses it and assert its observable effect (return values, state read back "
        "through public getters, calls on mocked collaborators).",
        "Whatever technique you pick, state it in one short comment above the test "
        "(e.g. `// private access via SeekTestAccess`), and still write real assertions - "
        "never an empty test body and never a `TODO` saying a member is inaccessible.",
    ]
    accessible = [c for c in described if c.inheritable_protected() and not c.is_final]
    if accessible:
        target = accessible[0]
        accessor = accessor_name(target.name)
        rules.append(
            f"For `{target.name}` the accessor is `{accessor}` (declared in the UT header): "
            f"instantiate `{accessor}` instead of `{target.name}` in every test that "
            f"exercises a protected member."
        )
    shim_classes = [c.name for c in described if c.private or (c.is_final and c.protected)]
    if shim_classes:
        rules.append(
            "These classes need technique 3 or 4 for their non-public members: "
            + ", ".join(f"`{n}`" for n in shim_classes)
            + "."
        )
    blocks.append("\n".join(rules))
    return "\n\n".join(blocks)


# ---- internals -------------------------------------------------------------


def _names(values: list[str]) -> str:
    return ", ".join(f"`{v}`" for v in values[:_MAX_MEMBERS]) or "none"


def _dedup(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def _friends(body: str) -> list[str]:
    """Names a class already grants friendship to (usable as the test fixture name)."""
    names = [match.group(1) for match in _FRIEND_TEST_RE.finditer(body)]
    names += [
        match.group(1)
        for match in _FRIEND_RE.finditer(body)
        if match.group(1) not in _NON_MEMBER_WORDS
    ]
    return _dedup(names)[:_MAX_MEMBERS]


def _class_body(text: str, brace_index: int) -> str | None:
    """Text between the class's opening brace and its matching closing brace."""
    depth = 0
    for index in range(brace_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index + 1: index]
    return None


def _members(body: str, is_struct: bool) -> dict[str, list[str]]:
    """Member names declared directly in a class body, grouped by access."""
    groups: dict[str, list[str]] = {"public": [], "protected": [], "private": []}
    access = "public" if is_struct else "private"
    depth = 0
    statement = ""
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 0:
            specifier = _ACCESS_RE.match(stripped)
            if specifier is not None:
                access = specifier.group(1)
                statement = ""
                continue
            statement = f"{statement} {stripped}".strip()
            if "{" in stripped or ";" in stripped:
                name = _member_name(statement)
                if name is not None and name not in groups[access]:
                    groups[access].append(name)
                statement = ""
        depth += stripped.count("{") - stripped.count("}")
        if depth < 0:
            depth = 0
    return groups


def _member_name(statement: str) -> str | None:
    """Name declared by a single member statement, or None when not a member."""
    text = statement.strip()
    if not text or text.lstrip().startswith(_SKIP_PREFIXES):
        return None
    text = re.sub(r"\bnoexcept\s*\([^)]*\)", " ", text)
    paren = text.find("(")
    if paren > 0 and "=" not in text[:paren]:
        head = text[:paren].rstrip()
        match = re.search(r"([A-Za-z_~]\w*)$", head)
        if match is None or match.group(1) in _NON_MEMBER_WORDS:
            return None
        return match.group(1)
    if paren >= 0:
        return None
    declaration = text.split("=", 1)[0].rstrip(" ;")
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?$", declaration)
    if match is None or match.group(1) in _NON_MEMBER_WORDS:
        return None
    prefix = declaration[: match.start()].strip()
    if not prefix:  # no type in front -> not a data member declaration
        return None
    return match.group(1)
