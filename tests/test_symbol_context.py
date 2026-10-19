"""Tests for the dependency/API context collected for the generation prompt."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.generation.symbol_context import (
    collect_symbol_context,
    condense_declarations,
    constructors_prompt_text,
    extract_constructors,
    include_directive_for,
)
from ut_analyzer.models import ScannedFile, ScanResult


def _scan(root: Path, language: str) -> ScanResult:
    return ScanResult(
        root=root,
        files=[ScannedFile(path=p, language=language) for p in root.rglob("*") if p.is_file()],
    )


def test_cpp_include_declarations_are_collected(tmp_path: Path) -> None:
    (tmp_path / "Types.h").write_text(
        "#ifndef TYPES_H\n"
        "#define TYPES_H\n"
        "enum class DdsState : int { Init = 0, Ready = 1 };\n"
        "static const int kMaxItems = 32;\n"
        "class ReaderBase {\n"
        "public:\n"
        "    ReaderBase(const std::string& topic);\n"
        "    bool Read(Sample& out)\n"
        "    {\n"
        "        return true;\n"
        "    }\n"
        "};\n"
        "#endif\n",
        encoding="utf-8",
    )
    source = tmp_path / "Engine.cpp"
    source.write_text(
        '#include "Types.h"\n'
        "void Engine::Run()\n"
        "{\n"
        "    ReaderBase reader(\"topic\");\n"
        "    state_ = DdsState::Ready;\n"
        "}\n",
        encoding="utf-8",
    )

    context = collect_symbol_context(
        source=source,
        language="cpp",
        source_code=source.read_text(encoding="utf-8"),
        scan=_scan(tmp_path, "cpp"),
    )

    text = context.to_prompt_text()
    assert "Types.h" in text
    assert "enum class DdsState" in text
    assert "kMaxItems" in text
    # function bodies are dropped, signatures kept
    assert "bool Read(Sample& out);" in text
    assert "return true;" not in text
    assert ("enum", "DdsState", "Types.h") in context.symbols


def test_python_imports_are_collected(tmp_path: Path) -> None:
    (tmp_path / "models.py").write_text(
        "MAX_ITEMS = 32\n\n\n"
        "class Reader:\n"
        "    def read(self, timeout: float) -> str:\n"
        "        return 'x'\n",
        encoding="utf-8",
    )
    source = tmp_path / "engine.py"
    source.write_text(
        "from models import MAX_ITEMS, Reader\n\n\ndef run():\n    return Reader()\n",
        encoding="utf-8",
    )

    context = collect_symbol_context(
        source=source,
        language="python",
        source_code=source.read_text(encoding="utf-8"),
        scan=_scan(tmp_path, "python"),
    )

    text = context.to_prompt_text()
    assert "models.py" in text
    assert "MAX_ITEMS = 32" in text
    assert "def read(self, timeout: float) -> str:" in text
    assert "return 'x'" not in text


def test_no_scan_yields_empty_context(tmp_path: Path) -> None:
    source = tmp_path / "engine.py"
    source.write_text("x = 1\n", encoding="utf-8")

    context = collect_symbol_context(
        source=source, language="python", source_code="x = 1\n", scan=None
    )

    assert context.is_empty()
    assert context.to_prompt_text() == ""


CPP_CLASS = """
class Seek
{
public:
    Seek(int id);
    explicit Seek(const std::string& name, int retries = 3);
    Seek(const Seek& other) = delete;
    ~Seek();
    void start();
};
"""


def test_cpp_constructors_are_extracted() -> None:
    found = extract_constructors(
        condense_declarations(CPP_CLASS, "cpp"),
        "cpp",
        file_name="seek.h",
        include='#include "seek.h"',
    )

    signatures = [item.signature for item in found]
    assert "Seek(int id)" in signatures
    assert "Seek(const std::string& name, int retries = 3)" in signatures
    # deleted constructors are not offered to the model
    assert all("= delete" not in sig for sig in signatures)
    bounds = {item.signature: item.arg_bounds() for item in found}
    assert bounds["Seek(int id)"] == (1, 1)
    assert bounds["Seek(const std::string& name, int retries = 3)"] == (1, 2)

    text = constructors_prompt_text(found)
    assert "Constructors that actually exist" in text
    assert '#include "seek.h"' in text


def test_out_of_line_cpp_constructor_is_extracted() -> None:
    source = 'Seek::Seek(int id)\n{\n    m_id = id;\n}\n'
    found = extract_constructors(condense_declarations(source, "cpp"), "cpp")
    assert [item.signature for item in found] == ["Seek(int id)"]


def test_python_constructor_drops_self() -> None:
    module = (
        "class Reader:\n"
        "    def __init__(self, path, timeout=1.0):\n"
        "        self.path = path\n"
        "    def read(self):\n"
        "        return 1\n"
    )
    found = extract_constructors(
        condense_declarations(module, "python"), "python", file_name="reader.py"
    )
    assert [item.signature for item in found] == ["Reader(path, timeout=1.0)"]
    assert found[0].arg_bounds() == (1, 2)


def test_include_directives_are_reported(tmp_path: Path) -> None:
    (tmp_path / "Types.h").write_text(
        "class ReaderBase {\npublic:\n    ReaderBase(int id);\n};\n", encoding="utf-8"
    )
    source = tmp_path / "Engine.cpp"
    source.write_text(
        '#include "Types.h"\nvoid Engine::Run()\n{\n    ReaderBase r(1);\n}\n',
        encoding="utf-8",
    )

    context = collect_symbol_context(
        source=source,
        language="cpp",
        source_code=source.read_text(encoding="utf-8"),
        scan=_scan(tmp_path, "cpp"),
    )

    assert context.include_for("Types.h") == '#include "Types.h"'
    text = context.to_prompt_text()
    assert "MANDATORY include/import lines" in text
    assert '#include "Types.h"' in text
    assert any(item.type_name == "ReaderBase" for item in context.constructors)


def test_include_directive_for_python_module(tmp_path: Path) -> None:
    module = tmp_path / "pkg" / "models.py"
    module.parent.mkdir()
    module.write_text("X = 1\n", encoding="utf-8")

    directive = include_directive_for(module, "python", tmp_path)

    assert directive == "from pkg.models import <symbol>"
