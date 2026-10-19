"""Framework detection must not depend on how many folders were scanned."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.core.detection.test_content_sampler import sample_test_contents
from ut_analyzer.models import ScannedFile

_GTEST_UT = """\
#include "gtest/gtest.h"
#include "seekUT.h"

TEST_F(SeekUT, ReturnsZero)
{
    EXPECT_EQ(0, 0);
}
"""

_DECOY = """\
// A production file whose name merely looks test-related.
void TestHarnessRun() {}
"""


def _scanned(path: Path, is_test: bool = False) -> ScannedFile:
    return ScannedFile(path=path, language="cpp", is_test=is_test, size=path.stat().st_size)


def _make_repo(tmp_path: Path, decoy_count: int) -> tuple[Path, Path, list[ScannedFile]]:
    repo = tmp_path / "repo"
    ut_dir = repo / "UnitTest"
    src = repo / "src"
    ut_dir.mkdir(parents=True)
    src.mkdir(parents=True)

    ut_file = ut_dir / "seekUT.cpp"
    ut_file.write_text(_GTEST_UT, encoding="utf-8")
    files = [_scanned(ut_file, is_test=True)]

    for index in range(decoy_count):
        decoy = src / f"TestHarness{index}.cpp"
        decoy.write_text(_DECOY, encoding="utf-8")
        files.append(_scanned(decoy, is_test=True))

    # the UT file is walked last in a full-repository scan
    files.reverse()
    return repo, ut_dir, files


def test_ut_folder_contents_are_sampled_before_test_looking_files(tmp_path: Path) -> None:
    repo, ut_dir, files = _make_repo(tmp_path, decoy_count=60)
    contents = sample_test_contents(files, "cpp", scan_root=repo, test_dir=ut_dir)
    assert "gtest/gtest.h" in contents[0]


def test_full_repo_scan_detects_the_same_framework_as_a_ut_folder_scan(
    tmp_path: Path,
) -> None:
    repo, ut_dir, files = _make_repo(tmp_path, decoy_count=60)
    adapter = CppAdapter()

    ut_only = [f for f in files if f.path.parent == ut_dir]
    scoped = adapter.detect_environment(repo, ut_only, ut_dir)
    full = adapter.detect_environment(repo, files, ut_dir)

    assert scoped.test_framework == "GoogleTest"
    assert full.test_framework == scoped.test_framework


def test_ut_folder_is_auto_resolved_when_no_test_dir_is_configured(
    tmp_path: Path,
) -> None:
    repo, _, files = _make_repo(tmp_path, decoy_count=60)
    env = CppAdapter().detect_environment(repo, files)
    assert env.test_framework == "GoogleTest"
