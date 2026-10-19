"""Shared tree walking used by every language-specific extractor."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...models.semantics import (
    CallSite,
    ExtractionSource,
    FileSemantics,
    ImportDecl,
    MemberAccess,
    ParamDecl,
    SymbolDecl,
    SymbolKind,
    scope_separator,
)
from . import ts_support as ts

#: Sentinel returned by :meth:`BaseExtractor.handle` to stop descending.
SKIP = object()

_OPTIONAL_PARAM_NODES = {
    "optional_parameter_declaration",
    "default_parameter",
    "typed_default_parameter",
    "assignment_pattern",
    "optional_parameter",
}
_VARIADIC_PARAM_NODES = {
    "variadic_parameter_declaration",
    "variadic_parameter",
    "spread_parameter",
    "list_splat_pattern",
    "dictionary_splat_pattern",
    "rest_pattern",
    "rest_parameter",
}


@dataclass(frozen=True)
class Ctx:
    """Where in the file we currently are."""

    scope: str = ""
    enclosing: str = ""
    access: str = "public"


class BaseExtractor:
    """Recursive-descent extractor over a tree-sitter tree.

    Subclasses declare the node types that mean "call", "construction" and
    "member access" for their grammar and override :meth:`handle` to turn
    declaration nodes into :class:`SymbolDecl` objects.
    """

    language = ""
    call_nodes: tuple[str, ...] = ()
    construction_nodes: tuple[str, ...] = ()
    member_nodes: tuple[str, ...] = ()
    default_access = "public"

    def __init__(self, path: Path | None, data: bytes) -> None:
        self.path = path
        self.data = data
        self.symbols: list[SymbolDecl] = []
        self.calls: list[CallSite] = []
        self.accesses: list[MemberAccess] = []
        self.imports: list[ImportDecl] = []

    @staticmethod
    def sanitize(code: str) -> str:
        """Source rewritten so the grammar copes with it (same length).

        Only used when the first parse reported errors. The default keeps the
        source as it is.
        """
        return code

    # ------------------------------------------------------------------ run

    def run(self, root: Any) -> FileSemantics:
        self._walk(root, Ctx(access=self.default_access))
        self.finish()
        return FileSemantics(
            path=self.path,
            language=self.language,
            source=ExtractionSource.TREE_SITTER,
            parse_ok=not ts.has_error(root),
            symbols=self.symbols,
            calls=self.calls,
            member_accesses=self.accesses,
            imports=self.imports,
        )

    def finish(self) -> None:
        """Hook for post-processing once the whole tree was visited."""

    def _walk(self, node: Any, ctx: Ctx) -> None:
        child_ctx = self.handle(node, ctx)
        if child_ctx is SKIP:
            return
        if child_ctx is None:
            child_ctx = ctx
        self._record_expression(node, ctx)
        for child in node.children:
            self._walk(child, child_ctx)  # type: ignore[arg-type]

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        """Turn a declaration node into symbols. Return the ctx for children."""
        return None

    # -------------------------------------------------------- expressions

    def _record_expression(self, node: Any, ctx: Ctx) -> None:
        if node.type in self.call_nodes:
            call = self.call_info(node, ctx)
            if call is not None:
                call.target = call.target or self.binding_target(node)
                self.calls.append(call)
        elif node.type in self.construction_nodes:
            call = self.construction_info(node, ctx)
            if call is not None:
                call.target = call.target or self.binding_target(node)
                self.calls.append(call)
        elif node.type in self.member_nodes:
            access = self.member_info(node, ctx)
            if access is not None:
                self.accesses.append(access)

    #: Parent nodes that bind an expression to a name, and the field holding it.
    binding_nodes: dict[str, str] = {
        "assignment": "left",
        "assignment_expression": "left",
        "short_var_declaration": "left",
        "assignment_statement": "left",
        "variable_declarator": "name",
        "var_spec": "name",
        "init_declarator": "declarator",
    }

    def binding_target(self, node: Any) -> str:
        """Name the expression's value is assigned to, walking up a few parents."""
        current = node.parent
        for _ in range(3):
            if current is None:
                return ""
            field_name = self.binding_nodes.get(current.type)
            if field_name is not None:
                bound = ts.field(current, field_name)
                return self._identifier_text(bound) if bound is not None else ""
            current = current.parent
        return ""

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        return None

    def construction_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        return None

    def member_info(self, node: Any, ctx: Ctx) -> MemberAccess | None:
        return None

    # ------------------------------------------------------------ helpers

    def text(self, node: Any) -> str:
        return ts.text_of(node, self.data)

    def line_text(self, node: Any, limit: int = 200) -> str:
        return ts.one_line(node, self.data, limit)

    def qualify(self, scope: str, name: str) -> str:
        if not scope:
            return name
        return f"{scope}{scope_separator(self.language)}{name}"

    def add(self, symbol: SymbolDecl) -> SymbolDecl:
        symbol.language = self.language
        symbol.file = self.path
        symbol.source = ExtractionSource.TREE_SITTER
        self.symbols.append(symbol)
        return symbol

    def make(
        self,
        kind: SymbolKind,
        name: str,
        node: Any,
        ctx: Ctx,
        **extra: Any,
    ) -> SymbolDecl:
        fields: dict[str, Any] = {
            "line": ts.line_of(node),
            "end_line": ts.end_line_of(node),
            "scope": ctx.scope,
            "access": ctx.access,
        }
        fields.update(extra)
        return self.add(SymbolDecl(kind=kind, name=name, **fields))

    def enter(self, ctx: Ctx, name: str, *, access: str | None = None) -> Ctx:
        """Child context inside a named scope (namespace/class/module)."""
        return Ctx(
            scope=self.qualify(ctx.scope, name),
            enclosing=ctx.enclosing,
            access=access if access is not None else self.default_access,
        )

    def inside(self, ctx: Ctx, qualified_name: str) -> Ctx:
        """Child context inside a function body."""
        return replace(ctx, enclosing=qualified_name)

    # ---------------------------------------------------------- parameters

    def params_from(self, container: Any, *, drop_first: bool = False) -> list[ParamDecl]:
        """Extract parameters from any of the grammars' parameter containers."""
        if container is None:
            return []
        params: list[ParamDecl] = []
        for child in ts.named_children(container):
            if child.type in ("comment", "line_comment", "block_comment"):
                continue
            param = self._param_from(child)
            if param is not None:
                params.append(param)
        if drop_first and params:
            params = params[1:]
        return params

    def _param_from(self, node: Any) -> ParamDecl | None:
        text = self.line_text(node, 120)
        if not text or text in ("(", ")", ","):
            return None
        if node.type in _VARIADIC_PARAM_NODES:
            return ParamDecl(
                name=self._param_name(node),
                type_name="",
                default="...",
                text=text,
            )
        default_node = (
            ts.field(node, "value")
            or ts.field(node, "default_value")
            or ts.field(node, "right")
        )
        type_node = ts.field(node, "type")
        return ParamDecl(
            name=self._param_name(node),
            type_name=self.line_text(type_node, 80) if type_node else "",
            default=self.line_text(default_node, 60) if default_node else "",
            text=text,
        )

    def _param_name(self, node: Any) -> str:
        for field_name in ("name", "declarator", "pattern", "left"):
            child = ts.field(node, field_name)
            if child is not None:
                return self._identifier_text(child)
        if node.type in ("identifier", "type_identifier"):
            return self.text(node)
        inner = ts.first_of_type(node, "identifier")
        return self.text(inner) if inner is not None else ""

    def _identifier_text(self, node: Any) -> str:
        if node is None:
            return ""
        if node.type in ("identifier", "type_identifier", "field_identifier",
                         "property_identifier", "package_identifier"):
            return self.text(node)
        inner = ts.first_of_type(node, "identifier", "field_identifier", "type_identifier")
        if inner is not None:
            return self.text(inner)
        return self.line_text(node, 60)
