import subprocess

import pytest

from codemods.config import CodemodConfig, Decomposition
from codemods.decompose import DecompositionError, decompose, unit_files


def make_config(repo, **dkwargs):
    return CodemodConfig(
        name="t",
        author="dev@example.com",
        repo=str(repo),
        base_branch="main",
        run="/bin/true",
        decomposition=Decomposition(**dkwargs),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for d in ("src/alpha", "src/beta", "src/gamma"):
        (root / d).mkdir(parents=True)
        (root / d / "x.cpp").write_text("// x\n")
    (root / "src/main.cpp").write_text("int main() {}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_literal():
    cfg = make_config("/nonexistent", type="literal", items=["a", "b"])
    assert decompose(cfg) == ["a", "b"]


def test_literal_duplicates_rejected():
    cfg = make_config("/nonexistent", type="literal", items=["a", "a"])
    with pytest.raises(DecompositionError, match="duplicate"):
        decompose(cfg)


def test_glob_directories(repo):
    cfg = make_config(repo, type="glob", include=["src/*"], kind="directory")
    assert decompose(cfg) == ["src/alpha", "src/beta", "src/gamma"]


def test_glob_exclude_and_files(repo):
    cfg = make_config(repo, type="glob", include=["src/*"], exclude=["src/beta"], kind="any")
    assert decompose(cfg) == ["src/alpha", "src/gamma", "src/main.cpp"]
    cfg = make_config(repo, type="glob", include=["src/*"], kind="file")
    assert decompose(cfg) == ["src/main.cpp"]


def test_glob_skips_hidden_unless_named(repo):
    (repo / ".github").mkdir()
    cfg = make_config(repo, type="glob", include=["*"], kind="directory")
    assert decompose(cfg) == ["src"]  # not .git, not .github
    cfg = make_config(repo, type="glob", include=[".github"], kind="directory")
    assert decompose(cfg) == [".github"]


def test_command_lines(repo):
    cfg = make_config(repo, type="command",
                      command="find src -mindepth 1 -maxdepth 1 -type d | sort")
    assert decompose(cfg) == ["src/alpha", "src/beta", "src/gamma"]


def test_command_nul(repo):
    cfg = make_config(repo, type="command", format="nul",
                      command="find src -mindepth 1 -maxdepth 1 -type d -print0")
    assert sorted(decompose(cfg)) == ["src/alpha", "src/beta", "src/gamma"]


def test_command_failure(repo):
    cfg = make_config(repo, type="command", command="false")
    with pytest.raises(DecompositionError, match="exited 1"):
        decompose(cfg)


def test_empty_decomposition_rejected(repo):
    cfg = make_config(repo, type="glob", include=["nothing/*"])
    with pytest.raises(DecompositionError, match="no units"):
        decompose(cfg)


def test_codeowners(repo):
    co = repo / "CODEOWNERS"
    co.write_text(
        "# comment\n"
        "*        @org/default\n"
        "/src/alpha/  @org/alpha-team\n"
        "src/beta  @org/beta-team @bob\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    cfg = make_config(repo, type="codeowners", path=str(co))
    units = decompose(cfg)
    assert units == ["@bob", "@org/alpha-team", "@org/beta-team", "@org/default"]
    assert unit_files(cfg, "@org/alpha-team") == ["src/alpha/x.cpp"]
    assert unit_files(cfg, "@org/beta-team") == ["src/beta/x.cpp"]
    assert set(unit_files(cfg, "@org/default")) == {"CODEOWNERS", "src/gamma/x.cpp", "src/main.cpp"}


def test_unit_files_none_for_non_codeowners():
    cfg = make_config("/nonexistent", type="literal", items=["a"])
    assert unit_files(cfg, "a") is None
