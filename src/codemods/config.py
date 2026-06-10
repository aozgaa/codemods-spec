"""Parse and validate codemod HCL configuration (EXAMPLE_SPEC.md §3)."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path

import hcl2


class ConfigError(Exception):
    pass


DECOMPOSITION_TYPES = {"literal", "glob", "command", "codeowners"}
NOTIFY_EVENTS = {"failed", "noop", "pr_open", "merged", "abandoned"}


@dataclass(frozen=True)
class Decomposition:
    type: str
    items: list[str] = field(default_factory=list)  # literal
    include: list[str] = field(default_factory=list)  # glob
    exclude: list[str] = field(default_factory=list)  # glob
    kind: str = "any"  # glob: directory | file | any
    command: str = ""  # command
    format: str = "lines"  # command: lines | nul
    path: str = ""  # codeowners


@dataclass(frozen=True)
class ReviewConfig:
    driver: str
    repo: str = ""
    push_url: str = ""
    title: str = "[codemods] {codemod}: {unit}"
    body: str = "Automated change `{codemod}` applied to `{unit}`."
    reviewers: list[str] = field(default_factory=list)
    draft: bool = False


@dataclass(frozen=True)
class NotifyConfig:
    driver: str
    to: list[str] = field(default_factory=list)
    sender: str = "codemods@localhost"
    smtp: str = "localhost:25"
    on: list[str] = field(default_factory=lambda: ["failed", "pr_open", "merged", "abandoned"])


@dataclass(frozen=True)
class Limits:
    max_open_reviews: int = 0  # 0 = unlimited (SPEC.md §4.2)
    max_failures: int = 0      # 0 = never auto-pause


@dataclass(frozen=True)
class TestOverrides:
    """Testing-stage overrides (EXAMPLE_SPEC.md §3.4)."""

    review: ReviewConfig | None = None
    notify: NotifyConfig | None = None
    sample: int = 0  # 0 = all units


@dataclass(frozen=True)
class CodemodConfig:
    name: str
    author: str
    repo: str
    base_branch: str
    run: str
    decomposition: Decomposition
    description: str = ""
    branch_prefix: str = "codemods"
    workdir: str = ""
    postmod: str = ""
    review: ReviewConfig | None = None
    notify: NotifyConfig | None = None
    limits: Limits = field(default_factory=Limits)
    test: TestOverrides | None = None

    def branch_for(self, slug: str) -> str:
        return f"{self.branch_prefix}/{self.name}/{slug}"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CodemodConfig":
        d = dict(d)
        d.setdefault("author", "")  # config snapshots predating the author field
        d["decomposition"] = Decomposition(**d["decomposition"])
        if d.get("review"):
            d["review"] = ReviewConfig(**d["review"])
        if d.get("notify"):
            d["notify"] = NotifyConfig(**d["notify"])
        d["limits"] = Limits(**d.get("limits") or {})
        if d.get("test"):
            t = dict(d["test"])
            if t.get("review"):
                t["review"] = ReviewConfig(**t["review"])
            if t.get("notify"):
                t["notify"] = NotifyConfig(**t["notify"])
            d["test"] = TestOverrides(**t)
        return CodemodConfig(**d)


def for_stage(cfg: CodemodConfig, stage: str) -> CodemodConfig:
    """Effective config for a codemod's stage (EXAMPLE_SPEC.md §3.4).

    In the test stage, reviews never request owner attention and
    notifications reach only the author; the stored config is untouched.
    """
    if stage != "test":
        return cfg
    t = cfg.test or TestOverrides()
    review = t.review
    if review is None and cfg.review is not None:
        review = dataclasses.replace(cfg.review, draft=True,
                                     title=f"[test] {cfg.review.title}")
    notify = t.notify
    if notify is None and cfg.notify is not None:
        notify = dataclasses.replace(cfg.notify, to=[cfg.author])
    return dataclasses.replace(cfg, review=review, notify=notify)


def sample_units(cfg: CodemodConfig, stage: str, units: list[str]) -> list[str]:
    """Apply the test-stage decomposition sample (EXAMPLE_SPEC.md §3.4)."""
    if stage == "test" and cfg.test and cfg.test.sample > 0:
        return units[: cfg.test.sample]
    return units


def _single_block(raw: dict, key: str, where: str) -> dict | None:
    blocks = raw.get(key)
    if blocks is None:
        return None
    if not isinstance(blocks, list) or len(blocks) != 1:
        raise ConfigError(f"{where}: expected exactly one '{key}' block")
    return blocks[0]


def _resolve(base: Path, p: str) -> str:
    return str((base / p).resolve()) if p and not Path(p).is_absolute() else p


def _parse_decomposition(raw: dict, where: str) -> Decomposition:
    dtype = raw.get("type")
    if dtype not in DECOMPOSITION_TYPES:
        raise ConfigError(f"{where}: decomposition type must be one of {sorted(DECOMPOSITION_TYPES)}, got {dtype!r}")
    d = Decomposition(
        type=dtype,
        items=list(raw.get("items", [])),
        include=list(raw.get("include", [])),
        exclude=list(raw.get("exclude", [])),
        kind=raw.get("kind", "any"),
        command=raw.get("command", "").strip(),
        format=raw.get("format", "lines"),
        path=raw.get("path", ""),
    )
    if d.type == "literal" and not d.items:
        raise ConfigError(f"{where}: literal decomposition requires non-empty 'items'")
    if d.type == "glob" and not d.include:
        raise ConfigError(f"{where}: glob decomposition requires 'include'")
    if d.kind not in ("directory", "file", "any"):
        raise ConfigError(f"{where}: glob 'kind' must be directory|file|any, got {d.kind!r}")
    if d.type == "command" and not d.command:
        raise ConfigError(f"{where}: command decomposition requires 'command'")
    if d.format not in ("lines", "nul"):
        raise ConfigError(f"{where}: command 'format' must be lines|nul, got {d.format!r}")
    if d.type == "codeowners" and not d.path:
        raise ConfigError(f"{where}: codeowners decomposition requires 'path'")
    return d


def _parse_review(rraw: dict, where: str) -> ReviewConfig:
    if not rraw.get("driver"):
        raise ConfigError(f"{where}: review block requires 'driver'")
    return ReviewConfig(
        driver=rraw["driver"],
        repo=rraw.get("repo", ""),
        push_url=rraw.get("push_url", ""),
        title=rraw.get("title", ReviewConfig.title),
        body=rraw.get("body", ReviewConfig.body),
        reviewers=list(rraw.get("reviewers", [])),
        draft=bool(rraw.get("draft", False)),
    )


def _parse_notify(nraw: dict, where: str) -> NotifyConfig:
    if not nraw.get("driver"):
        raise ConfigError(f"{where}: notify block requires 'driver'")
    on = list(nraw.get("on", ["failed", "pr_open", "merged", "abandoned"]))
    bad = set(on) - NOTIFY_EVENTS
    if bad:
        raise ConfigError(f"{where}: unknown notify events {sorted(bad)}")
    return NotifyConfig(
        driver=nraw["driver"],
        to=list(nraw.get("to", [])),
        sender=nraw.get("from", NotifyConfig.sender),
        smtp=nraw.get("smtp", NotifyConfig.smtp),
        on=on,
    )


def load_config(path: str | Path) -> CodemodConfig:
    """Load a single-codemod HCL file into a validated CodemodConfig."""
    path = Path(path).resolve()
    base = path.parent
    try:
        with open(path) as f:
            raw = hcl2.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except Exception as e:
        raise ConfigError(f"cannot parse {path}: {e}") from e

    codemods = raw.get("codemod", [])
    if len(codemods) != 1:
        raise ConfigError(f"{path}: expected exactly one codemod block, found {len(codemods)}")
    (name, body), = codemods[0].items()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError(f"{path}: invalid codemod name {name!r}")

    for key in ("author", "repo", "base_branch", "run"):
        if not body.get(key):
            raise ConfigError(f"{path}: codemod {name!r} missing required key '{key}'")

    draw = _single_block(body, "decomposition", f"{path}")
    if draw is None:
        raise ConfigError(f"{path}: codemod {name!r} missing decomposition block")
    decomposition = _parse_decomposition(draw, str(path))
    if decomposition.path:
        decomposition = dataclasses.replace(decomposition, path=_resolve(base, decomposition.path))

    review = None
    if (rraw := _single_block(body, "review", str(path))) is not None:
        review = _parse_review(rraw, str(path))
        if review.driver == "fake" and review.repo:
            # The fake driver's `repo` is a state-file path, not an SCM repo.
            review = dataclasses.replace(review, repo=_resolve(base, review.repo))

    notify = None
    if (nraw := _single_block(body, "notify", str(path))) is not None:
        notify = _parse_notify(nraw, str(path))

    limits = Limits()
    if (lraw := _single_block(body, "limits", str(path))) is not None:
        limits = Limits(
            max_open_reviews=int(lraw.get("max_open_reviews", 0)),
            max_failures=int(lraw.get("max_failures", 0)),
        )

    test = None
    if (traw := _single_block(body, "test", str(path))) is not None:
        t_review = _single_block(traw, "review", f"{path} (test block)")
        t_notify = _single_block(traw, "notify", f"{path} (test block)")
        test = TestOverrides(
            review=_parse_review(t_review, f"{path} (test block)") if t_review else None,
            notify=_parse_notify(t_notify, f"{path} (test block)") if t_notify else None,
            sample=int(traw.get("sample", 0)),
        )
        if test.review and test.review.driver == "fake" and test.review.repo:
            # The fake driver's `repo` is a state-file path, not an SCM repo.
            test = dataclasses.replace(
                test, review=dataclasses.replace(
                    test.review, repo=_resolve(base, test.review.repo)))

    return CodemodConfig(
        name=name,
        author=body["author"],
        description=body.get("description", ""),
        repo=_resolve(base, body["repo"]),
        base_branch=body["base_branch"],
        branch_prefix=body.get("branch_prefix", "codemods"),
        workdir=_resolve(base, body.get("workdir", "./work")),
        run=_resolve(base, body["run"]),
        postmod=_resolve(base, body.get("postmod", "")),
        decomposition=decomposition,
        review=review,
        notify=notify,
        limits=limits,
        test=test,
    )


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(unit: str, taken: set[str] | None = None, maxlen: int = 60) -> str:
    """Unit slug per EXAMPLE_SPEC.md §3.3."""
    slug = _SLUG_RE.sub("-", unit.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:maxlen] or "unit"
    if taken is not None:
        candidate, n = slug, 1
        while candidate in taken:
            n += 1
            candidate = f"{slug}-{n}"
        taken.add(candidate)
        return candidate
    return slug
