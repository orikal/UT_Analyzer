"""Python semantic extraction rules."""

from __future__ import annotations

from typing import Any

from ...models.semantics import CallSite, ImportDecl, MemberAccess, SymbolKind
from . import ts_support as ts
from .extractor_base import SKIP, BaseExtractor, Ctx

_SELF_NAMES = {"self", "cls"}
_FIXTURE_DECORATORS = ("fixture", "pytest.fixture", "pytest_asyncio.fixture")
_TEST_PREFIXES = ("test_", "test")


class PythonExtractor(BaseExtractor):
    language = "python"
    call_nodes = ("call",)
    member_nodes = ("attribute",)
    default_access = "public"

    def __init__(self, path, data) -> None:  # type: ignore[no-untyped-def]
        super().__init__(path, data)
        self._decorators: list[str] = []

    def handle(self, node: Any, ctx: Ctx) -> Ctx | None | object:
        kind = node.type
        if kind == "import_statement":
            return self._import(node)
        if kind == "import_from_statement":
            return self._import_from(node)
        if kind == "decorated_definition":
            return self._decorated(node, ctx)
        if kind == "class_definition":
            return self._class(node, ctx)
        if kind == "function_definition":
            return self._function(node, ctx)
        if kind == "assignment":
            return self._assignment(node, ctx)
        return None

    # ---------------------------------------------------------- imports

    def _import(self, node: Any) -> object:
        for name in ts.fields(node, "name"):
            module = self.line_text(name, 120)
            self.imports.append(
                ImportDecl(
                    raw=self.line_text(node, 160),
                    module=module.split(" as ")[0].strip(),
                    line=ts.line_of(node),
                    file=self.path,
                )
            )
        return SKIP

    def _import_from(self, node: Any) -> object:
        module_node = ts.field(node, "module_name")
        module = self.line_text(module_node, 120) if module_node is not None else ""
        symbols = [
            self.line_text(name, 80).split(" as ")[0].strip()
            for name in ts.fields(node, "name")
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

    # ------------------------------------------------------ definitions

    def _decorated(self, node: Any, ctx: Ctx) -> object:
        self._decorators = [
            self.line_text(d, 120).lstrip("@")
            for d in ts.children_of_type(node, "decorator")
        ]
        definition = ts.field(node, "definition")
        if definition is not None:
            self._walk(definition, ctx)
        self._decorators = []
        return SKIP

    def _class(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        bases = [
            self.line_text(c, 80)
            for c in ts.named_children(ts.field(node, "superclasses"))
        ]
        decorators = self._take_decorators()
        symbol = self.make(
            SymbolKind.CLASS,
            name,
            node,
            ctx,
            base_types=bases,
            decorators=decorators,
            signature=f"class {name}({', '.join(bases)})" if bases else f"class {name}",
        )
        if self._looks_like_mock(name, bases):
            symbol.kind = SymbolKind.MOCK
        inner = self.enter(ctx, name)
        body = ts.field(node, "body")
        if body is not None:
            self._walk(body, inner)
        symbol.members = [s.name for s in self.symbols if s.scope == inner.scope]
        return SKIP

    def _function(self, node: Any, ctx: Ctx) -> object:
        name_node = ts.field(node, "name")
        if name_node is None:
            return SKIP
        name = self.text(name_node)
        decorators = self._take_decorators()
        is_method = bool(ctx.scope)
        params = self.params_from(ts.field(node, "parameters"))
        if is_method and params and params[0].name in _SELF_NAMES:
            params = params[1:]
        kind = self._function_kind(name, decorators, is_method)
        body = ts.field(node, "body")
        symbol = self.make(
            kind,
            name,
            node,
            ctx,
            params=params,
            return_type=self.line_text(ts.field(node, "return_type"), 60),
            decorators=decorators,
            access="private" if name.startswith("_") and not name.startswith("__") else
            ("private" if name.startswith("__") and not name.endswith("__") else "public"),
            signature=f"def {name}({', '.join(p.render() for p in params)})",
            body=self.text(body)[:6000] if body is not None else "",
        )
        if body is not None:
            self._walk(body, self.inside(ctx, symbol.qualified_name))
        return SKIP

    def _function_kind(self, name: str, decorators: list[str], is_method: bool) -> SymbolKind:
        if any(d.split("(")[0].endswith(_FIXTURE_DECORATORS) for d in decorators):
            return SymbolKind.FIXTURE
        if name.startswith(_TEST_PREFIXES) and name != "test":
            return SymbolKind.TEST_CASE
        if name == "__init__":
            return SymbolKind.CONSTRUCTOR
        if any(d.startswith("property") for d in decorators):
            return SymbolKind.PROPERTY
        return SymbolKind.METHOD if is_method else SymbolKind.FUNCTION

    def _assignment(self, node: Any, ctx: Ctx) -> Ctx | None:
        if ctx.enclosing:
            return None
        left = ts.field(node, "left")
        if left is None or left.type != "identifier":
            return None
        name = self.text(left)
        right = ts.field(node, "right")
        kind = SymbolKind.CONSTANT if name.isupper() else SymbolKind.VARIABLE
        self.make(
            kind,
            name,
            node,
            ctx,
            value=self.line_text(right, 120) if right is not None else "",
            signature=self.line_text(node, 160),
        )
        return None

    # ------------------------------------------------------- expressions

    def call_info(self, node: Any, ctx: Ctx) -> CallSite | None:
        function = ts.field(node, "function")
        arguments = ts.field(node, "arguments")
        if function is None:
            return None
        args = [self.line_text(a, 80) for a in ts.named_children(arguments)] if arguments else []
        qualifier = ""
        if function.type == "attribute":
            obj = ts.field(function, "object")
            attr = ts.field(function, "attribute")
            qualifier = self.line_text(obj, 80) if obj is not None else ""
            name = self.text(attr) if attr is not None else ""
        else:
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

    def member_info(self, node: Any, ctx: Ctx) -> MemberAccess | None:
        attribute = ts.field(node, "attribute")
        obj = ts.field(node, "object")
        if attribute is None:
            return None
        return MemberAccess(
            name=self.text(attribute),
            qualifier=self.line_text(obj, 80) if obj is not None else "",
            line=ts.line_of(node),
            enclosing=ctx.enclosing,
            file=self.path,
        )

    # ---------------------------------------------------------- helpers

    def _take_decorators(self) -> list[str]:
        decorators, self._decorators = self._decorators, []
        return decorators

    def _looks_like_mock(self, name: str, bases: list[str]) -> bool:
        lowered = name.lower()
        if lowered.startswith(("mock", "fake", "stub")) or lowered.endswith(
            ("mock", "fake", "stub")
        ):
            return True
        return any("Mock" in b for b in bases)
