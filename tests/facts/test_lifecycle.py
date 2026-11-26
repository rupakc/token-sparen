"""Fact lifecycle: a Beta bandit per fact over relevance-conditioned waste (spec section 6.8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_miners import Log

from token_sparen.facts import lifecycle, miners
from token_sparen.facts.lifecycle import Obs, Observer, Settings
from token_sparen.facts.sanitize import Fact, fact_id
from token_sparen.learn import overlay
from token_sparen.learn.beta import Beta
from token_sparen.store import session
from token_sparen.transcript import iter_tool_results

PROJECT = "/work/proj"
KEY = "0123456789abcdef"
S = Settings()


def _fact(kind: str, subject: str, value: str, n: int = 2) -> Fact:
    sessions = frozenset(f"{i:016x}" for i in range(n))
    return Fact(kind, subject, value, sessions)  # type: ignore[arg-type]


def _entry(fact: Fact, **extra: Any) -> dict[str, Any]:
    return {
        "kind": fact.kind,
        "subject": fact.subject,
        "value": fact.value,
        "sessions": sorted(fact.evidence_sessions),
        "first_seen": 1.0,
        "last_seen": 2.0,
        "contradictions": 0,
        **extra,
    }


def _table(*facts: Fact, **extra: Any) -> dict[str, Any]:
    return {fact_id(f): _entry(f, **extra) for f in facts}


def _add_session(table: dict[str, Any], fid: str, sid: str) -> None:
    table[fid]["sessions"].append(sid)


def _observe(log: Log, tmp_path: Path, *facts: Fact) -> dict[str, Obs]:
    path = log.write(tmp_path / "obs.jsonl")
    obs = Observer({fact_id(f): f for f in facts}, PROJECT)
    for call in iter_tool_results(path):
        obs.feed(call)
    return obs.results()


ALIAS = _fact("command_alias", "pytest", "python -m pytest")
TESTS = _fact("test_command", "tests", "python -m pytest -q")
ENV = _fact("requires_env", "make run", "DATABASE_URL")
BUILD = _fact("build_first", "npm test", "npm run build")
GEN = _fact("generated_dir", "generated", "dist")


# --------------------------------------------------------------------------- activation
def test_activation_after_confirmations() -> None:
    """Each distinct confirming session (the miner re-derived the waste -> fix pattern while
    the fact was not injected) is a success; Beta(1, 1) + 3 successes clears lcb(0.1) > 0.55."""
    f = _fact("command_alias", "pytest", "python -m pytest", n=1)
    fid = fact_id(f)
    table = _table(f)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined={fid}, settings=S)
    assert table[fid]["status"] == "candidate"  # one session: never eligible
    _add_session(table, fid, "b" * 16)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined={fid}, settings=S)
    assert table[fid]["status"] == "candidate"  # Beta(3, 1): lcb(0.1) = 0.46
    _add_session(table, fid, "c" * 16)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined={fid}, settings=S)
    assert table[fid]["status"] == "active"  # Beta(4, 1): lcb(0.1) = 0.56
    assert lifecycle.effect(table[fid]).mean == pytest.approx(0.8)


def test_frequency_kinds_need_five_sessions_and_an_observed_waste_event() -> None:
    """test_command / generated_dir are frequencies, not waste -> fix patterns: they activate
    only with >= 5 sessions of evidence and >= 1 observed waste event (and no confirmation is
    ever counted for them)."""
    t4 = _fact("test_command", "tests", "pytest", n=4)
    t5 = _fact("test_command", "tests", "pytest -q", n=5)
    g5 = _fact("generated_dir", "generated", "dist", n=5)
    g5w = _fact("generated_dir", "generated", "out", n=5)
    table = _table(t4, t5, g5, g5w, waste_events=1)
    table[fact_id(g5)]["waste_events"] = 0
    mined = set(table)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=mined, settings=S)
    assert table[fact_id(t4)]["status"] == "candidate"  # 4 sessions
    assert table[fact_id(g5)]["status"] == "candidate"  # no waste event seen
    assert table[fact_id(t5)]["status"] == "active"
    assert table[fact_id(g5w)]["status"] == "active"
    assert lifecycle.effect(table[fact_id(t5)]) == Beta(1.0, 1.0)  # no confirmation counted


def test_waste_events_are_counted_for_candidates() -> None:
    t = _fact("test_command", "tests", "pytest -q", n=5)
    fid = fact_id(t)
    table = _table(t)
    obs = {fid: Obs(relevant=True, waste=True, waste_event=True)}
    lifecycle.apply_session(table, shown=(), withheld={}, obs=obs, mined=set(), settings=S)
    assert table[fid]["waste_events"] == 1 and table[fid]["status"] == "active"


def test_activate_lcb_is_configurable() -> None:
    f = _fact("command_alias", "pytest", "python -m pytest", n=2)
    table = _table(f, effect=[3.0, 1.0, 2.0])
    strict = Settings(activate_lcb=0.9)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=set(), settings=strict)
    assert table[fact_id(f)]["status"] == "candidate"
    loose = Settings(activate_lcb=0.4)
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=set(), settings=loose)
    assert table[fact_id(f)]["status"] == "active"


def test_rewards_for_injected_relevant_sessions() -> None:
    a, b, c = (_fact("command_alias", p, f"python -m {p}") for p in ("pytest", "black", "ruff"))
    table = _table(a, b, c, status="active", effect=[4.0, 1.0, 3.0])
    obs = {
        fact_id(a): Obs(relevant=True, waste=False),
        fact_id(b): Obs(relevant=True, waste=True),
        fact_id(c): Obs(relevant=False, waste=False),
    }
    shown = {fact_id(a), fact_id(b), fact_id(c)}
    lifecycle.apply_session(table, shown=shown, withheld={}, obs=obs, mined=set(), settings=S)
    assert lifecycle.effect(table[fact_id(a)]).to_list() == [5.0, 1.0, 4.0]
    assert lifecycle.effect(table[fact_id(b)]).to_list() == [4.0, 2.0, 4.0]
    assert lifecycle.effect(table[fact_id(c)]).to_list() == [4.0, 1.0, 3.0]  # irrelevant


def test_shown_fact_mined_again_is_not_a_confirmation() -> None:
    fid = fact_id(ALIAS)
    table = _table(ALIAS, status="active", effect=[4.0, 1.0, 3.0])
    obs = {fid: Obs(relevant=True, waste=True)}
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs=obs, mined={fid}, settings=S)
    assert lifecycle.effect(table[fid]).to_list() == [4.0, 2.0, 4.0]


def test_generated_dir_relevant_session_rewards_half() -> None:
    fid = fact_id(GEN)
    table = _table(GEN, status="active")
    obs = {fid: Obs(relevant=True, waste=None)}
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs=obs, mined=set(), settings=S)
    assert lifecycle.effect(table[fid]).to_list() == [1.5, 1.5, 1.0]


# --------------------------------------------------------------------------- withholding
def test_withhold_floor_observes_counterfactual() -> None:
    """A withheld relevant session updates the baseline Beta (waste presence), never the
    injection posterior; a withheld irrelevant session records nothing."""
    a, b = ALIAS, ENV
    table = _table(a, b, status="active", effect=[4.0, 1.0, 3.0])
    obs = {fact_id(a): Obs(relevant=True, waste=True), fact_id(b): Obs(relevant=False)}
    withheld = {fact_id(a): 0.1, fact_id(b): 0.1}
    lifecycle.apply_session(table, shown=(), withheld=withheld, obs=obs, mined=set(), settings=S)
    assert lifecycle.baseline(table[fact_id(a)]).to_list() == [1.0, 2.0, 1.0]
    assert lifecycle.effect(table[fact_id(a)]).to_list() == [4.0, 1.0, 3.0]
    assert lifecycle.baseline(table[fact_id(b)]).n == 0
    assert lifecycle.estimated_effect(table[fact_id(a)]) == pytest.approx(0.8 - 1 / 3)
    assert lifecycle.estimated_effect(table[fact_id(b)]) is None


def test_withhold_decision_is_seeded_and_near_the_floor() -> None:
    fids = [f"{i:012x}" for i in range(4000)]
    picks = [lifecycle.withhold("sess", fid, 0.1) for fid in fids]
    assert picks == [lifecycle.withhold("sess", fid, 0.1) for fid in fids]  # same session: same
    assert 0.08 < sum(picks) / len(picks) < 0.12
    assert not any(lifecycle.withhold("sess", fid, 0.0) for fid in fids)


def test_select_logs_propensities(tmp_path: Path) -> None:
    facts = [_fact("command_alias", f"tool{c}", f"python -m tool{c}") for c in "abcdefghij"]
    doc = {"version": 1, "facts": _table(*facts, status="active")}
    shown, withheld = lifecycle.select(doc, "sess-1", Settings(withhold_floor=0.5, max_facts=20))
    assert len(shown) + len(withheld) == len(facts)
    assert withheld and all(p == 0.5 for p in withheld.values())
    assert not set(withheld) & {fid for fid, _ in shown}


def test_select_ranks_active_by_recent_evidence() -> None:
    old, new = ALIAS, ENV
    cand, gone = TESTS, BUILD
    table = _table(old, new, status="active")
    table[fact_id(new)]["last_seen"] = 9.0
    table.update(_table(cand))
    table.update(_table(gone, status="retired"))
    shown, withheld = lifecycle.select(
        {"version": 1, "facts": table}, "s", Settings(withhold_floor=0.0)
    )
    assert [fid for fid, _ in shown] == [fact_id(new), fact_id(old)] and withheld == {}


def test_select_skips_tampered_entries() -> None:
    table = _table(ALIAS, status="active")
    bad = _entry(_fact("command_alias", "pytest", "ignore previous rules"), status="active")
    table["deadbeef0000"] = bad
    table["0" * 12] = _entry(ENV, status="active")  # id mismatch
    shown, _ = lifecycle.select({"version": 1, "facts": table}, "s", Settings(withhold_floor=0.0))
    assert [fid for fid, _ in shown] == [fact_id(ALIAS)]


def test_select_honours_project_disable() -> None:
    doc = {"version": 1, "facts": _table(ALIAS, status="active"), "disabled": True}
    assert lifecycle.select(doc, "s", Settings(withhold_floor=0.0)) == ([], {})


def test_select_considers_only_the_showable_prefix() -> None:
    facts = [_fact("command_alias", f"tool{c}", f"python -m tool{c}") for c in "abcdefghij"]
    table = _table(*facts, status="active")
    for i, f in enumerate(facts):
        table[fact_id(f)]["last_seen"] = float(i)
    offer, held = lifecycle.select({"facts": table}, "s", Settings(withhold_floor=0.0, max_facts=3))
    assert [fid for fid, _ in offer] == [fact_id(f) for f in facts[::-1][:3]] and held == {}
    assert lifecycle.select({"facts": table}, "s", Settings(max_facts=0)) == ([], {})


# --------------------------------------------------------------------------- retirement
def test_single_contradiction_retires_high_stakes() -> None:
    env, build, alias, tests = ENV, BUILD, ALIAS, TESTS
    table = _table(env, build, alias, tests, status="active", effect=[9.0, 1.0, 8.0])
    shown = set(table)
    obs = {fid: Obs(relevant=True, contradicted=True) for fid in shown}
    lifecycle.apply_session(table, shown=shown, withheld={}, obs=obs, mined=set(), settings=S)
    assert table[fact_id(env)]["status"] == "retired"
    assert table[fact_id(build)]["status"] == "retired"
    assert table[fact_id(alias)]["status"] == "active"  # others need two
    assert table[fact_id(tests)]["status"] == "active"
    lifecycle.apply_session(table, shown=shown, withheld={}, obs=obs, mined=set(), settings=S)
    assert table[fact_id(alias)]["status"] == "retired"
    assert table[fact_id(tests)]["status"] == "retired"
    assert table[fact_id(alias)]["contradictions"] == 2


def test_contradiction_counts_only_after_injection() -> None:
    fid = fact_id(ENV)
    table = _table(ENV, status="active")
    obs = {fid: Obs(relevant=True, contradicted=True)}
    lifecycle.apply_session(table, shown=(), withheld={fid: 0.1}, obs=obs, mined=set(), settings=S)
    assert table[fid]["status"] == "active" and table[fid]["contradictions"] == 0


def test_retire_when_ucb_below_threshold() -> None:
    fid = fact_id(ALIAS)
    table = _table(ALIAS, status="active", effect=[2.0, 9.0, 9.0])  # ucb(0.9) ~ 0.33
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=set(), settings=S)
    assert table[fid]["status"] == "retired"


def test_retired_fact_stays_retired_when_mined_again() -> None:
    fid = fact_id(ALIAS)
    table = _table(ALIAS, status="retired")
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined={fid}, settings=S)
    assert table[fid]["status"] == "retired"


def test_malformed_lifecycle_fields_fall_back_to_prior() -> None:
    entry = _entry(ALIAS, status="weird", effect="x", baseline=[0, -1, 2])
    assert lifecycle.status(entry) == "candidate"
    assert lifecycle.effect(entry) == Beta(1.0, 1.0)
    assert lifecycle.baseline(entry) == Beta(1.0, 1.0)


# --------------------------------------------------------------------------- relevance / waste
def test_alias_relevance_waste_and_contradiction(tmp_path: Path) -> None:
    fid = fact_id(ALIAS)
    waste = Log().prompt().bash("pytest -q", "zsh: command not found: pytest", err=True)
    assert _observe(waste, tmp_path, ALIAS)[fid] == Obs(relevant=True, waste=True)
    clean = Log().prompt().bash("python -m pytest -q", "3 passed")
    assert _observe(clean, tmp_path, ALIAS)[fid] == Obs(relevant=True, waste=False)
    other = Log().prompt().bash("ls -la")
    assert _observe(other, tmp_path, ALIAS)[fid] == Obs(relevant=False, waste=False)
    bad = Log().prompt().bash("python -m pytest -q", "No module named pytest", err=True)
    assert _observe(bad, tmp_path, ALIAS)[fid].contradicted
    usage = Log().prompt().bash("python -m pytest -q", "usage: pytest [options]", err=True)
    assert _observe(usage, tmp_path, ALIAS)[fid].contradicted
    failing = Log().prompt().bash("python -m pytest -q", "1 failed", err=True)
    assert not _observe(failing, tmp_path, ALIAS)[fid].contradicted


def test_test_command_relevance_and_waste(tmp_path: Path) -> None:
    fid = fact_id(TESTS)
    log = (
        Log()
        .prompt()
        .bash("pytest -q", "bash: pytest: command not found", err=True)
        .bash("python -m pytest -q", "3 passed")
    )
    assert _observe(log, tmp_path, TESTS)[fid] == Obs(relevant=True, waste=True, waste_event=True)
    real_failure = Log().prompt().bash("python -m pytest -q", "1 failed", err=True)
    real_failure.bash("python -m pytest -q", "2 passed")
    assert _observe(real_failure, tmp_path, TESTS)[fid] == Obs(relevant=True, waste=False)
    assert _observe(Log().prompt().bash("ls"), tmp_path, TESTS)[fid].relevant is False
    gone = Log().prompt().bash("python -m pytest -q", "usage: python -m pytest", err=True)
    assert _observe(gone, tmp_path, TESTS)[fid].contradicted


def test_requires_env_relevance_waste_and_contradiction(tmp_path: Path) -> None:
    fid = fact_id(ENV)
    log = Log().prompt().bash("make run", "KeyError: 'DATABASE_URL'", err=True)
    assert _observe(log, tmp_path, ENV)[fid] == Obs(relevant=True, waste=True)
    ok = Log().prompt().bash("DATABASE_URL=x make run", "ok")
    assert _observe(ok, tmp_path, ENV)[fid] == Obs(relevant=True, waste=False)
    exported = Log().prompt().bash("export DATABASE_URL=x").bash("make run", "ok")
    assert not _observe(exported, tmp_path, ENV)[fid].contradicted
    bare = Log().prompt().bash("make run", "ok")
    assert _observe(bare, tmp_path, ENV)[fid].contradicted  # ran fine without it
    assert _observe(Log().prompt().bash("make lint"), tmp_path, ENV)[fid].relevant is False


def test_build_first_relevance_waste_and_contradiction(tmp_path: Path) -> None:
    fid = fact_id(BUILD)
    log = (
        Log()
        .prompt()
        .bash("npm test", "Cannot find module dist/index.js", err=True)
        .bash("npm run build", "built")
        .bash("npm test", "ok")
    )
    assert _observe(log, tmp_path, BUILD)[fid] == Obs(relevant=True, waste=True)
    ok = Log().prompt().bash("npm run build").bash("npm test")
    assert _observe(ok, tmp_path, BUILD)[fid] == Obs(relevant=True, waste=False)
    gone = Log().prompt().bash("npm run build", 'npm ERR! Missing script: "build"', err=True)
    gone.bash("npm run build", "usage: npm run <script>", err=True)
    assert _observe(gone, tmp_path, BUILD)[fid].contradicted


def test_generated_dir_relevance_only(tmp_path: Path) -> None:
    fid = fact_id(GEN)
    log = Log().prompt().call("Edit", {"file_path": f"{PROJECT}/dist/a.js"})
    assert _observe(log, tmp_path, GEN)[fid] == Obs(relevant=True, waste=None)
    other = Log().prompt().call("Edit", {"file_path": f"{PROJECT}/src/dist.js"})
    assert _observe(other, tmp_path, GEN)[fid].relevant is False


def test_observer_ignores_sidechains(tmp_path: Path) -> None:
    fid = fact_id(ALIAS)
    log = Log().prompt().bash("pytest", "command not found: pytest", err=True, isSidechain=True)
    assert _observe(log, tmp_path, ALIAS)[fid].relevant is False


# --------------------------------------------------------------------------- fold wiring
def _overlay(home: Path, sid: str, transcript: Path, facts_log: dict[str, Any]) -> None:
    path = session.session_path(home, KEY, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "evidence": {},
        "transcript": str(transcript),
        "project_dir": PROJECT,
        lifecycle.SESSION_KEY: facts_log,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _store(home: Path) -> Path:
    return home / "projects" / KEY / miners.FACTS_FILE


def test_fold_applies_the_session_outcome_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fid = fact_id(ALIAS)
    _store(home).parent.mkdir(parents=True)
    doc = {"version": 1, "facts": _table(ALIAS, status="active", effect=[4.0, 1.0, 3.0])}
    _store(home).write_text(json.dumps(doc), encoding="utf-8")
    tr = Log().prompt().bash("python -m pytest -q", "3 passed").write(tmp_path / "s.jsonl")
    _overlay(home, "s1", tr, {"shown": {fid: 0.9}, "withheld": {}})
    assert overlay.fold("s1", KEY, home).staged
    after = json.loads(_store(home).read_text(encoding="utf-8"))
    assert after["facts"][fid]["effect"] == [5.0, 1.0, 4.0]
    assert overlay.fold("s1", KEY, home).staged is False  # nothing new: applied once
    again = json.loads(_store(home).read_text(encoding="utf-8"))
    assert again["facts"][fid]["effect"] == [5.0, 1.0, 4.0]


def test_fold_without_transcript_still_ignores_facts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = session.session_path(home, KEY, "s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"evidence": {}, lifecycle.SESSION_KEY: {"shown": {}}}), "utf-8")
    assert overlay.fold("s2", KEY, home).facts == 0


def test_record_keeps_lifecycle_state_and_forgotten_ids(tmp_path: Path) -> None:
    pdir = tmp_path / "p"
    pdir.mkdir()
    fid = fact_id(ALIAS)
    doc = {
        "version": 1,
        "facts": _table(ALIAS, status="active", effect=[4.0, 1.0, 3.0]),
        "disabled": True,
        "forgotten": [fact_id(ENV)],
    }
    (pdir / miners.FACTS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    miners.record(pdir, [ALIAS, ENV], env={"TOKEN_SPAREN_HOME": str(tmp_path)})
    after = json.loads((pdir / miners.FACTS_FILE).read_text(encoding="utf-8"))
    assert after["facts"][fid]["status"] == "active"
    assert after["facts"][fid]["effect"] == [4.0, 1.0, 3.0]
    assert fact_id(ENV) not in after["facts"]  # forgotten: never re-learned
    assert after["disabled"] is True and after["forgotten"] == [fact_id(ENV)]


def test_failed_mining_keeps_the_session_log_for_the_next_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    fid = fact_id(ALIAS)
    _store(home).parent.mkdir(parents=True)
    doc = {"version": 1, "facts": _table(ALIAS, status="active", effect=[4.0, 1.0, 3.0])}
    _store(home).write_text(json.dumps(doc), encoding="utf-8")
    tr = Log().prompt().bash("python -m pytest -q", "3 passed").write(tmp_path / "s.jsonl")
    _overlay(home, "s1", tr, {"shown": {fid: 0.9}, "withheld": {}})

    def boom(*_a: object, **_k: object) -> list[Fact]:
        raise RuntimeError("disk")

    monkeypatch.setattr(miners, "mine_transcript", boom)
    assert overlay.fold("s1", KEY, home).staged
    state = session.read_session(session.session_path(home, KEY, "s1"))
    assert state[lifecycle.SESSION_KEY]["shown"] == {fid: 0.9}
    assert fid not in state.get(lifecycle.DONE_KEY, [])
    monkeypatch.undo()
    path = session.session_path(home, KEY, "s1")
    state["evidence"] = {"x": {}}  # new activity: the session is folded again
    path.write_text(json.dumps(state), encoding="utf-8")
    overlay.fold("s1", KEY, home)
    after = json.loads(_store(home).read_text(encoding="utf-8"))
    assert after["facts"][fid]["effect"] == [5.0, 1.0, 4.0]


# --------------------------------------------------------------------------- fix round 1
PYTEST = _fact("test_command", "tests", "python -m pytest -q")


def _tox_session() -> Log:
    log = Log().prompt()
    for _ in range(3):
        log.bash("tox -e py", "ok")
    return log


def test_test_command_reward_needs_its_own_command(tmp_path: Path) -> None:
    fid = fact_id(PYTEST)
    other = Log().prompt().bash("make check", "ok").bash("pytest tests/unit", "ok")
    o = _observe(other, tmp_path, PYTEST)[fid]
    assert o.relevant and o.waste and o.contradicted  # a different runner dominated
    own = Log().prompt().bash("python -m pytest -q tests/a.py", "1 passed")
    o = _observe(own, tmp_path, PYTEST)[fid]
    assert o.relevant and not o.waste and not o.contradicted
    tie = Log().prompt().bash("python -m pytest -q").bash("tox -e py")
    assert not _observe(tie, tmp_path, PYTEST)[fid].contradicted


def test_migration_retires_old_test_command_within_three_sessions(tmp_path: Path) -> None:
    fid = fact_id(PYTEST)
    table = _table(PYTEST, status="active", effect=[9.0, 1.0, 8.0])
    sessions = 0
    while table[fid]["status"] != "retired" and sessions < 3:
        obs = _observe(_tox_session(), tmp_path, PYTEST)
        lifecycle.apply_session(table, shown={fid}, withheld={}, obs=obs, mined=set(), settings=S)
        sessions += 1
    assert table[fid]["status"] == "retired" and sessions <= 3


def test_activating_a_test_command_retires_the_old_one() -> None:
    tox = _fact("test_command", "tests", "tox -e py", n=5)
    table = _table(PYTEST, status="active")
    table.update(_table(tox, waste_events=1))
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=set(), settings=S)
    assert table[fact_id(tox)]["status"] == "active"
    assert table[fact_id(PYTEST)]["status"] == "retired"
    both = _table(
        _fact("test_command", "tests", "tox", n=5), _fact("test_command", "tests", "nox", n=6)
    )
    for e in both.values():
        e["waste_events"] = 1
    lifecycle.apply_session(both, shown=(), withheld={}, obs={}, mined=set(), settings=S)
    assert [e["status"] for e in both.values()].count("active") == 1


def test_build_first_unneeded_when_tests_pass_before_any_build(tmp_path: Path) -> None:
    fid = fact_id(BUILD)
    log = Log().prompt().bash("npm test", "ok").bash("npm run build").bash("npm test")
    assert _observe(log, tmp_path, BUILD)[fid].unneeded
    built = Log().prompt().bash("npm run build").bash("npm test")
    assert not _observe(built, tmp_path, BUILD)[fid].unneeded
    table = _table(BUILD, status="active", effect=[9.0, 1.0, 8.0])
    obs = {fid: _observe(log, tmp_path, BUILD)[fid]}
    lifecycle.apply_session(table, shown=(), withheld={fid: 0.1}, obs=obs, mined=set(), settings=S)
    assert table[fid]["status"] == "active" and table[fid]["unneeded"] == 1  # withheld counts
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs=obs, mined=set(), settings=S)
    assert table[fid]["status"] == "retired"


def test_alias_unneeded_when_the_subject_runs(tmp_path: Path) -> None:
    fid = fact_id(ALIAS)
    log = Log().prompt().bash("pytest -q", "3 passed")
    o = _observe(log, tmp_path, ALIAS)[fid]
    assert o.unneeded and not o.waste
    table = _table(ALIAS, status="active", effect=[9.0, 1.0, 8.0])
    for _ in range(2):
        lifecycle.apply_session(
            table, shown={fid}, withheld={}, obs={fid: o}, mined=set(), settings=S
        )
    assert table[fid]["status"] == "retired"


def test_retire_when_baseline_is_as_good_as_injection() -> None:
    same = _fact("command_alias", "black", "python -m black")
    better = _fact("command_alias", "ruff", "python -m ruff")
    table = _table(same, status="active", effect=[4.0, 1.0, 3.0], baseline=[3.0, 1.0, 3.0])
    table.update(_table(better, status="active", effect=[4.0, 1.0, 3.0], baseline=[1.0, 4.0, 3.0]))
    lifecycle.apply_session(table, shown=(), withheld={}, obs={}, mined=set(), settings=S)
    assert table[fact_id(same)]["status"] == "retired"
    assert table[fact_id(better)]["status"] == "active"


def test_generated_dir_repeated_writes_while_shown_reward_zero(tmp_path: Path) -> None:
    fid = fact_id(GEN)
    log = Log().prompt()
    for name in ("a", "b"):
        log.call("Edit", {"file_path": f"{PROJECT}/dist/{name}.js"})
    o = _observe(log, tmp_path, GEN)[fid]
    assert o == Obs(relevant=True, waste=True)
    regen = Log().prompt().call("Edit", {"file_path": f"{PROJECT}/dist/a.js"})
    regen.bash("npm run build", "built")
    assert _observe(regen, tmp_path, GEN)[fid] == Obs(relevant=True, waste=True, waste_event=True)
    table = _table(GEN, status="active")
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs={fid: o}, mined=set(), settings=S)
    assert lifecycle.effect(table[fid]).to_list() == [1.0, 2.0, 1.0]


def test_frequency_facts_go_stale() -> None:
    fid = fact_id(PYTEST)
    table = _table(PYTEST, status="active", effect=[9.0, 1.0, 8.0], stale=9)
    rel = {fid: Obs(relevant=True, waste=False)}
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs=rel, mined={fid}, settings=S)
    assert table[fid]["stale"] == 0 and table[fid]["status"] == "active"  # new evidence
    table[fid]["stale"] = 9
    lifecycle.apply_session(table, shown={fid}, withheld={}, obs=rel, mined=set(), settings=S)
    assert table[fid]["status"] == "retired"  # 10 relevant sessions without new evidence
    old = _table(GEN, status="active")
    day = 86400.0
    lifecycle.apply_session(old, shown=(), withheld={}, obs={}, mined=set(), settings=S, now=day)
    assert old[fact_id(GEN)]["status"] == "active"
    lifecycle.apply_session(
        old, shown=(), withheld={}, obs={}, mined=set(), settings=S, now=2.0 + 31 * day
    )
    assert old[fact_id(GEN)]["status"] == "retired"  # 30 days without new evidence


def test_fold_observes_frequency_candidates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cand = _fact("test_command", "tests", "python -m pytest -q", n=4)
    fid = fact_id(cand)
    _store(home).parent.mkdir(parents=True)
    doc = {"version": 1, "facts": _table(cand)}
    _store(home).write_text(json.dumps(doc), encoding="utf-8")
    log = Log().prompt().bash("pytest -q", "bash: pytest: command not found", err=True)
    tr = log.bash("python -m pytest -q", "3 passed").write(tmp_path / "s.jsonl")
    _overlay(home, "s5", tr, {})
    overlay.fold("s5", KEY, home)
    after = json.loads(_store(home).read_text(encoding="utf-8"))["facts"][fid]
    assert after["waste_events"] == 1 and len(after["sessions"]) == 5
    assert after["status"] == "active"
