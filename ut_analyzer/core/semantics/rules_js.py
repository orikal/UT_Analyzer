"""JavaScript / TypeScript semantic extraction rules."""

from __future__ import annotations

from typing import Any

from ...models.semantics import CallSite, ImportDecl, MemberAccess, SymbolKind
from . import ts_support as ts
from .extractor_base import SKIP, BaseExtractor, Ctx

_TEST_CALLS = {"it", "test", "specify"}
_SUITE_CALLS = {"describe", "suite", "context"}
_HOOK_CALLS = {"beforeEach", "beforeAll", "before", "afterEach", "afterAll", "after"}


class JavaScriptExtractor(BaseExtractor):
    language = "javascript"
    call_nodes = ("call_expression",)
    construction_nodes = ("new_expression",)
    member_nodes = ("member_expression",)
    default_access = "public"

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind == "import_statement":
            return self._import(node)
        if kind in ("class_declaration", "class"):
            return self._class(node, ctx)
        if kind == "function_declaration":
            return self._function(node, ctx, SymbolKind.FUNCTION)
        if kind == "method_definition":
            return self._method(node, ctx)
        if kind == "lexical_declaration":
            return self._lexical(node, ctx)
        if kind == "call_expression":
            return self._maybe_test(node, ctx)
        return None

    def _import(self, node: Any) -> object:
        source = ts.field(node, "source")
        module = self.text(source).strip("'\"") if source is not None else ""
        symbols = [
            self._identifier_text(ts.field(spec, "name") or spec)
            for spec in ts.descendants(node, "import_specifier")
        ]
        self.imports.append(
            ImportDecl(
                raw=self.line_text(node, 160),
                module=module,
                symbols=[s for s in symbols if s],
                line=ts.line_of(node),
                is_relative=module.startswith("."),
                file=self.path,
            )
        )
        return SKIP

    def _class(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return None
        name = self.text(name_node)
        heritage = ts.first_of_type(node, "class_heritage")
        bases = (
            [self.line_text(heritage, 80).replace("extends", "").strip()]
            if heritage is not None
            else []
        )
        symbol = self.make(
            SymbolKind.CLASS,
            name,
            node,
            ctx,
            base_types=[b for b in bases if b],
            signature=f"class {name}",
        )
        lowered = name.lower()
        if lowered.startswith(("mock", "fake", "stub")) or lowered.endswith(
            ("mock", "fake", "stub")
        ):
            symbol.kind = SymbolKind.MOCK
        inner = self.enter(ctx, name)
        body = ts.field(node, "body")
        if body is not None:
            self._walk(body, inner)
        symbol.members = [s.name for s in self.symbols if s.scope == inner.scope]
        return SKIP

    def _method(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self._identifier_text(name_node)
        kind = SymbolKind.CONSTRUCTOR if name == "constructor" else SymbolKind.METHOD
        return self._emit(node, ctx, kind, name)

    def _function(self, node: Any, ctx: Ctx, kind: SymbolKind) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return None
        return self._emit(node, ctx, kind, self.text(name_node))

    def _emit(self, node: Any, ctx: Ctx, kind: SymbolKind, name: str) -> object:
        params = self.params_from(ts.field(node, "parameters"))
        body = ts.field(node, "body")
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            params=params,
            signature=f"{name}({', '.join(p.render() for p in params)})",
            body=self.text(body)[:6000] if body is not None else "",
        )
        if body is not None:
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    def _lexical(self, node: Any, ctx: Ctx) -> Ctx | None:
        if ctx.enclosing:
            return None
        is_const = "const" in self.line_text(node, 40)
        for declarator in ts.children_of_type(node, "variable_declarator"):
            name_node = ts.field(declarator, "name")
            if name_node is None or name_node.type != "identifier":
                continue
            name = self.text(name_node)
            value = ts.field(declarator, "value")
            self.make(
                SymbolKind.CONSTANT if is_const and name.isupper() else SymbolKind.VARIABLE,
                name,
                declarator,
                ctx,
                value=self.line_text(value, 100) if value is not None else "",
                signature=self.line_text(node, 160),
            )
        return None

    def _maybe_test(self, node: Any, ctx: Ctx) -> Ctx | None:
        function = ts.field(node, "function")
        arguments = ts.field(node, "arguments")
        if function is None or function.type != "identifier":
            return None
        callee = self.text(function)
        args = ts.named_children(arguments) if arguments else []
        if not args or args[0].type not in ("string", "template_string"):
            return None
        title = self.text(args[0]).strip("'\"`")
        if callee in _SUITE_CALLS:
            self.make(SymbolKind.CLASS, title, node, ctx, signature=f"{callee}('{title}')")
            return self.enter(ctx, title)
        if callee in _TEST_CALLS:
            symbol = self.make(
                SymbolKind.TEST_CASE,
                title,
                node,
                ctx,
                decorators=[callee],
                signature=f"{callee}('{title}')",
                body=self.line_text(node, 4000),
            )
            return self.inside(ctx, symbol.qualified_name)
        if callee in _HOOK_CALLS:
            self.make(SymbolKind.FIXTURE, callee, node, ctx, signature=f"{callee}()")
        return None

    # ------------------------------------------------------- expressions

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        function = ts.field(node, "function")
        arguments = ts.field(node, "arguments")
        if function is None:
            return None
        args = [self.line_text(a, 60) for a in ts.named_children(arguments)] if arguments else []
        if function.type == "member_expression":
            obj = ts.field(function, "object")
            prop = ts.field(function, "property")
            qualifier = self.line_text(obj, 80) if obj is not None else ""
            name = self._identifier_text(prop) if prop is not None else ""
        else:
            qualifier = ""
            name = self.line_text(function, 80)
        return CallSite(
            name=name,
            qualifier=qualifier,
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            text=self.line_text(node, 160),
            file=self.path,
        )

    def construction_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        constructor = ts.field(node, "constructor")
        arguments = ts.field(node, "arguments")
        if constructor is None:
            return None
        raw = self.line_text(constructor, 80)
        args = [self.line_text(a, 60) for a in ts.named_children(arguments)] if arguments else []
        return CallSite(
            name=raw.split(".")[-1],
            qualifier=raw.rsplit(".", 1)[0] if "." in raw else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            is_construction=True,
            text=self.line_text(node, 160),
            file=self.path,
        )

    def member_info(self, node: Any, ctx: Ctx) -> MemberAccess | None:
        prop = ts.field(node, "property")
        obj = ts.field(node, "object")
        if prop is None:
            return None
        return MemberAccess(
            name=self._identifier_text(prop),
            qualifier=self.line_text(obj, 80) if obj is not None else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            file=self.path,
        )


class TypeScriptExtractor(JavaScriptExtractor):
    language = "typescript"
