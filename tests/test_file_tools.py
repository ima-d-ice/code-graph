import pytest

from app.tools.file_tools import glob_search, grep_search


@pytest.fixture
def sample_repo(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "utils.py").write_text("def helper(): pass\n")
    (tmp_path / "lib" / "app").mkdir()
    (tmp_path / "lib" / "app" / "main.py").write_text("def run(): pass\n")
    (tmp_path / "main.py").write_text("def entry(): pass\n")
    (tmp_path / "decoy.txt").write_text("not python\n")
    return str(tmp_path)


def test_glob_search_relative_pattern(sample_repo, tmp_path):
    out = glob_search("**/*.py", str(tmp_path))
    assert "main.py" in out
    assert "lib/utils.py" in out
    assert "decoy.txt" not in out


def test_glob_search_with_path_kwarg(sample_repo, tmp_path):
    """The LLM commonly calls glob with a path arg (like grep) — must not crash."""
    out = glob_search("**/*.py", str(tmp_path), path="lib")
    assert "lib/utils.py" in out
    assert "lib/app/main.py" in out
    assert not any(line == "main.py" for line in out.splitlines())


def test_glob_search_path_escape_blocked(sample_repo, tmp_path):
    out = glob_search("**/*.py", str(tmp_path), path="../")
    assert "Error: Access denied" in out


def test_grep_search_with_path(sample_repo, tmp_path):
    out = grep_search("def run", "lib", str(tmp_path))
    assert "lib/app/main.py" in out
