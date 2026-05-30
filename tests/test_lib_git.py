"""tests/test_lib_git.py - lib_git.in_git_repo のテスト (U-115)"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import lib_git  # noqa: E402


def _has_git() -> bool:
    return shutil.which("git") is not None


class TestInGitRepo:
    @pytest.mark.skipif(not _has_git(), reason="git not available")
    def test_in_git_repo_true_in_repo(self, tmp_path):
        """tmp に git init したディレクトリは True"""
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10, check=True,
        )
        assert lib_git.in_git_repo(tmp_path) is True

    def test_in_git_repo_false_outside_repo(self, tmp_path):
        """git 管理外の隔離 tmp_path は False（git init しない）"""
        # tmp_path は pytest が用意する隔離ディレクトリで .git を持たない
        assert lib_git.in_git_repo(tmp_path) is False

    def test_in_git_repo_missing_cwd_returns_false(self, tmp_path):
        """存在しない cwd は例外を漏らさず False"""
        missing = tmp_path / "does-not-exist"
        assert lib_git.in_git_repo(missing) is False
