"""Tests for the repository-wide knowledge index and its Markdown file."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.knowledge.repo_indexer import (
    KNOWLEDGE_FILE_NAME,
    build_repo_knowledge,
    write_knowledge_markdown,
)
from ut_analyzer.models import ScannedFile, ScanResult


def _scan(root: Path, language: str) -> ScanResult:
    return ScanResult(
        root=root,
        files=[
            ScannedFile(path=p, language=language)
            for p in root.rglob("*")
            if p.is_file()
        ],
    )


def _cpp_repo(root: Path) -> None:
    (root / "Types.h").write_text(
        "#ifndef TYPES_H\n"
        "#define TYPES_H\n"
        "#define MAX_TOPICS 8\n"
        "enum class DdsState : int { Init = 0, Ready = 1, Failed = 2 };\n"
        "enum Color { RED, GREEN, BLUE };\n"
        "typedef unsigned int SampleId;\n"
        "using TopicName = std::string;\n"
        "static const int kMaxItems = 32;\n"
        "struct Sample {\n"
        "    SampleId id;\n"
        "    double value;\n"
        "};\n"
        "#endif\n",
        encoding="utf-8",
    )
    (root / "TempReader.h").write_text(
        "#include \"Types.h\"\n"
        "class TempReader : public ReaderBase {\n"
        "public:\n"
        "    TempReader(const TopicName& topic, int depth);\n"
        "    bool Read(Sample& out) const;\n"
        "protected:\n"
        "    void Reset();\n"
        "private:\n"
        "    int secret_;\n"
        "};\n",
        encoding="utf-8",
    )
    (root / "StatePublisher.h").write_text(
        "class StatePublisher {\n"
        "public:\n"
        "    void Publish(DdsState state);\n"
        "};\n",
        encoding="utf-8",
    )


def test_cpp_repository_index_finds_types_enums_and_constants(tmp_path: Path) -> None:
    _cpp_repo(tmp_path)

    knowledge = build_repo_knowledge(_scan(tmp_path, "cpp"), "cpp")

    state = knowledge.find("DdsState")
    assert state is not None
    assert state.kind == "enum"
    assert "Ready = 1" in state.members
    assert knowledge.find("Color") is not None

    sample = knowledge.find("Sample")
    assert sample is not None and sample.kind == "struct"
    assert any("id" in member for member in sample.members)

    reader = knowledge.find("TempReader")
    assert reader is not None and reader.kind == "class"
    assert reader.role == "reader"
    assert any("Read(Sample& out) const" in member for member in reader.members)
    assert any(member.startswith("[protected] void Reset") for member in reader.members)
    assert any(member.startswith("[private] int secret_") for member in reader.members)
    assert reader.include == '#include "TempReader.h"'

    assert knowledge.find("kMaxItems") is not None
    assert knowledge.find("MAX_TOPICS") is not None
    assert knowledge.find("SampleId") is not None
    assert knowledge.find("TopicName") is not None

    roles = knowledge.by_role()
    assert "publisher" in roles and roles["publisher"][0].name == "StatePublisher"


def test_prompt_text_only_contains_relevant_symbols(tmp_path: Path) -> None:
    _cpp_repo(tmp_path)
    knowledge = build_repo_knowledge(_scan(tmp_path, "cpp"), "cpp")

    text = knowledge.prompt_text_for(
        "void Engine::Run() { TempReader reader; state_ = DdsState::Ready; }"
    )

    assert "DdsState" in text
    assert "Ready = 1" in text
    assert "TempReader" in text
    assert "StatePublisher" not in text.split("Other types that exist")[0]


def test_markdown_file_is_written_into_the_repository(tmp_path: Path) -> None:
    _cpp_repo(tmp_path)
    knowledge = build_repo_knowledge(_scan(tmp_path, "cpp"), "cpp")

    path = write_knowledge_markdown(knowledge)

    assert path is not None and path.name == KNOWLEDGE_FILE_NAME
    content = path.read_text(encoding="utf-8")
    assert "# Repository knowledge" in content
    assert "DdsState" in content
    assert "TempReader" in content
    assert knowledge.markdown_path == path
    assert (path.parent / ".gitignore").is_file()


def test_python_repository_index(tmp_path: Path) -> None:
    (tmp_path / "device.py").write_text(
        "from enum import Enum\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "\n"
        "class Mode(Enum):\n"
        "    IDLE = 0\n"
        "    RUNNING = 1\n"
        "\n"
        "class TelemetryPublisher:\n"
        "    def __init__(self, topic: str, retries: int = MAX_RETRIES):\n"
        "        self._topic = topic\n"
        "\n"
        "    def publish(self, mode: Mode) -> bool:\n"
        "        return True\n"
        "\n"
        "def build_publisher(topic: str) -> TelemetryPublisher:\n"
        "    return TelemetryPublisher(topic)\n",
        encoding="utf-8",
    )

    knowledge = build_repo_knowledge(_scan(tmp_path, "python"), "python")

    mode = knowledge.find("Mode")
    assert mode is not None and mode.kind == "enum"
    assert "IDLE = 0" in mode.members

    publisher = knowledge.find("TelemetryPublisher")
    assert publisher is not None and publisher.role == "publisher"
    assert any("def publish" in member for member in publisher.members)
    assert publisher.include == "from device import <symbol>"

    assert knowledge.find("MAX_RETRIES") is not None
    assert knowledge.find("build_publisher") is not None


def test_cpp_initialization_recipe_is_learned_from_real_code(tmp_path: Path) -> None:
    _cpp_repo(tmp_path)
    (tmp_path / "engine.cpp").write_text(
        '#include "TempReader.h"\n'
        '#include "StatePublisher.h"\n'
        "void Engine::Start()\n"
        "{\n"
        "    TempReader reader(topicName_, 10);\n"
        "    reader.Reset();\n"
        "    Sample sample;\n"
        "    reader.Read(sample);\n"
        "\n"
        "    StatePublisher* publisher = new StatePublisher();\n"
        "    publisher->Publish(DdsState::Ready);\n"
        "}\n",
        encoding="utf-8",
    )

    knowledge = build_repo_knowledge(_scan(tmp_path, "cpp"), "cpp")

    reader = knowledge.find("TempReader")
    assert reader is not None and reader.init_examples
    recipe = reader.init_examples[0]
    assert recipe.file == "engine.cpp"
    assert "TempReader reader(topicName_, 10);" in recipe.code
    assert "reader.Read(sample);" in recipe.code
    assert "Sample sample;" not in recipe.code

    publisher = knowledge.find("StatePublisher")
    assert publisher is not None and publisher.init_examples
    assert "new StatePublisher()" in publisher.init_examples[0].code

    text = knowledge.prompt_text_for("TempReader reader; DdsState state;")
    assert "creates and initializes it like this" in text
    assert "| TempReader reader(topicName_, 10);" in text

    markdown = knowledge.to_markdown()
    assert "How this project creates/initializes it (from `engine.cpp`)" in markdown


def test_python_initialization_recipe(tmp_path: Path) -> None:
    (tmp_path / "publisher.py").write_text(
        "class TelemetryPublisher:\n"
        "    def __init__(self, topic, retries=3):\n"
        "        self.topic = topic\n"
        "\n"
        "    def connect(self):\n"
        "        return True\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from publisher import TelemetryPublisher\n"
        "\n"
        "def run():\n"
        "    pub = TelemetryPublisher('temperature', retries=5)\n"
        "    pub.connect()\n"
        "    return pub\n",
        encoding="utf-8",
    )

    knowledge = build_repo_knowledge(_scan(tmp_path, "python"), "python")

    entity = knowledge.find("TelemetryPublisher")
    assert entity is not None and entity.init_examples
    code = entity.init_examples[0].code
    assert "pub = TelemetryPublisher('temperature', retries=5)" in code
    assert "pub.connect()" in code
