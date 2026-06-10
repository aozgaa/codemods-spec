"""The reconciler engine (EXAMPLE_SPEC.md §5, §7).

All orchestration logic lives here, behind injectable drivers: pass
`review_driver` / `notifier` to use fakes (tests, dry runs); leave them None
to resolve from each codemod's config via the driver registries. The CLI is
a thin wrapper over this class.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import db, state as st, worktree as wt
from .config import CodemodConfig, for_stage, load_config, sample_units
from .decompose import decompose
from .notify.base import Notifier, NotifyError, get_notifier
from .review.base import ReviewDriver, get_review_driver
from .runner import run_script

# State -> notification event it implies (EXAMPLE_SPEC.md §9).
EVENT_OF_STATE = {
    st.FAILED: "failed",
    st.NOOP: "noop",
    st.PR_OPEN: "pr_open",
    st.MERGED: "merged",
    st.ABANDONED: "abandoned",
}


@dataclass
class Outcome:
    codemod: str
    unit: str
    kind: str       # step | notify | doctor | warning
    message: str

    def __str__(self) -> str:
        return f"[{self.codemod}/{self.unit}] {self.message}"


class Engine:
    def __init__(self, conn: psycopg.Connection, *,
                 review_driver: ReviewDriver | None = None,
                 notifier: Notifier | None = None,
                 lease_seconds: int = db.DEFAULT_LEASE_SECONDS):
        self.conn = conn
        self._review_driver = review_driver
        self._notifier = notifier
        self.lease_seconds = lease_seconds

    # -- driver resolution (injection point for tests) --------------------

    def review_driver(self, cfg: CodemodConfig) -> ReviewDriver | None:
        if self._review_driver is not None:
            return self._review_driver
        return get_review_driver(cfg.review.driver) if cfg.review else None

    def notifier(self, cfg: CodemodConfig) -> Notifier | None:
        if self._notifier is not None:
            return self._notifier
        return get_notifier(cfg.notify.driver) if cfg.notify else None

    # -- register (EXAMPLE_SPEC.md §7, §8) -----------------------------------------

    def register(self, config_path: str | Path, test: bool = False) -> dict:
        cfg = load_config(config_path)
        stage = st.STAGE_TEST if test else st.STAGE_PRODUCTION
        units = sample_units(cfg, stage, decompose(cfg))
        return db.register(self.conn, cfg, str(Path(config_path).resolve()),
                           units, stage=stage) | {
            "name": cfg.name, "units": len(units),
        }

    def _cfg(self, cm: dict) -> CodemodConfig:
        """Stage-effective config for a codemod row (EXAMPLE_SPEC.md §3.4)."""
        return for_stage(db.config_of(cm), cm["stage"])

    # -- sync: the reconciler (EXAMPLE_SPEC.md §7) ----------------------------------

    def sync(self, codemod_name: str | None = None, limit: int | None = None) -> list[Outcome]:
        outcomes = self.recover_stale()
        for cm in self._codemods(codemod_name):
            if cm["status"] != st.CM_ACTIVE:
                outcomes.append(Outcome(cm["name"], "*", "info",
                                        f"skipped: codemod is {cm['status']}"
                                        + (f" ({cm['status_reason']})" if cm["status_reason"] else "")))
                continue
            cfg = self._cfg(cm)
            n = 0
            for sub in db.list_subtasks(self.conn, cm["id"],
                                        states=sorted(st.ALL_STATES - st.TERMINAL)):
                if limit is not None and n >= limit:
                    break
                stepped = self._advance(cm, cfg, sub, outcomes)
                n += 1 if stepped else 0
            outcomes += self._notify_pass(cm, cfg)
            outcomes += self._auto_pause(cm, cfg)
        return outcomes

    def _auto_pause(self, cm: dict, cfg: CodemodConfig) -> list[Outcome]:
        """Failure containment (SPEC.md §4.2): pause at limits.max_failures."""
        limit = cfg.limits.max_failures
        if limit <= 0:
            return []
        failures = db.count_subtasks(self.conn, cm["id"], [st.FAILED])
        if failures < limit:
            return []
        reason = f"auto-paused: {failures} failed subtasks (limit {limit})"
        db.set_codemod_status(self.conn, cm["id"], st.CM_PAUSED, reason)
        cm["status"] = st.CM_PAUSED
        outcomes = [Outcome(cfg.name, "*", "doctor", reason)]
        notifier = self.notifier(cfg)
        if notifier is not None and cfg.notify is not None:
            author_notify = dataclasses.replace(cfg.notify, to=[cfg.author])
            try:
                notifier.send(author_notify, "paused", cfg.name, "*",
                              f"[codemods] {cfg.name}: {reason}",
                              f"codemod: {cfg.name}\nauthor: {cfg.author}\n{reason}\n"
                              "Inspect failures with `codemods status`, fix the "
                              "scripts, `codemods retry` the units, then "
                              "`codemods resume`.")
                db.record_notification(self.conn, None, "paused", cfg.notify.driver, "sent")
            except NotifyError as e:
                db.record_notification(self.conn, None, "paused", cfg.notify.driver,
                                       "failed", {"error": str(e)})
                outcomes.append(Outcome(cfg.name, "*", "warning",
                                        f"pause notification failed: {e}"))
        return outcomes

    def _codemods(self, name: str | None) -> list[dict]:
        if name is None:
            return db.list_codemods(self.conn)
        cm = db.get_codemod(self.conn, name)
        if cm is None:
            raise LookupError(f"no codemod named {name!r} is registered")
        return [cm]

    def _advance(self, cm: dict, cfg: CodemodConfig, sub: dict,
                 outcomes: list[Outcome]) -> bool:
        """Step `sub` until it stops moving. Returns whether any step ran."""
        moved = False
        while sub["state"] not in st.TERMINAL:
            before = sub["state"]
            out = self._step(cm, cfg, sub)
            if out:
                outcomes.append(out)
            if sub["state"] == before:
                break
            moved = True
            if before == st.VERIFIED:  # just opened a PR; don't poll it yet
                break
        return moved

    def _step(self, cm: dict, cfg: CodemodConfig, sub: dict) -> Outcome | None:
        unit, slug = sub["unit"], sub["unit_slug"]

        def out(msg: str, kind: str = "step") -> Outcome:
            return Outcome(cfg.name, unit, kind, msg)

        match sub["state"]:
            case st.PENDING:
                branch = cfg.branch_for(slug)
                if not db.transition(self.conn, sub, st.RUNNING, claim=True,
                                     lease_seconds=self.lease_seconds, branch=branch,
                                     attempts=sub["attempts"] + 1,
                                     worktree=str(wt.worktree_path(cfg, slug)),
                                     log_path=str(wt.log_path(cfg, slug))):
                    return None  # another reconciler won the claim
                try:
                    tree = wt.prepare(cfg, slug)
                    res = run_script(cfg, cfg.run, unit, slug, tree, phase="run")
                    if not res.ok:
                        db.transition(self.conn, sub, st.FAILED,
                                      error=f"run script exited {res.returncode}")
                        return out(f"run failed (exit {res.returncode}), see {res.log_path}")
                    if not wt.has_changes(tree):
                        db.transition(self.conn, sub, st.NOOP)
                        wt.discard(tree)
                        return out("run produced no changes -> NOOP")
                    sha = wt.commit_all(tree, self._commit_message(cfg, unit))
                    db.transition(self.conn, sub, st.MODDED, detail={"commit": sha})
                    return out(f"ran and committed {sha[:10]} -> MODDED")
                except Exception as e:
                    db.transition(self.conn, sub, st.FAILED, error=str(e))
                    return out(f"run errored: {e}")

            case st.MODDED:
                if not cfg.postmod:
                    db.transition(self.conn, sub, st.VERIFIED)
                    return out("no postmod -> VERIFIED")
                if not Path(sub["worktree"] or "").exists():
                    return out("worktree missing; run `codemods doctor --fix`", "warning")
                if not db.transition(self.conn, sub, st.VERIFYING, claim=True,
                                     lease_seconds=self.lease_seconds):
                    return None
                try:
                    tree = Path(sub["worktree"])
                    res = run_script(cfg, cfg.postmod, unit, slug, tree, phase="postmod")
                    if not res.ok:
                        db.transition(self.conn, sub, st.FAILED,
                                      error=f"postmod exited {res.returncode}")
                        return out(f"postmod failed (exit {res.returncode}), see {res.log_path}")
                    if wt.has_changes(tree):  # e.g. a formatter pass; fold it in
                        wt.commit_all(tree, self._commit_message(cfg, unit), amend=True)
                    db.transition(self.conn, sub, st.VERIFIED)
                    return out("postmod passed -> VERIFIED")
                except Exception as e:
                    db.transition(self.conn, sub, st.FAILED, error=str(e))
                    return out(f"postmod errored: {e}")

            case st.VERIFIED:
                if cfg.review is None:
                    return None  # rest state: no review configured
                throttle = cfg.limits.max_open_reviews
                if throttle > 0 and db.count_subtasks(
                        self.conn, cm["id"], [st.PR_OPEN]) >= throttle:
                    return None  # review throttle (SPEC.md §4.2); opens as PRs land
                driver = self.review_driver(cfg)
                if not Path(sub["worktree"] or "").exists():
                    return out("worktree missing; run `codemods doctor --fix`", "warning")
                push_url = cfg.review.push_url or cfg.repo
                wt.push(Path(sub["worktree"]), push_url, sub["branch"])
                url = driver.open(
                    cfg.review, sub["branch"], cfg.base_branch,
                    cfg.review.title.format(codemod=cfg.name, unit=unit),
                    cfg.review.body.format(codemod=cfg.name, unit=unit))
                db.transition(self.conn, sub, st.PR_OPEN, pr_url=url)
                return out(f"opened review {url}")

            case st.PR_OPEN:
                driver = self.review_driver(cfg)
                match driver.state(cfg.review, sub["pr_url"]):
                    case "merged":
                        db.transition(self.conn, sub, st.MERGED)
                        wt.discard(sub["worktree"] or "")
                        return out(f"review merged: {sub['pr_url']}")
                    case "closed":
                        db.transition(self.conn, sub, st.ABANDONED)
                        wt.discard(sub["worktree"] or "")
                        return out(f"review closed unmerged -> ABANDONED: {sub['pr_url']}")
                return None

            case st.RUNNING | st.VERIFYING:
                return None  # claimed in-flight (live or stale; recover_stale handles)
        return None

    @staticmethod
    def _commit_message(cfg: CodemodConfig, unit: str) -> str:
        msg = f"[codemods] {cfg.name}: {unit}"
        if cfg.description:
            msg += f"\n\n{cfg.description}"
        return msg

    # -- notifications (EXAMPLE_SPEC.md §9): derived from state, deduped ------------

    def _notify_pass(self, cm: dict, cfg: CodemodConfig) -> list[Outcome]:
        notifier = self.notifier(cfg)
        if notifier is None or cfg.notify is None:
            return []
        outcomes = []
        for sub in db.list_subtasks(self.conn, cm["id"],
                                    states=sorted(EVENT_OF_STATE)):
            event = EVENT_OF_STATE[sub["state"]]
            if event not in cfg.notify.on or db.notified(self.conn, sub["id"], event):
                continue
            subject = f"[codemods] {cfg.name}/{sub['unit']}: {event}"
            body = "\n".join(filter(None, [
                f"codemod: {cfg.name}",
                f"unit:    {sub['unit']}",
                f"state:   {sub['state']}",
                f"review:  {sub['pr_url']}" if sub["pr_url"] else "",
                f"error:   {sub['last_error']}" if sub["last_error"] else "",
                f"log:     {sub['log_path']}" if sub["log_path"] else "",
            ]))
            try:
                notifier.send(cfg.notify, event, cfg.name, sub["unit"], subject, body)
                db.record_notification(self.conn, sub["id"], event,
                                       cfg.notify.driver, "sent")
                outcomes.append(Outcome(cfg.name, sub["unit"], "notify", f"notified: {event}"))
            except NotifyError as e:
                db.record_notification(self.conn, sub["id"], event,
                                       cfg.notify.driver, "failed", {"error": str(e)})
                outcomes.append(Outcome(cfg.name, sub["unit"], "warning",
                                        f"notification {event} failed: {e}"))
        return outcomes

    # -- crash recovery (EXAMPLE_SPEC.md §5.2) ---------------------------------------

    def recover_stale(self) -> list[Outcome]:
        outcomes = []
        for sub in db.stale_claims(self.conn, self.lease_seconds):
            target = st.CLAIMED_RECOVERY[sub["state"]]
            if target == st.PENDING:
                wt.discard(sub["worktree"] or "")
            if db.transition(self.conn, sub, target,
                             detail={"reason": "stale claim recovered"}):
                outcomes.append(Outcome(sub["codemod_name"], sub["unit"], "doctor",
                                        f"stale claim recovered -> {target}"))
        return outcomes

    # -- operator commands (EXAMPLE_SPEC.md §7) --------------------------------------

    def retry(self, codemod_name: str, unit: str) -> Outcome:
        cm, sub = self._find(codemod_name, unit)
        if sub["state"] != st.FAILED:
            raise ValueError(f"can only retry FAILED subtasks, {unit!r} is {sub['state']}")
        db.transition(self.conn, sub, st.PENDING, detail={"reason": "operator retry"})
        return Outcome(codemod_name, unit, "step", "FAILED -> PENDING (will re-run)")

    def abandon(self, codemod_name: str, unit: str) -> Outcome:
        cm, sub = self._find(codemod_name, unit)
        if sub["state"] in st.TERMINAL:
            raise ValueError(f"{unit!r} is already terminal ({sub['state']})")
        self._abandon_subtask(self._cfg(cm), sub, "operator abandon")
        return Outcome(codemod_name, unit, "step", "ABANDONED")

    def _abandon_subtask(self, cfg: CodemodConfig, sub: dict, reason: str) -> None:
        if sub["pr_url"] and sub["state"] == st.PR_OPEN and cfg.review is not None:
            self.review_driver(cfg).close(
                cfg.review, sub["pr_url"],
                f"Abandoned by codemods ({cfg.name}/{sub['unit']}): {reason}.")
        wt.discard(sub["worktree"] or "")
        db.transition(self.conn, sub, st.ABANDONED, detail={"reason": reason})

    def _find(self, codemod_name: str, unit: str) -> tuple[dict, dict]:
        cm = db.get_codemod(self.conn, codemod_name)
        if cm is None:
            raise LookupError(f"no codemod named {codemod_name!r} is registered")
        sub = db.get_subtask(self.conn, cm["id"], unit)
        if sub is None:
            raise LookupError(f"codemod {codemod_name!r} has no unit {unit!r}")
        return cm, sub

    # -- campaign management (SPEC.md §4.2, EXAMPLE_SPEC.md §5.3) -------------

    def pause(self, codemod_name: str, reason: str | None = None) -> Outcome:
        cm = self._codemods(codemod_name)[0]
        if cm["status"] != st.CM_ACTIVE:
            raise ValueError(f"{codemod_name!r} is {cm['status']}, not active")
        db.set_codemod_status(self.conn, cm["id"], st.CM_PAUSED,
                              reason or "operator pause")
        return Outcome(codemod_name, "*", "step", "paused")

    def resume(self, codemod_name: str) -> Outcome:
        cm = self._codemods(codemod_name)[0]
        if cm["status"] != st.CM_PAUSED:
            raise ValueError(f"{codemod_name!r} is {cm['status']}, not paused")
        db.set_codemod_status(self.conn, cm["id"], st.CM_ACTIVE)
        return Outcome(codemod_name, "*", "step", "resumed")

    def cancel(self, codemod_name: str) -> list[Outcome]:
        cm = self._codemods(codemod_name)[0]
        if cm["status"] == st.CM_CANCELLED:
            raise ValueError(f"{codemod_name!r} is already cancelled")
        cfg = self._cfg(cm)
        outcomes = []
        for sub in db.list_subtasks(self.conn, cm["id"],
                                    states=sorted(st.ALL_STATES - st.TERMINAL)):
            self._abandon_subtask(cfg, sub, "codemod cancelled")
            outcomes.append(Outcome(cfg.name, sub["unit"], "step", "ABANDONED (cancel)"))
        db.set_codemod_status(self.conn, cm["id"], st.CM_CANCELLED, "operator cancel")
        outcomes.append(Outcome(codemod_name, "*", "step", "cancelled"))
        return outcomes

    def promote(self, codemod_name: str) -> dict:
        """Test stage -> production (EXAMPLE_SPEC.md §5.3): abandon the test
        generation, bump the generation, re-decompose with production config."""
        cm = self._codemods(codemod_name)[0]
        if cm["stage"] != st.STAGE_TEST:
            raise ValueError(f"{codemod_name!r} is in stage {cm['stage']!r}; "
                             "only test-stage codemods can be promoted")
        test_cfg = self._cfg(cm)
        abandoned = 0
        for sub in db.list_subtasks(self.conn, cm["id"],
                                    states=sorted(st.ALL_STATES - st.TERMINAL)):
            self._abandon_subtask(test_cfg, sub, "promoted to production")
            abandoned += 1
        prod_cfg = db.config_of(cm)
        units = decompose(prod_cfg)
        with self.conn.transaction():
            generation = db.bump_generation(self.conn, cm["id"], st.STAGE_PRODUCTION)
            new_units, _, _ = db.insert_units(self.conn, cm["id"], generation, units)
        return {"name": codemod_name, "abandoned": abandoned,
                "generation": generation, "units": len(new_units)}

    # -- doctor (EXAMPLE_SPEC.md §7) --------------------------------------------------

    def doctor(self, fix: bool = False, codemod_name: str | None = None) -> list[Outcome]:
        findings: list[Outcome] = []
        # Check 1: stale claims.
        for sub in db.stale_claims(self.conn, self.lease_seconds):
            target = st.CLAIMED_RECOVERY[sub["state"]]
            msg = f"stale claim ({sub['claimed_by']}) in {sub['state']}"
            if fix:
                if target == st.PENDING:
                    wt.discard(sub["worktree"] or "")
                db.transition(self.conn, sub, target, detail={"reason": "doctor"})
                msg += f" -> reset to {target}"
            findings.append(Outcome(sub["codemod_name"], sub["unit"], "doctor", msg))

        for cm in self._codemods(codemod_name):
            findings += self._doctor_codemod(cm, self._cfg(cm), fix)
        return findings

    def _doctor_codemod(self, cm: dict, cfg: CodemodConfig, fix: bool) -> list[Outcome]:
        findings: list[Outcome] = []
        subtasks = db.list_subtasks(self.conn, cm["id"])
        active = [s for s in subtasks if s["state"] not in st.TERMINAL]

        # Check 2: states that require a worktree, but it is gone.
        for sub in active:
            if sub["state"] in (st.MODDED, st.VERIFIED) and not Path(sub["worktree"] or "").exists():
                msg = f"worktree missing in {sub['state']}"
                if fix:
                    db.transition(self.conn, sub, st.PENDING,
                                  detail={"reason": "doctor: worktree missing"})
                    msg += " -> reset to PENDING"
                findings.append(Outcome(cfg.name, sub["unit"], "doctor", msg))

        # Check 3: vanished units (decomposition no longer yields them).
        try:
            units = set(sample_units(cfg, cm["stage"], decompose(cfg)))
        except Exception as e:
            findings.append(Outcome(cfg.name, "*", "warning",
                                    f"cannot re-evaluate decomposition: {e}"))
            units = None
        if units is not None:
            for sub in active:
                if sub["unit"] not in units:
                    msg = "unit vanished from decomposition"
                    if fix:
                        self.abandon(cfg.name, sub["unit"])
                        msg += " -> ABANDONED"
                    findings.append(Outcome(cfg.name, sub["unit"], "doctor", msg))

        # Check 4: orphaned reviews under our branch prefix.
        driver = self.review_driver(cfg)
        if driver is not None and cfg.review is not None:
            tracked = {s["branch"] for s in subtasks if s["state"] == st.PR_OPEN}
            adoptable = {s["branch"]: s for s in subtasks if s["state"] == st.VERIFIED}
            prefix = f"{cfg.branch_prefix}/{cfg.name}"
            for branch, url in driver.find_orphans(cfg.review, cfg.branch_prefix):
                if not branch.startswith(prefix + "/") or branch in tracked:
                    continue
                if (sub := adoptable.get(branch)) is not None:
                    msg = f"untracked review {url} matches VERIFIED subtask"
                    if fix:
                        db.transition(self.conn, sub, st.PR_OPEN, pr_url=url,
                                      detail={"reason": "doctor: adopted orphan review"})
                        msg += " -> adopted"
                else:
                    msg = f"orphaned review {url} on {branch}"
                    if fix:
                        driver.close(cfg.review, url,
                                     "Closed by codemods doctor: no subtask tracks this review.")
                        msg += " -> closed"
                findings.append(Outcome(cfg.name, branch, "doctor", msg))

        # Check 5: worktree directories no active subtask references.
        wt_root = Path(cfg.workdir) / "worktrees"
        referenced = {s["worktree"] for s in active if s["worktree"]}
        if wt_root.is_dir():
            for d in sorted(wt_root.iterdir()):
                if d.name.startswith(f"{cfg.name}-") and str(d) not in referenced:
                    msg = f"orphaned worktree {d}"
                    if fix:
                        wt.discard(d)
                        msg += " -> deleted"
                    findings.append(Outcome(cfg.name, d.name, "doctor", msg))
        return findings
