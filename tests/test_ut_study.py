"""Tests for studying (learning) the recognized UT folder."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.conventions.ut_study import study_test_folder
from ut_analyzer.models import UTCodeSample, UTKnowledge


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _discover(adapter, paths: list[Path]):
    return [adapter.parse_test_file(p, p.read_text(encoding="utf-8")) for p in paths]


def test_learns_helpers_fixtures_and_examples(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    test_file = _write(
        tests_dir / "test_calc.py",
        "import pytest\n"
        "from helpers import build_calculator\n"
        "\n"
        "def test_add_returns_sum():\n"
        "    calc = build_calculator()\n"
        "    assert calc.add(1, 2) == 3\n",
    )
    _write(
        tests_dir / "helpers.py",
        "import pytest\n"
        "\n"
        "DEFAULT_PRECISION = 4\n"
        "\n"
        "class FakeStore:\n"
        "    def get(self, key):\n"
        "        return None\n"
        "\n"
        "@pytest.fixture\n"
        "def calculator():\n"
        "    return build_calculator()\n"
        "\n"
        "def build_calculator():\n"
        "    return object()\n",
    )

    adapter = PythonAdapter()
    knowledge = study_test_folder(tests_dir, _discover(adapter, [test_file]), adapter)

    assert knowledge.test_dir == tests_dir
    assert knowledge.test_file_count == 1
    assert [h.name for h in knowledge.helper_files] == ["helpers.py"]
    assert "calculator" in knowledge.fixtures
    assert "FakeStore" in knowledge.mocks
    assert any(h.startswith("build_calculator") for h in knowledge.helpers)
    assert any(c.startswith("DEFAULT_PRECISION") for c in knowledge.constants)
    assert knowledge.example_tests and "calc.add(1, 2)" in knowledge.example_tests[0]
    assert not knowledge.is_empty()


def test_prompt_text_lists_reusable_symbols(tmp_path: Path) -> None:
    tests_dir = tmp_path / "ut"
    test_file = _write(
        tests_dir / "test_thing.py",
        "def test_thing_works():\n    assert True\n",
    )
    _write(
        tests_dir / "conftest.py",
        "import pytest\n\n@pytest.fixture\ndef client():\n    return 1\n",
    )

    adapter = PythonAdapter()
    knowledge = study_test_folder(tests_dir, _discover(adapter, [test_file]), adapter)
    text = knowledge.to_prompt_text()

    assert "client" in text
    assert "conftest.py" in text


def test_falls_back_to_common_parent_when_no_test_dir(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    test_file = _write(
        tests_dir / "test_a.py", "def test_a():\n    assert 1 == 1\n"
    )

    adapter = PythonAdapter()
    knowledge = study_test_folder(None, _discover(adapter, [test_file]), adapter)

    assert knowledge.test_dir == tests_dir.resolve()
    assert knowledge.test_file_count == 1


def test_empty_knowledge_without_tests() -> None:
    adapter = PythonAdapter()
    knowledge = study_test_folder(None, [], adapter)

    assert knowledge.is_empty()
    assert knowledge.to_prompt_text() == ""


def test_learns_how_python_tests_build_their_objects(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    test_file = _write(
        tests_dir / "test_engine.py",
        "import unittest\n"
        "\n"
        "from engine import Engine\n"
        "from helpers import build_engine\n"
        "\n"
        "class EngineTest(unittest.TestCase):\n"
        "    def setUp(self):\n"
        "        self.engine = Engine(config=DEFAULT_CONFIG)\n"
        "        self.engine.start()\n"
        "\n"
        "    def test_runs(self):\n"
        "        spare = build_engine(DEFAULT_CONFIG)\n"
        "        assert self.engine.run() == spare.run()\n",
    )
    _write(
        tests_dir / "helpers.py",
        "DEFAULT_CONFIG = 3\n"
        "\n"
        "def build_engine(config):\n"
        "    return object()\n",
    )

    adapter = PythonAdapter()
    knowledge = study_test_folder(tests_dir, _discover(adapter, [test_file]), adapter)

    setup = [s for s in knowledge.setup_patterns if s.title.startswith("setUp")]
    assert setup and "EngineTest" in setup[0].title
    assert "self.engine = Engine(config=DEFAULT_CONFIG)" in setup[0].code
    assert "self.engine.start()" in setup[0].code

    recipes = {s.title: s.code for s in knowledge.construction_recipes}
    assert "Engine" in recipes
    assert "self.engine.start()" in recipes["Engine"]

    assert any("self.engine = Engine(" in item for item in knowledge.member_declarations)
    assert any("build_engine(DEFAULT_CONFIG)" in item for item in knowledge.helper_usages)


def test_learns_how_cpp_tests_declare_and_create_members(tmp_path: Path) -> None:
    tests_dir = tmp_path / "UnitTest"
    test_file = _write(
        tests_dir / "SeekerUT.cpp",
        '#include "SeekerUT.h"\n'
        "\n"
        "TEST_F(SeekerTest, FindsTarget) {\n"
        "    EXPECT_EQ(seeker_->Find(1), 2);\n"
        "}\n",
    )
    _write(
        tests_dir / "SeekerUT.h",
        "#include <memory>\n"
        "\n"
        "class SeekerTest : public ::testing::Test {\n"
        "protected:\n"
        "    void SetUp() override {\n"
        "        reader_ = std::make_unique<FakeReader>(kTopicName);\n"
        "        reader_->Init();\n"
        "        seeker_ = new Seeker(*reader_);\n"
        "    }\n"
        "    void TearDown() override {\n"
        "        delete seeker_;\n"
        "    }\n"
        "\n"
        "    std::unique_ptr<FakeReader> reader_;\n"
        "    Seeker* seeker_;\n"
        "};\n",
    )

    adapter = CppAdapter()
    knowledge = study_test_folder(tests_dir, _discover(adapter, [test_file]), adapter)

    titles = [s.title for s in knowledge.setup_patterns]
    assert "SetUp() of SeekerTest" in titles
    setup = next(s for s in knowledge.setup_patterns if s.title.startswith("SetUp"))
    assert "std::make_unique<FakeReader>(kTopicName)" in setup.code
    assert "seeker_ = new Seeker(*reader_);" in setup.code

    assert any(s.title == "Seeker" for s in knowledge.construction_recipes)
    assert any(
        "std::unique_ptr<FakeReader> reader_" in item
        for item in knowledge.member_declarations
    )
    assert any("Seeker* seeker_" in item for item in knowledge.member_declarations)


def test_learns_the_fixture_constructor_style_of_the_ut_folder(tmp_path: Path) -> None:
    tests_dir = tmp_path / "UnitTest"
    test_file = _write(
        tests_dir / "SeekerUT.cpp",
        '#include "SeekerUT.h"\n'
        "\n"
        "SeekerUT::SeekerUT()\n"
        "{\n"
        "    m_reader = new FakeReader(kTopicName);\n"
        "    Init();\n"
        "}\n"
        "\n"
        "SeekerUT::~SeekerUT()\n"
        "{\n"
        "    delete m_reader;\n"
        "}\n"
        "\n"
        "void SeekerUT::Init()\n"
        "{\n"
        "    m_reader->Open();\n"
        "}\n"
        "\n"
        "TEST_F(SeekerUT, FindsTarget) {\n"
        "    EXPECT_EQ(m_reader->Find(1), 2);\n"
        "}\n",
    )
    _write(
        tests_dir / "SeekerUT.h",
        "class SeekerUT : public ::testing::Test {\n"
        "protected:\n"
        "    SeekerUT();\n"
        "    ~SeekerUT();\n"
        "    void Init();\n"
        "\n"
        "    FakeReader* m_reader;\n"
        "};\n",
    )

    adapter = CppAdapter()
    knowledge = study_test_folder(tests_dir, _discover(adapter, [test_file]), adapter)

    assert knowledge.lifecycle is not None
    assert knowledge.lifecycle.uses_constructor
    assert knowledge.lifecycle.uses_destructor
    assert knowledge.lifecycle.defines_out_of_line
    assert knowledge.lifecycle.init_methods == ("Init",)

    text = knowledge.to_prompt_text()
    assert "How this project's test fixtures are initialized and destroyed" in text
    assert "m_reader = new FakeReader(kTopicName);" in text

    small = knowledge.to_prompt_text(max_chars=300)
    assert len(small) <= 300
    assert "How this project's test fixtures are initialized and destroyed" not in small


def test_prompt_text_drops_whole_blocks_to_fit_budget(tmp_path: Path) -> None:
    knowledge = UTKnowledge(
        test_dir=tmp_path,
        fixtures=["EngineTest"],
        setup_patterns=[UTCodeSample(title="SetUp() of EngineTest", code="x" * 400)],
        example_tests=["y" * 400],
    )

    full = knowledge.to_prompt_text()
    small = knowledge.to_prompt_text(max_chars=200)

    assert "EngineTest" in full and "x" * 400 in full
    assert len(small) <= 200
    assert "x" * 400 not in small  # a snippet is dropped whole, never cut
