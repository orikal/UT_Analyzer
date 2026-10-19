"""Go semantic extraction rules."""

from __future__ import annotations

from typing import Any

from ...models.semantics import CallSite, ImportDecl, MemberAccess, SymbolKind
from . import ts_support as ts
from .extractor_base import SKIP, BaseExtractor, Ctx

_TEST_PREFIXES = ("Test", "Benchmark", "Fuzz", "Example")


class GoExtractor(BaseExtractor):
    language = "go"
    call_nodes = ("call_expression",)
    construction_nodes = ("composite_literal",)
    member_nodes = ("selector_expression",)
    default_access = "public"

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind == "source_file":
            return self._package(node, ctx)
        if kind == "package_clause":
            return SKIP
        if kind == "import_spec":
            return self._import(node)
        if kind == "type_spec":
            return self._type(node, ctx)
        if kind in ("const_spec", "var_spec"):
            return self._value(node, ctx, kind)
        if kind == "function_declaration":
            return self._function(node, ctx)
        if kind == "method_declaration":
            return self._method(node, ctx)
        return None

    def _package(self, node: Any, ctx: Ctx) -> Ctx | None:
        """The package clause scopes every declaration in the file."""
        clause = ts.first_of_type(node, "package_clause")
        if clause is None:
            return None
        name = self.line_text(clause, 80).replace("package", "").strip()
        if not name:
            return None
        self.make(SymbolKind.NAMESPACE, name, clause, ctx)
        return self.enter(ctx, name)

    def _import(self, node: Any) -> object:
        path_node = ts.field(node, "path")
        module = self.text(path_node).strip('"') if path_node is not None else ""
        self.imports.append(
            ImportDecl(
                raw=self.line_text(node, 120),
                module=module,
                line=ts.line_of(node),
                file=self.path,
            )
        )
        return SKIP

    def _type(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        type_node = ts.field(node, "type")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        kind = SymbolKind.ALIAS
        members: list[str] = []
        if type_node is not None and type_node.type == "struct_type":
            kind = SymbolKind.STRUCT
            members = self._struct_fields(type_node, ctx, name)
        elif type_node is not None and type_node.type == "interface_type":
            kind = SymbolKind.INTERFACE
            members = self._interface_methods(type_node, ctx, name)
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            members=members,
            value=self.line_text(type_node, 80) if type_node is not None else "",
            access="public" if name[:1].isupper() else "private",
            signature=self.line_text(node, 160),
        )
        lowered = name.lower()
        if lowered.startswith(("mock", "fake", "stub")) or lowered.endswith(
            ("mock", "fake", "stub")
        ):
            symbol.kind = SymbolKind.MOCK
        return SKIP

    def _struct_fields(self, type_node: Any, ctx: Ctx, owner: str) -> list[str]:
        inner = self.enter(ctx, owner)
        names: list[str] = []
        body = ts.first_of_type(type_node, "field_declaration_list")
        for declaration in ts.children_of_type(body, "field_declaration") if body else []:
            name_node = ts.field(declaration, "name")
            if name_node is None:
                continue
            name = self._identifier_text(name_node)
            names.append(name)
            self.make(
                SymbolKind.FIELD,
                name,
                declaration,
                inner,
                return_type=self.line_text(ts.field(declaration, "type"), 60),
                access="public" if name[:1].isupper() else "private",
                signature=self.line_text(declaration, 120),
            )
        return names

    def _interface_methods(self, type_node: Any, ctx: Ctx, owner: str) -> list[str]:
        inner = self.enter(ctx, owner)
        names: list[str] = []
        for elem in ts.descendants(type_node, "method_elem", "method_spec"):
            name_node = ts.field(elem, "name")
            if name_node is None:
                continue
            name = self._identifier_text(name_node)
            names.append(name)
            self.make(
                SymbolKind.METHOD,
                name,
                elem,
                inner,
                params=self.params_from(ts.field(elem, "parameters")),
                return_type=self.line_text(ts.field(elem, "result"), 60),
                is_abstract=True,
                signature=self.line_text(elem, 160),
            )
        return names

    def _value(self, node: Any, ctx: Ctx, kind: str) -> object:
        if ctx.enclosing:
            return SKIP
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self._identifier_text(name_node)
        value = ts.field(node, "value")
        self.make(
            SymbolKind.CONSTANT if kind == "const_spec" else SymbolKind.VARIABLE,
            name,
            node,
            ctx,
            value=self.line_text(value, 80) if value is not None else "",
            access="public" if name[:1].isupper() else "private",
            signature=self.line_text(node, 120),
        )
        return SKIP

    def _function(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        params = self.params_from(ts.field(node, "parameters"))
        kind = SymbolKind.FUNCTION
        if name.startswith(_TEST_PREFIXES) and self._takes_testing_arg(params):
            kind = SymbolKind.TEST_CASE
        return self._emit_callable(node, ctx, kind, name, params, scope=ctx.scope)

    def _method(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        receiver = ts.field(node, "receiver")
        owner = ""
        if receiver is not None:
            for declaration in ts.descendants(receiver, "type_identifier"):
                owner = self.text(declaration)
                break
        return self._emit_callable(
            node,
            ctx,
            SymbolKind.METHOD,
            self.text(name_node),
            self.params_from(ts.field(node, "parameters")),
            scope=self.qualify(ctx.scope, owner) if owner else ctx.scope,
        )

    def _emit_callable(self, node, ctx, kind, name, params, *, scope):  # type: ignore[no-untyped-def]
        body = ts.field(node, "body")
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            scope=scope,
            params=params,
            return_type=self.line_text(ts.field(node, "result"), 60),
            access="public" if name[:1].isupper() else "private",
            signature=f"func {name}({', '.join(p.render() for p in params)})",
            body=self.text(body)[:6000] if body is not None else "",
        )
        if body is not None:
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    def _takes_testing_arg(self, params) -> bool:  # type: ignore[no-untyped-def]
        return any("testing." in p.text for p in params)

    # ------------------------------------------------------- expressions

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        function = ts.field(node, "function")
        arguments = ts.field(node, "arguments")
        if function is None:
            return None
        args = [self.line_text(a, 80) for a in ts.named_children(arguments)] if arguments else []
        if function.type == "selector_expression":
            operand = ts.field(function, "operand")
            field_node = ts.field(function, "field")
            qualifier = self.line_text(operand, 80) if operand is not None else ""
            name = self._identifier_text(field_node) if field_node is not None else ""
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
        type_node = ts.field(node, "type")
        if type_node is None:
            return None
        raw = self.line_text(type_node, 80)
        body = ts.field(node, "body")
        args = [self.line_text(a, 60) for a in ts.named_children(body)] if body else []
        return CallSite(
            name=raw.split(".")[-1].lstrip("*&"),
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
        field_node = ts.field(node, "field")
        operand = ts.field(node, "operand")
        if field_node is None:
            return None
        return MemberAccess(
            name=self._identifier_text(field_node),
            qualifier=self.line_text(operand, 80) if operand is not None else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            file=self.path,
        )
