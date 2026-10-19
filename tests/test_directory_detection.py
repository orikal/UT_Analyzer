"""Tests for automatic unit-test directory detection."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.detection.test_content_sampler import (
    detect_test_directory,
    planned_test_directory,
    preferred_test_directory,
)


def test_detects_tests_folder_at_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    assert detect_test_directory(repo) == tests.resolve()


def test_detects_java_test_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    java_tests = repo / "src" / "test" / "java"
    java_tests.mkdir(parents=True)
    assert detect_test_directory(repo) == java_tests.resolve()


def test_detects_ut_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ut_dir = repo / "UT"
    ut_dir.mkdir(parents=True)
    assert detect_test_directory(repo) == ut_dir.resolve()


def test_prefers_root_unittest_folder_over_other_candidates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    unit_test = repo / "UnitTest"
    unit_test.mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "UT").mkdir()
    assert detect_test_directory(repo) == unit_test.resolve()


def test_falls_back_to_root_ut_folder_when_no_unittest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ut_dir = repo / "UT"
    ut_dir.mkdir(parents=True)
    (repo / "tests").mkdir()
    assert detect_test_directory(repo) == ut_dir.resolve()


def test_ignores_nested_unittest_folder_for_root_priority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shallow = repo / "tests"
    shallow.mkdir(parents=True)
    (repo / "src" / "UnitTest").mkdir(parents=True)
    assert detect_test_directory(repo) == shallow.resolve()


def test_returns_none_when_no_test_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    assert detect_test_directory(repo) is None


def test_returns_root_when_repo_is_named_tests(tmp_path: Path) -> None:
    repo = tmp_path / "tests"
    repo.mkdir()
    assert detect_test_directory(repo) == repo.resolve()


def test_prefers_shallow_tests_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shallow = repo / "tests"
    nested = repo / "legacy" / "old" / "tests"
    shallow.mkdir(parents=True)
    nested.mkdir(parents=True)
    assert detect_test_directory(repo) == shallow.resolve()


def test_prefers_unittests_code_tests_over_everything(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    preferred = repo / "UnitTests" / "Code" / "tests"
    preferred.mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "UnitTest").mkdir()
    (repo / "UT").mkdir()
    assert detect_test_directory(repo) == preferred.resolve()


def test_preferred_path_matching_is_case_insensitive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    preferred = repo / "unittests" / "code" / "Tests"
    preferred.mkdir(parents=True)
    assert detect_test_directory(repo) == preferred.resolve()


def test_incomplete_preferred_chain_is_not_used(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "UnitTests" / "Code").mkdir(parents=True)
    shallow = repo / "tests"
    shallow.mkdir()
    assert detect_test_directory(repo) == shallow.resolve()


def test_planned_directory_is_the_preferred_chain_when_nothing_exists(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    planned = planned_test_directory(repo)
    assert planned == repo / "UnitTests" / "Code" / "tests"
    assert not planned.exists()  # analysis never creates it


def test_planned_directory_keeps_existing_folder_casing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "unittests").mkdir(parents=True)
    assert preferred_test_directory(repo) == repo / "unittests" / "Code" / "tests"


def test_planned_directory_uses_the_detected_folder_when_one_exists(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    assert planned_test_directory(repo) == tests.resolve()
