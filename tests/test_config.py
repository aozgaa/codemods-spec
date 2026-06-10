import pytest

from codemods.config import CodemodConfig, ConfigError, load_config, slugify

MINIMAL = """
codemod "demo" {{
  author      = "dev@example.com"
  repo        = "{repo}"
  base_branch = "main"
  run         = "./run.sh"
  decomposition {{
    type  = "literal"
    items = ["a", "b"]
  }}
{extra}
}}
"""


def write_config(tmp_path, extra="", repo="/tmp/repo"):
    p = tmp_path / "demo.hcl"
    p.write_text(MINIMAL.format(repo=repo, extra=extra))
    return p


def test_minimal_config(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.name == "demo"
    assert cfg.repo == "/tmp/repo"
    assert cfg.base_branch == "main"
    assert cfg.decomposition.type == "literal"
    assert cfg.decomposition.items == ["a", "b"]
    assert cfg.review is None and cfg.notify is None
    assert cfg.branch_prefix == "codemods"
    assert cfg.branch_for("a") == "codemods/demo/a"


def test_relative_paths_resolve_against_config_dir(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.run == str(tmp_path / "run.sh")
    assert cfg.workdir == str(tmp_path / "work")


def test_review_and_notify_blocks(tmp_path):
    extra = """
  review {
    driver   = "github"
    repo     = "me/proj"
    push_url = "git@github.com:me/proj.git"
  }
  notify {
    driver = "email"
    to     = ["a@b.c"]
    from   = "mods@b.c"
    on     = ["failed", "merged"]
  }
"""
    cfg = load_config(write_config(tmp_path, extra))
    assert cfg.review.driver == "github"
    assert cfg.review.repo == "me/proj"
    assert "{unit}" in cfg.review.title
    assert cfg.notify.sender == "mods@b.c"
    assert cfg.notify.on == ["failed", "merged"]


def test_round_trips_through_dict(tmp_path):
    extra = '  review {\n    driver = "github"\n  }\n'
    cfg = load_config(write_config(tmp_path, extra))
    assert CodemodConfig.from_dict(cfg.to_dict()) == cfg


@pytest.mark.parametrize(
    "mutation,match",
    [
        ('decomposition {\n type = "nope"\n}', "exactly one 'decomposition'"),
        ('notify {\n driver = "email"\n on = ["bogus"]\n}', "unknown notify events"),
        ('review {\n repo = "x/y"\n}', "requires 'driver'"),
    ],
)
def test_invalid_configs_rejected(tmp_path, mutation, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write_config(tmp_path, mutation))


def test_missing_required_key(tmp_path):
    p = tmp_path / "bad.hcl"
    p.write_text('codemod "x" {\n  author = "a@b.c"\n  repo = "/tmp/r"\n  base_branch = "main"\n}\n')
    with pytest.raises(ConfigError, match="missing required key 'run'"):
        load_config(p)
    p.write_text('codemod "x" {\n  repo = "/tmp/r"\n  base_branch = "main"\n  run = "./r.sh"\n}\n')
    with pytest.raises(ConfigError, match="missing required key 'author'"):
        load_config(p)


def test_slugify():
    assert slugify("src/data_structures") == "src-data_structures"
    assert slugify("@org/Payments Team") == "org-payments-team"
    assert slugify("///") == "unit"
    taken = set()
    assert slugify("a/b", taken) == "a-b"
    assert slugify("a.b", taken) == "a.b"
    assert slugify("a:b", taken) == "a-b-2"
    assert slugify("a;b", taken) == "a-b-3"
