"""C/C++ semantic extraction rules."""

from __future__ import annotations

import re
from typing import Any

from ...models.semantics import CallSite, MemberAccess, SymbolDecl, SymbolKind
from . import ts_support as ts
from .extractor_base import SKIP, BaseExtractor, Ctx

#: Macros that declare a test case in the C++ frameworks we support.
TEST_MACROS = {
    "TEST",
    "TEST_F",
    "TEST_P",
    "TYPED_TEST",
    "TYPED_TEST_P",
    "TEST_CASE",
    "TEST_CASE_METHOD",
    "SCENARIO",
    "BOOST_AUTO_TEST_CASE",
    "BOOST_FIXTURE_TEST_CASE",
    "CATCH_TEST_CASE",
}
#: Macros whose first argument names a fixture class rather than a suite.
FIXTURE_MACROS = {"TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P",
                  "TEST_CASE_METHOD", "BOOST_FIXTURE_TEST_CASE"}

_SMART_FACTORIES = {"make_unique", "make_shared", "make_optional"}
_TEMPLATE_ARG_RE = re.compile(r"<(.+)>$", re.DOTALL)
_MOCK_MACROS = {"MOCK_METHOD", "MOCK_METHOD0", "MOCK_METHOD1", "MOCK_METHOD2",
                "MOCK_METHOD3", "MOCK_METHOD4", "MOCK_CONST_METHOD0",
                "MOCK_CONST_METHOD1", "MOCK_CONST_METHOD2"}

_DECLARATOR_WRAPPERS = (
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "array_declarator",
    "init_declarator",
)

#: An identifier that looks like a macro: ALL CAPS, at least three characters.
_MACRO_TOKEN = r"[A-Z][A-Z0-9_]{2,}"
#: ``Q_OBJECT`` / ``GENERATED_BODY()`` / ``DECLARE_DYNAMIC(CFoo)`` alone on a
#: line and WITHOUT a semicolon: the grammar glues it to the next declaration.
_BARE_MACRO_LINE_RE = re.compile(
    rf"^[ \t]*({_MACRO_TOKEN}[ \t]*(?:\([^()\r\n]*\))?)[ \t\r]*$", re.MULTILINE
)
#: ``class MYLIB_API Foo : public Bar``
_CLASS_MACRO_RE = re.compile(rf"\b(?:class|struct)[ \t]+({_MACRO_TOKEN})[ \t]+(?=\w+\s*[:{{])")
#: ``MYLIB_API void run() override;`` - macro, then a real type, then a name.
_DECL_MACRO_RE = re.compile(
    rf"^[ \t]*({_MACRO_TOKEN})[ \t]+"
    r"(?=(?:const|constexpr|static|virtual|inline|explicit|friend)?[ \t]*"
    r"[A-Za-z_][\w:<>,\*&\[\] \t]*[ \t*&][~\w]+[ \t]*\()",
    re.MULTILINE,
)
#: Qt section keywords the C++ grammar does not know.
_QT_SECTION_RE = re.compile(r"^[ \t]*(signals|Q_SIGNALS|Q_SLOTS)[ \t]*:", re.MULTILINE)
_QT_SLOTS_RE = re.compile(r"\b(?:public|protected|private)[ \t]+(slots)[ \t]*:")
_MACRO_HINT_RE = re.compile(rf"\b{_MACRO_TOKEN}\b|\bsignals[ \t]*:|\bslots[ \t]*:")


def _blank(match: "re.Match[str]", group: int = 1, filler: str = "") -> str:
    """The matched text with ``group`` replaced by ``filler``, padded with spaces.

    Replacements keep the original length so every byte offset - and with it
    every line number and snippet the extractor reports - stays valid.
    """
    text = match.group(0)
    start = match.start(group) - match.start(0)
    end = match.end(group) - match.start(0)
    return text[:start] + filler.ljust(end - start) + text[end:]


def sanitize_macros(code: str) -> str:
    """Blank out macros that break the C++ grammar.

    ``Q_OBJECT``, ``GENERATED_BODY()`` and dllexport macros make tree-sitter
    swallow the declaration that follows them, so methods (typically the
    ``override`` ones right below ``Q_OBJECT``) disappear from the analysis.
    Removing the macro text - never anything else - lets the class parse.
    """
    if not _MACRO_HINT_RE.search(code):
        return code
    code = _BARE_MACRO_LINE_RE.sub(_blank, code)
    code = _CLASS_MACRO_RE.sub(_blank, code)
    code = _DECL_MACRO_RE.sub(_blank, code)
    code = _QT_SECTION_RE.sub(lambda m: _blank(m, 1, "public"), code)
    code = _QT_SLOTS_RE.sub(_blank, code)
    return code


class CppExtractor(BaseExtractor):
    language = "cpp"
    call_nodes = ("call_expression",)
    construction_nodes = ("new_expression",)
    member_nodes = ("field_expression",)
    default_access = "public"

    @staticmethod
    def sanitize(code: str) -> str:
        return sanitize_macros(code)

    def __init__(self, path, data) -> None:  # type: ignore[no-untyped-def]
        super().__init__(path, data)
        self._template_params: list[str] = []
        self._mock_classes: set[int] = set()

    # ---------------------------------------------------------- dispatch

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind == "preproc_include":
            return self._include(node)
        if kind in ("preproc_def", "preproc_function_def"):
            return self._macro(node, ctx)
        if kind == "namespace_definition":
            return self._namespace(node, ctx)
        if kind == "template_declaration":
            self._template_params = self._template_parameters(node)
            return ctx
        if kind in ("class_specifier", "struct_specifier", "union_specifier"):
            return self._record_class(node, ctx)
        if kind == "enum_specifier":
            return self._enum(node, ctx)
        if kind in ("alias_declaration", "type_definition"):
            return self._alias(node, ctx)
        if kind == "field_declaration_list":
            return self._members(node, ctx)
        if kind == "function_definition":
            return self._function_definition(node, ctx)
        if kind in ("declaration", "field_declaration"):
            return self._declaration(node, ctx)
        return None

    # ------------------------------------------------------------ pieces

    def _include(self, node: Any) -> object:
        path_node = ts.field(node, "path")
        raw = self.line_text(node, 200)
        target = self.text(path_node).strip('<>"') if path_node is not None else ""
        self.imports.append(
            self._import(raw=raw, module=target, node=node,
                         relative=raw.count('"') >= 2)
        )
        return SKIP

    def _import(self, *, raw: str, module: str, node: Any, relative: bool):
        from ...models.semantics import ImportDecl

        return ImportDecl(
            raw=raw,
            module=module,
            line=ts.line_of(node),
            is_relative=relative,
            file=self.path,
        )

    def _macro(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        value = ts.field(node, "value")
        self.make(
            SymbolKind.MACRO,
            self.text(name_node),
            node,
            ctx,
            signature=self.line_text(node, 160),
            value=self.line_text(value, 120) if value is not None else "",
            params=self.params_from(ts.field(node, "parameters")),
        )
        return SKIP

    def _namespace(self, node: Any, ctx: Ctx) -> Ctx:
        name_node = ts.field(node, "name")
        name = self.text(name_node) if name_node is not None else ""
        if name:
            self.make(SymbolKind.NAMESPACE, name, node, ctx)
            return self.enter(ctx, name)
        return ctx

    def _record_class(self, node: Any, ctx: Ctx) -> Ctx | object:
        name_node = ts.field(node, "name")
        body = ts.field(node, "body")
        if name_node is None:
            return ctx
        name = self.text(name_node)
        kind = SymbolKind.STRUCT if node.type == "struct_specifier" else SymbolKind.CLASS
        bases = self._base_types(node)
        template = self._take_template()
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            base_types=bases,
            template_params=template,
            signature=self._class_signature(node, name, bases, template),
        )
        if body is None:
            return SKIP
        if self._looks_like_mock(name, bases, body):
            symbol.kind = SymbolKind.MOCK
        elif self._looks_like_fixture(bases):
            symbol.kind = SymbolKind.FIXTURE
        inner = self.enter(
            ctx, name, access="public" if kind is SymbolKind.STRUCT else "private"
        )
        self._walk(body, inner)
        symbol.members = [
            s.name
            for s in self.symbols
            if s.scope == inner.scope and s.kind is not SymbolKind.NAMESPACE
        ]
        return SKIP

    def _members(self, node: Any, ctx: Ctx) -> object:
        """Walk a class body, tracking ``public:`` / ``protected:`` markers."""
        access = ctx.access
        for child in node.children:
            if child.type == "access_specifier":
                access = self.text(child).strip(": ").strip() or access
                continue
            self._walk(child, Ctx(scope=ctx.scope, enclosing=ctx.enclosing, access=access))
        return SKIP

    def _enum(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        base = ts.field(node, "base")
        body = ts.field(node, "body")
        values: list[str] = []
        inner = self.enter(ctx, name, access=ctx.access)
        for enumerator in ts.children_of_type(body, "enumerator") if body else []:
            member_name_node = ts.field(enumerator, "name")
            if member_name_node is None:
                continue
            member_name = self.text(member_name_node)
            value_node = ts.field(enumerator, "value")
            values.append(member_name)
            self.make(
                SymbolKind.ENUM_MEMBER,
                member_name,
                enumerator,
                inner,
                value=self.line_text(value_node, 60) if value_node is not None else "",
                signature=self.line_text(enumerator, 80),
            )
        self.make(
            SymbolKind.ENUM,
            name,
            node,
            ctx,
            members=values,
            base_types=[self.line_text(base, 40)] if base is not None else [],
            signature=f"enum {name}",
        )
        return SKIP

    def _alias(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        target = ts.field(node, "type")
        name = self.text(name_node) if name_node is not None else self._typedef_name(node)
        if not name:
            return SKIP
        self.make(
            SymbolKind.ALIAS,
            name,
            node,
            ctx,
            value=self.line_text(target, 120) if target is not None else "",
            signature=self.line_text(node, 160),
        )
        return SKIP

    def _typedef_name(self, node: Any) -> str:
        declarator = ts.field(node, "declarator")
        return self._identifier_text(declarator) if declarator is not None else ""

    # ------------------------------------------------------- functions

    def _function_definition(self, node: Any, ctx: Ctx) -> Ctx | object:
        declarator = self._function_declarator(ts.field(node, "declarator"))
        if declarator is None:
            return ctx
        name_node = ts.field(declarator, "declarator")
        raw_name = self._declarator_name(name_node)
        if raw_name in TEST_MACROS:
            return self._test_macro(node, declarator, raw_name, ctx)
        body = ts.field(node, "body")
        symbol = self._callable_symbol(node, declarator, ctx, has_body=True)
        if symbol is None:
            return ctx
        if body is not None:
            symbol.body = self.text(body)[:4000]
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    def _declaration(self, node: Any, ctx: Ctx) -> Ctx | object:
        declarator = self._function_declarator(ts.field(node, "declarator"))
        if declarator is not None:
            if ctx.enclosing:
                return None
            self._callable_symbol(node, declarator, ctx, has_body=False)
            return SKIP
        construction = self._construction_from_declaration(node, ctx)
        if construction is not None:
            self.calls.append(construction)
        if ctx.enclosing:
            return None
        self._data_member(node, ctx)
        return None

    def _data_member(self, node: Any, ctx: Ctx) -> None:
        declarator = ts.field(node, "declarator")
        name = self._declarator_name(declarator)
        if not name:
            return
        text = self.line_text(node, 200)
        is_const = bool(re.search(r"\b(const|constexpr|static)\b", text))
        kind = SymbolKind.CONSTANT if (is_const and not ctx.scope) else SymbolKind.FIELD
        value_node = ts.field(declarator, "value") if declarator is not None else None
        self.make(
            kind,
            name,
            node,
            ctx,
            signature=text,
            return_type=self.line_text(ts.field(node, "type"), 80),
            value=self.line_text(value_node, 80) if value_node is not None else "",
        )

    def _callable_symbol(
        self, node: Any, declarator: Any, ctx: Ctx, *, has_body: bool
    ) -> SymbolDecl | None:
        name_node = ts.field(declarator, "declarator")
        raw_name = self._declarator_name(name_node)
        if not raw_name:
            return None
        text = self.line_text(node, 240)
        if "= delete" in text:
            return None
        scope, name = self._split_qualified(raw_name, ctx)
        owner = scope.split("::")[-1] if scope else ""
        kind = SymbolKind.FUNCTION
        if name.startswith("~"):
            kind = SymbolKind.DESTRUCTOR
        elif owner and name == owner:
            kind = SymbolKind.CONSTRUCTOR
        elif scope:
            kind = SymbolKind.METHOD
        signature = self.line_text(declarator, 240)
        return self.make(
            kind,
            name,
            node,
            ctx,
            scope=scope,
            params=self.params_from(ts.field(declarator, "parameters")),
            return_type=self.line_text(ts.field(node, "type"), 80),
            template_params=self._take_template(),
            is_static=bool(re.match(r"^\s*static\b", text)),
            is_abstract="= 0" in text,
            signature=signature if has_body else text.rstrip(";"),
        )

    def _test_macro(self, node: Any, declarator: Any, macro: str, ctx: Ctx) -> object:
        args = [
            self.line_text(child, 80)
            for child in ts.named_children(ts.field(declarator, "parameters") or node)
        ]
        args = [a for a in args if a]
        suite = args[0] if args else ""
        case = args[1] if len(args) > 1 else (args[0] if args else "test")
        name = case.strip('"').strip()
        body = ts.field(node, "body")
        symbol = self.make(
            SymbolKind.TEST_CASE,
            name or macro,
            node,
            ctx,
            scope=suite.strip('"').strip(),
            signature=f"{macro}({', '.join(args)})",
            base_types=[suite] if macro in FIXTURE_MACROS and suite else [],
            decorators=[macro],
            body=self.text(body)[:6000] if body is not None else "",
        )
        if body is not None:
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    # ------------------------------------------------------- expressions

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        function = ts.field(node, "function")
        arguments = ts.field(node, "arguments")
        if function is None:
            return None
        args = [self.line_text(a, 80) for a in ts.named_children(arguments)] if arguments else []
        raw = self.line_text(function, 160)
        template_arg = self._template_argument(raw)
        base = _TEMPLATE_ARG_RE.sub("", raw)
        qualifier, name = self._split_call_name(function, base)
        is_construction = False
        if name in _SMART_FACTORIES and template_arg:
            name = template_arg.split("::")[-1]
            qualifier = template_arg.rsplit("::", 1)[0] if "::" in template_arg else ""
            is_construction = True
        elif name and name[:1].isupper() and not qualifier:
            is_construction = False
        return CallSite(
            name=name,
            qualifier=qualifier,
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            is_construction=is_construction,
            text=self.line_text(node, 160),
            file=self.path,
        )

    def construction_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        type_node = ts.field(node, "type")
        arguments = ts.field(node, "arguments")
        if type_node is None:
            return None
        raw = self.line_text(type_node, 120)
        args = [self.line_text(a, 80) for a in ts.named_children(arguments)] if arguments else []
        return CallSite(
            name=raw.split("::")[-1],
            qualifier=raw.rsplit("::", 1)[0] if "::" in raw else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            is_construction=True,
            text=self.line_text(node, 160),
            file=self.path,
        )

    def _construction_from_declaration(self, node: Any, ctx: Ctx) -> CallSite | None:
        type_node = ts.field(node, "type")
        declarator = ts.field(node, "declarator")
        if type_node is None or declarator is None or declarator.type != "init_declarator":
            return None
        value = ts.field(declarator, "value")
        if value is None or value.type not in ("argument_list", "initializer_list"):
            return None
        raw = self.line_text(type_node, 120)
        args = [self.line_text(a, 80) for a in ts.named_children(value)]
        bound = ts.field(declarator, "declarator")
        return CallSite(
            name=raw.split("::")[-1],
            qualifier=raw.rsplit("::", 1)[0] if "::" in raw else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            is_construction=True,
            target=self._identifier_text(bound) if bound is not None else "",
            text=self.line_text(node, 160),
            file=self.path,
        )

    def member_info(self, node: Any, ctx: Ctx) -> MemberAccess | None:
        field_node = ts.field(node, "field")
        argument = ts.field(node, "argument")
        if field_node is None:
            return None
        return MemberAccess(
            name=self.text(field_node),
            qualifier=self.line_text(argument, 80) if argument is not None else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            file=self.path,
        )

    # ---------------------------------------------------------- helpers

    def _split_call_name(self, function: Any, base: str) -> tuple[str, str]:
        if function.type == "field_expression":
            argument = ts.field(function, "argument")
            field_node = ts.field(function, "field")
            return (
                self.line_text(argument, 80) if argument is not None else "",
                self.text(field_node) if field_node is not None else "",
            )
        if "::" in base:
            head, _, tail = base.rpartition("::")
            return head, tail
        return "", base

    def _template_argument(self, raw: str) -> str:
        match = _TEMPLATE_ARG_RE.search(raw)
        if not match:
            return ""
        return match.group(1).split(",")[0].strip()

    def _function_declarator(self, node: Any) -> Any | None:
        current = node
        seen = 0
        while current is not None and seen < 6:
            if current.type == "function_declarator":
                return current
            if current.type in _DECLARATOR_WRAPPERS:
                current = ts.field(current, "declarator")
                seen += 1
                continue
            return None
        return None

    def _declarator_name(self, node: Any) -> str:
        if node is None:
            return ""
        if node.type == "qualified_identifier":
            return self.line_text(node, 120)
        if node.type == "destructor_name":
            return self.text(node)
        if node.type in ("identifier", "field_identifier", "type_identifier",
                         "operator_name"):
            return self.text(node)
        inner = ts.field(node, "declarator")
        if inner is not None:
            return self._declarator_name(inner)
        found = ts.first_of_type(node, "identifier", "field_identifier")
        return self.text(found) if found is not None else ""

    def _split_qualified(self, raw: str, ctx: Ctx) -> tuple[str, str]:
        if "::" not in raw:
            return ctx.scope, raw
        head, _, tail = raw.rpartition("::")
        if ctx.scope:
            return f"{ctx.scope}::{head}", tail
        return head, tail

    def _base_types(self, node: Any) -> list[str]:
        clause = ts.first_of_type(node, "base_class_clause")
        if clause is None:
            return []
        bases: list[str] = []
        for child in ts.named_children(clause):
            if child.type == "access_specifier":
                continue
            text = self.line_text(child, 80)
            if text:
                bases.append(text)
        return bases

    def _class_signature(self, node: Any, name: str, bases: list[str], template: list[str]) -> str:
        head = "struct" if node.type == "struct_specifier" else "class"
        prefix = f"template <{', '.join(template)}> " if template else ""
        suffix = f" : {', '.join(bases)}" if bases else ""
        return f"{prefix}{head} {name}{suffix}"

    def _template_parameters(self, node: Any) -> list[str]:
        params = ts.field(node, "parameters")
        if params is None:
            return []
        return [self.line_text(c, 60) for c in ts.named_children(params)]

    def _take_template(self) -> list[str]:
        params, self._template_params = self._template_params, []
        return params

    def _looks_like_mock(self, name: str, bases: list[str], body: Any) -> bool:
        if any(macro in self.text(body) for macro in _MOCK_MACROS):
            return True
        lowered = name.lower()
        return lowered.startswith(("mock", "fake", "stub")) or lowered.endswith(
            ("mock", "fake", "stub")
        )

    def _looks_like_fixture(self, bases: list[str]) -> bool:
        return any(
            b.replace(" ", "").endswith(("testing::Test", "::Test", "Fixture"))
            for b in bases
        )
