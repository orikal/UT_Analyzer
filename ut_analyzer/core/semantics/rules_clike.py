"""Java and C# semantic extraction rules (same shape, different node names)."""

from __future__ import annotations

from typing import Any

from ...models.semantics import CallSite, ImportDecl, MemberAccess, SymbolKind
from . import ts_support as ts
from .extractor_base import SKIP, BaseExtractor, Ctx

_ACCESS_WORDS = ("public", "protected", "private", "internal")

_JAVA_TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory",
                          "TestTemplate"}
_CSHARP_TEST_ATTRIBUTES = {"Fact", "Theory", "Test", "TestMethod", "TestCase"}
_MOCK_PREFIXES = ("mock", "fake", "stub")


class _ClassLikeExtractor(BaseExtractor):
    """Shared behaviour for the two curly-brace, class-per-file languages."""

    type_nodes: dict[str, SymbolKind] = {}
    enum_node = ""
    enum_member_nodes: tuple[str, ...] = ()
    method_nodes: tuple[str, ...] = ()
    constructor_nodes: tuple[str, ...] = ()
    field_nodes: tuple[str, ...] = ()
    body_field = "body"
    params_field = "parameters"
    return_field = "type"
    test_markers: frozenset[str] = frozenset()
    unit_nodes: tuple[str, ...] = ()

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind in self.unit_nodes:
            return self.file_scope(node, ctx)
        if kind in self.type_nodes:
            return self._type(node, ctx, self.type_nodes[kind])
        if kind == self.enum_node:
            return self._enum(node, ctx)
        if kind in self.constructor_nodes:
            return self._callable(node, ctx, SymbolKind.CONSTRUCTOR)
        if kind in self.method_nodes:
            return self._callable(node, ctx, SymbolKind.METHOD)
        if kind in self.field_nodes:
            return self._field(node, ctx)
        return self.handle_extra(node, ctx)

    def handle_extra(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        return None

    def file_scope(self, node: Any, ctx: Ctx) -> Ctx | None:
        return None

    # ------------------------------------------------------------ pieces

    def _type(self, node: Any, ctx: Ctx, kind: SymbolKind) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return None
        name = self.text(name_node)
        bases = self.base_types(node)
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            base_types=bases,
            access=self.access_of(node, ctx),
            decorators=self.annotations(node),
            signature=f"{kind.value} {name}" + (f" : {', '.join(bases)}" if bases else ""),
        )
        if self._looks_like_mock(name, bases):
            symbol.kind = SymbolKind.MOCK
        inner = self.enter(ctx, name)
        body = ts.field(node, self.body_field)
        if body is not None:
            self._walk(body, inner)
        symbol.members = [s.name for s in self.symbols if s.scope == inner.scope]
        return SKIP

    def _enum(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        body = ts.field(node, self.body_field)
        inner = self.enter(ctx, name)
        values: list[str] = []
        for member in ts.children_of_type(body, *self.enum_member_nodes) if body else []:
            member_name = ts.field(member, "name")
            if member_name is None:
                continue
            values.append(self.text(member_name))
            self.make(
                SymbolKind.ENUM_MEMBER,
                self.text(member_name),
                member,
                inner,
                signature=self.line_text(member, 80),
            )
        self.make(
            SymbolKind.ENUM,
            name,
            node,
            ctx,
            members=values,
            access=self.access_of(node, ctx),
            signature=f"enum {name}",
        )
        return SKIP

    def _callable(self, node: Any, ctx: Ctx, kind: SymbolKind) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        annotations = self.annotations(node)
        params = self.params_from(ts.field(node, self.params_field))
        if kind is SymbolKind.METHOD and self._is_test(name, annotations):
            kind = SymbolKind.TEST_CASE
        body = ts.field(node, self.body_field)
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            params=params,
            return_type=self.line_text(ts.field(node, self.return_field), 80),
            decorators=annotations,
            access=self.access_of(node, ctx),
            is_static="static" in self.modifier_text(node),
            signature=f"{name}({', '.join(p.render() for p in params)})",
            body=self.text(body)[:6000] if body is not None else "",
        )
        if body is not None:
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    def _field(self, node: Any, ctx: Ctx) -> object:
        modifiers = self.modifier_text(node)
        constant = any(word in modifiers for word in ("final", "const", "readonly"))
        for name, value in self.field_names(node):
            self.make(
                SymbolKind.CONSTANT if constant else SymbolKind.FIELD,
                name,
                node,
                ctx,
                value=value,
                access=self.access_of(node, ctx),
                is_static="static" in modifiers,
                signature=self.line_text(node, 160),
            )
        return SKIP

    # ---------------------------------------------------------- helpers

    def field_names(self, node: Any) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for declarator in ts.descendants(node, "variable_declarator"):
            name_node = ts.field(declarator, "name")
            value_node = ts.field(declarator, "value")
            if name_node is not None:
                found.append(
                    (
                        self._identifier_text(name_node),
                        self.line_text(value_node, 80) if value_node is not None else "",
                    )
                )
        return found

    def modifier_text(self, node: Any) -> str:
        return " ".join(
            self.line_text(child, 120)
            for child in ts.children_of_type(node, "modifiers", "modifier")
        )

    def access_of(self, node: Any, ctx: Ctx) -> str:
        text = self.modifier_text(node)
        for word in _ACCESS_WORDS:
            if word in text.split():
                return word
        return ctx.access

    def base_types(self, node: Any) -> list[str]:
        return []

    def annotations(self, node: Any) -> list[str]:
        return []

    def _is_test(self, name: str, annotations: list[str]) -> bool:
        return any(a.split("(")[0] in self.test_markers for a in annotations)

    def _looks_like_mock(self, name: str, bases: list[str]) -> bool:
        lowered = name.lower()
        return lowered.startswith(_MOCK_PREFIXES) or lowered.endswith(_MOCK_PREFIXES)

    def member_info(self, node: Any, ctx: Ctx) -> MemberAccess | None:
        name_node = ts.field(node, "name") or ts.field(node, "field")
        obj = ts.field(node, "object") or ts.field(node, "expression")
        if name_node is None:
            return None
        return MemberAccess(
            name=self._identifier_text(name_node),
            qualifier=self.line_text(obj, 80) if obj is not None else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            file=self.path,
        )

    def _call(self, node: Any, ctx: Ctx, *, construction: bool) -> CallSite | None:
        arguments = ts.field(node, "arguments")
        args = [self.line_text(a, 80) for a in ts.named_children(arguments)] if arguments else []
        if construction:
            type_node = ts.field(node, "type")
            raw = self.line_text(type_node, 100) if type_node is not None else ""
            name = raw.split(".")[-1].split("<")[0]
            qualifier = raw.rsplit(".", 1)[0] if "." in raw else ""
        else:
            name_node = ts.field(node, "name") or ts.field(node, "function")
            obj = ts.field(node, "object")
            if name_node is None:
                return None
            raw = self._identifier_text(name_node)
            if "." in raw and obj is None:
                qualifier, _, name = raw.rpartition(".")
            else:
                name = raw
                qualifier = self.line_text(obj, 80) if obj is not None else ""
        if not name:
            return None
        return CallSite(
            name=name,
            qualifier=qualifier,
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            arg_count=len(args),
            args=args,
            is_construction=construction,
            text=self.line_text(node, 160),
            file=self.path,
        )

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        return self._call(node, ctx, construction=False)

    def construction_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        return self._call(node, ctx, construction=True)


class JavaExtractor(_ClassLikeExtractor):
    language = "java"
    unit_nodes = ("program",)
    call_nodes = ("method_invocation",)
    construction_nodes = ("object_creation_expression",)
    member_nodes = ("field_access",)
    type_nodes = {
        "class_declaration": SymbolKind.CLASS,
        "interface_declaration": SymbolKind.INTERFACE,
        "record_declaration": SymbolKind.CLASS,
    }
    enum_node = "enum_declaration"
    enum_member_nodes = ("enum_constant",)
    method_nodes = ("method_declaration",)
    constructor_nodes = ("constructor_declaration",)
    field_nodes = ("field_declaration",)
    test_markers = frozenset(_JAVA_TEST_ANNOTATIONS)
    default_access = "package"

    def file_scope(self, node: Any, ctx: Ctx) -> Ctx | None:
        package = ts.first_of_type(node, "package_declaration")
        if package is None:
            return None
        name = self.line_text(package, 120).replace("package", "").strip().rstrip(";")
        if not name:
            return None
        self.make(SymbolKind.NAMESPACE, name, package, ctx)
        return Ctx(scope=name, access=self.default_access)

    def handle_extra(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        if node.type == "import_declaration":
            raw = self.line_text(node, 160)
            module = raw.replace("import", "").replace("static", "").strip().rstrip(";")
            self.imports.append(
                ImportDecl(
                    raw=raw,
                    module=module,
                    symbols=[module.rsplit(".", 1)[-1]] if "." in module else [],
                    line=ts.line_of(node),
                    file=self.path,
                )
            )
            return SKIP
        return None

    def base_types(self, node: Any) -> list[str]:
        bases: list[str] = []
        superclass = ts.field(node, "superclass")
        if superclass is not None:
            bases.append(self.line_text(superclass, 80).replace("extends", "").strip())
        interfaces = ts.field(node, "interfaces")
        if interfaces is not None:
            text = self.line_text(interfaces, 160)
            text = text.replace("implements", "").strip()
            bases.extend(part.strip() for part in text.split(",") if part.strip())
        return [b for b in bases if b]

    def annotations(self, node: Any) -> list[str]:
        found: list[str] = []
        for modifiers in ts.children_of_type(node, "modifiers"):
            for annotation in ts.children_of_type(modifiers, "marker_annotation", "annotation"):
                name = ts.field(annotation, "name")
                if name is not None:
                    found.append(self._identifier_text(name))
        return found


class CSharpExtractor(_ClassLikeExtractor):
    language = "csharp"
    unit_nodes = ("compilation_unit",)
    call_nodes = ("invocation_expression",)
    construction_nodes = ("object_creation_expression",)
    member_nodes = ("member_access_expression",)
    type_nodes = {
        "class_declaration": SymbolKind.CLASS,
        "struct_declaration": SymbolKind.STRUCT,
        "interface_declaration": SymbolKind.INTERFACE,
        "record_declaration": SymbolKind.CLASS,
    }
    enum_node = "enum_declaration"
    enum_member_nodes = ("enum_member_declaration",)
    method_nodes = ("method_declaration", "property_declaration")
    constructor_nodes = ("constructor_declaration",)
    field_nodes = ("field_declaration",)
    return_field = "returns"
    params_field = "parameters"
    test_markers = frozenset(_CSHARP_TEST_ATTRIBUTES)
    default_access = "private"

    def handle_extra(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind == "using_directive":
            raw = self.line_text(node, 160)
            module = raw.replace("using", "").strip().rstrip(";")
            self.imports.append(
                ImportDecl(raw=raw, module=module, line=ts.line_of(node), file=self.path)
            )
            return SKIP
        if kind in ("namespace_declaration", "file_scoped_namespace_declaration"):
            name_node = ts.field(node, "name")
            if name_node is None:
                return None
            name = self.line_text(name_node, 120)
            self.make(SymbolKind.NAMESPACE, name, node, ctx)
            return self.enter(ctx, name, access=self.default_access)
        return None

    def base_types(self, node: Any) -> list[str]:
        clause = ts.first_of_type(node, "base_list")
        if clause is None:
            return []
        return [
            self.line_text(child, 80)
            for child in ts.named_children(clause)
            if self.line_text(child, 80)
        ]

    def annotations(self, node: Any) -> list[str]:
        found: list[str] = []
        for attr_list in ts.children_of_type(node, "attribute_list"):
            for attribute in ts.children_of_type(attr_list, "attribute"):
                name = ts.field(attribute, "name")
                found.append(
                    self._identifier_text(name)
                    if name is not None
                    else self.line_text(attribute, 60)
                )
        return found

    def field_names(self, node: Any) -> list[tuple[str, str]]:
        found = super().field_names(node)
        if found:
            return found
        name_node = ts.field(node, "name")
        if name_node is not None:
            return [(self._identifier_text(name_node), "")]
        return []
