from pathlib import Path

from ut_analyzer.adapters.cpp import access
from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.models import EnvironmentInfo

SOURCE = """
class Seek
{
public:
    Seek(int id);
    void start();
    int value() const;
protected:
    bool validate(int raw);
    int m_counter;
private:
    void reset();
    int m_secret = 0;
};
"""


def _write(tmp_path: Path) -> Path:
    (tmp_path / "seek.h").write_text(SOURCE, encoding="utf-8")
    source = tmp_path / "seek.cpp"
    source.write_text('#include "seek.h"\n', encoding="utf-8")
    return source


def test_analyze_access_groups_members_by_specifier():
    classes = access.analyze_access(SOURCE)
    seek = access.find_class(classes, "Seek")
    assert seek is not None
    assert "start" in seek.public and "value" in seek.public
    assert "validate" in seek.protected and "m_counter" in seek.protected
    assert "reset" in seek.private and "m_secret" in seek.private


def test_final_class_is_detected():
    classes = access.analyze_access("class Locked final : public Base { protected: void go(); };")
    locked = access.find_class(classes, "Locked")
    assert locked is not None and locked.is_final


def test_header_declares_accessor_for_protected_members(tmp_path: Path):
    source = _write(tmp_path)
    adapter = CppAdapter()
    result = adapter.new_test_header(tmp_path / "seekUT.cpp", source, EnvironmentInfo())
    assert result is not None
    _, content = result
    assert "class SeekTestAccess : public Seek" in content
    assert "using Seek::validate;" in content
    assert "using Seek::reset;" not in content


def test_access_notes_explain_how_to_reach_non_public_members(tmp_path: Path):
    source = _write(tmp_path)
    notes = CppAdapter().access_notes(source, SOURCE)
    assert "SeekTestAccess" in notes
    assert "protected" in notes.lower()
    assert "`validate`" in notes
    assert "`reset`" in notes
    assert "#define private public" in notes
    assert "TODO" in notes


def test_friend_declarations_are_reported():
    classes = access.analyze_access(
        "class Seek\n"
        "{\n"
        "private:\n"
        "    void reset();\n"
        "    friend class SeekTest;\n"
        "    FRIEND_TEST(SeekSuite, Reset);\n"
        "};\n"
    )
    seek = access.find_class(classes, "Seek")
    assert seek is not None
    assert "SeekTest" in seek.friends and "SeekSuite" in seek.friends
    notes = access.access_prompt_text(classes, "Seek")
    assert "SeekTest" in notes
