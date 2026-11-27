"""Public E4 report and `e4.json` (M5 Task 11). Synthetic ledgers and grades only."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bench import e4, report, svg
from bench import ledger as lg
from bench.stats import summarise
from scripts import render_evidence
from scripts.docs_lint import figure_violations

HASH = "ab" * 32
COMMIT = "0123456789abcdef0123456789abcdef01234567"
TASKS = [f"django__django-{1000 + i}" for i in range(40)] + [
    f"sphinx-doc__sphinx-{2000 + i}" for i in range(40)
]
N_BOOT = 200


@dataclass(frozen=True)
class FakeGate:
    verdict: str
    reasons: list[str] = field(default_factory=list)


POWERED = {"required_tasks": 76, "seeds": 1, "achieved_power": 0.83, "verdict": "powered"}


def _header(stage: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "campaign": "c1",
        "stage": stage,
        "harness_commit": COMMIT,
        "plugin_hash": HASH,
        "plugin_ref": "v1.0.0-rc1" if stage == "confirmation" else "v0.5.0-screen1",
        "plugin_version": "1.0.0" if stage == "confirmation" else "0.5.0",
        "pins": {"claude_code": "2.1.121"},
        "model": "claude-sonnet-x",
        "claude_code_version": "2.1.121",
        "price_confirmed_on": "2027-01-03",
    }
    return doc | over


def _key(stage: str, arm: str, seed: int, iid: str) -> str:
    return lg.RunKey("c1", stage, arm, seed, iid).id


def make_campaign(
    tmp_path: Path,
    *,
    stage: str = "confirmation",
    arms: tuple[str, ...] = ("baseline", "token-sparen", "rtk"),
    pending: bool = False,
    header: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """80 tasks; every arm resolves the even tasks (no discordance); token-sparen costs 80 %
    of baseline; rtk has one task excluded after three `api` infra failures."""
    led = lg.Ledger(tmp_path / "ledger.jsonl", header=header or _header(stage))
    grades: list[dict[str, Any]] = []
    t0 = 1_800_000_000.0
    for i, iid in enumerate(TASKS):
        for arm in arms:
            key = _key(stage, arm, 0, iid)
            led.append({"kind": "start", "key": key, "attempt": 1, "at": t0 + i})
            if arm == "rtk" and i == 3:
                for attempt in (1, 2, 3):
                    led.append(
                        {
                            "kind": "end",
                            "key": key,
                            "attempt": attempt,
                            "status": "infra_fail",
                            "sub": "api",
                            "at": t0 + i,
                        }
                    )
                continue
            if pending and arm == "token-sparen" and i == 5:
                continue  # started, never finished: a pending key
            base = 2.0 + 0.01 * i
            cost = {"baseline": base, "token-sparen": 0.8 * base}.get(arm, 0.9 * base)
            led.append(
                {
                    "kind": "end",
                    "key": key,
                    "attempt": 1,
                    "status": "ok",
                    "sub": "success",
                    "run_cost": cost,
                    "repo": iid.rsplit("-", 1)[0].replace("__", "/"),
                    "model": "claude-sonnet-x",
                    "claude_code_version": "2.1.121",
                    "plugin_hash": HASH,
                    "live": True,
                    "at": t0 + 86400 * (i // 20) + i,
                }
            )
            grades.append(
                {
                    "kind": "grade",
                    "stage": stage,
                    "key": key,
                    "arm": arm,
                    "resolved": i % 2 == 0,
                    "patch_sha256": "0" * 64,
                    "reason": None,
                }
            )
    path = tmp_path / "grades.jsonl"
    path.write_text("".join(json.dumps(g) + "\n" for g in grades), encoding="utf-8")
    return led.path, path


def build(
    tmp_path: Path,
    *,
    stage: str = "confirmation",
    gate: FakeGate | None = None,
    power: dict[str, Any] | None = None,
    preflight: Path | None = None,
    **kw: Any,
) -> Path:
    ledger, grades = make_campaign(tmp_path, stage=stage, **kw)
    if stage == "confirmation":
        gate = gate or FakeGate("passed")
        power = power or POWERED
    return report.build_report(
        ledger,
        grades,
        stage=stage,
        out_dir=tmp_path / "out",
        gate=gate,
        power=power,
        n_boot=N_BOOT,
        preflight=preflight,
    )


def _report_text(out: Path) -> str:
    return (out / "report.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- source guard


def test_report_refuses_non_e4_input(tmp_path: Path) -> None:
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({"kind": "e1-replay", "schema": 1}) + "\n", encoding="utf-8")
    with pytest.raises(report.NotE4Error):
        report.assert_e4_source(replay)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(report.NotE4Error):
        report.assert_e4_source(empty)
    with pytest.raises(report.NotE4Error):
        report.assert_e4_source(tmp_path / "missing.jsonl")
    grades = tmp_path / "grades.jsonl"
    grades.write_text("", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(report.NotE4Error):
        report.build_report(replay, grades, stage="screening", out_dir=out, gate=None, power=None)
    assert not out.exists()
    ledger, _ = make_campaign(tmp_path / "ok", stage="screening")
    report.assert_e4_source(ledger)  # the E4 header passes


# --------------------------------------------------------------------------- private data


def test_report_has_no_private_paths_or_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "someone"
    token = "sk-ant-oat01-" + "Q" * 40
    scan = report.scan_private
    kw: dict[str, Any] = {"home": home, "deny": ["Initech"], "token": token}
    assert scan("plain public text 2.1.121", **kw) == []
    for bad in (
        f"see {home}/x",
        "at /" + "Users/alice/project",
        "at C:" + "\\Users\\alice\\project",
        "made at INITECH",
        "contact a.person@example.org",
        "token " + token,
        "another sk-ant-oat01-" + "Z" * 20,
    ):
        hits = scan(bad, **kw)
        assert hits, bad
        assert all(token not in h and "alice" not in h for h in hits)  # names, never values

    # the generated artefacts are clean
    out = build(tmp_path)
    for f in [out / "e4.json", out / "report.md", *sorted((out / "figures").glob("*.svg"))]:
        assert scan(f.read_text(encoding="utf-8"), home=Path.home(), deny=[], token=None) == []

    # a private word reaching the ledger header: nothing is written
    monkeypatch.setenv("TOKEN_SPAREN_DENYLIST", "Initech")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    ledger, grades = make_campaign(
        bad_dir, stage="screening", header=_header("screening", plugin_ref="initech-v0.5.0")
    )
    with pytest.raises(report.PrivateDataError) as exc:
        report.build_report(
            ledger,
            grades,
            stage="screening",
            out_dir=bad_dir / "out",
            gate=None,
            power=None,
            n_boot=N_BOOT,
        )
    assert "initech" not in str(exc.value).lower()
    assert not (bad_dir / "out" / "e4.json").exists()
    assert not (bad_dir / "out" / "report.md").exists()


# --------------------------------------------------------------------------- numbers


_DECIMAL = re.compile(r"(?<![\w.])-?\d+\.\d+(?![\w.])")


def test_every_number_has_ci_and_n(tmp_path: Path) -> None:
    pre = tmp_path / "preflight.json"
    results: dict[str, dict[str, str | None]] = {
        "astropy__astropy-7": {"status": "excluded", "reason": "flaky_gold"}
    }
    results["django__django-1000"] = {"status": "ok", "reason": None}
    pre.write_text(json.dumps({"attempts": 1, "results": results}), encoding="utf-8")
    out = build(tmp_path, preflight=pre)
    text = _report_text(out)
    doc = e4.load_e4(out / "e4.json")
    for row in doc["results"]:
        assert report.format_result(row) in text, row
    # every estimate in the report carries its CI and n (code spans hold ids and versions)
    for line in text.splitlines():
        bare = re.sub(r"`[^`]*`", " ", line)
        if _DECIMAL.search(bare):
            assert "95 % CI [" in bare and "n = " in bare, line
    # the claims block comes first and names every claims-policy field
    head = text.split("## Results", 1)[0]
    for needle in (
        "Model ids",
        "Claude Code",
        "Plugin version",
        "Plugin hash",
        "Harness commit",
        "Seeds",
        "Dates",
        "Grader",
        "swebench5-arm64",
        "Price table",
        "confirmed against live pricing on 2027-01-03",
        "Power",
        "Release gate",
        "Exclusions per arm",
        "Exclusions per repository",
        "grade_oom_repeat",
        "Network access",
        "TokenEconomizer",
        "caveman",
        "headroom",
        "descriptive",
        "non-inferior",
    ):
        assert needle in head, needle
    assert "django/django" in text and "sphinx-doc/sphinx" in text  # per-repo tables
    assert "| astropy/astropy | preflight 1 |" in text
    assert "`astropy__astropy-7`: grading preflight: flaky_gold (swebench5-arm64)" in text
    # passes the docs figure lint only through the hashed generated section, and only where
    # the lint allows generated sections (never README, docs pages or release notes)
    assert figure_violations(text, allow_generated=True) == []
    assert figure_violations(text) != []
    tampered = text.replace("95 % CI [", "95 % CI  [", 1)
    assert figure_violations(tampered, allow_generated=True) != []


# --------------------------------------------------------------------------- e4.json


def test_e4_json_validates_and_has_confirmatory_diff(tmp_path: Path) -> None:
    out = build(tmp_path)
    doc = e4.load_e4(out / "e4.json")  # what M6 reads
    assert doc["source"] == "E4" and doc["stage"] == "confirmation"
    assert doc["plugin_hash"] == HASH and doc["plugin_version"] == "1.0.0"
    assert doc["grader"] == {"name": "swebench", "version": "5.0.2", "arch": "arm64"}
    assert doc["n_tasks"] == 80 and doc["seeds"] == 1
    assert doc["date"] == {"from": "2027-01-15", "to": "2027-01-18"}
    (conf,) = [r for r in doc["results"] if r["confirmatory"]]
    assert (conf["metric"], conf["arm"], conf["baseline"]) == (
        "resolved_diff_pp",
        "token-sparen",
        "baseline",
    )
    ledger = lg.Ledger(tmp_path / "ledger.jsonl")
    from bench.grade import load_grades

    summ = summarise(
        ledger.records(),
        load_grades(tmp_path / "grades.jsonl", stage="confirmation"),
        stage="confirmation",
        excluded=ledger.excluded_after_retries("confirmation"),
    )
    gate = summ.gate(n_boot=N_BOOT)
    assert conf["n"] == gate.estimate.n == 80
    assert conf["estimate"] == pytest.approx(100 * gate.estimate.point, abs=1e-3)
    assert conf["ci95"][0] == pytest.approx(100 * gate.estimate.lo, abs=1e-3)
    assert conf["margin_pp"] == -5 and conf["non_inferior"] is True
    cost = [
        r
        for r in doc["results"]
        if r["metric"] == "cost_ratio_median" and r["arm"] == "token-sparen"
    ]
    assert cost and cost[0]["estimate"] == pytest.approx(0.8)
    assert doc["exclusions"] and any("rtk" in x["reason"] for x in doc["exclusions"])
    assert doc["gate"] == {"verdict": "passed", "reasons": []}
    # the M6 README renderer consumes it unchanged
    assert "resolved_diff_pp (confirmatory)" in render_evidence.render_evidence(out / "e4.json")

    # screening: no gate, no power, labelled screening, still valid
    scr = build(tmp_path / "s", stage="screening")
    sdoc = e4.load_e4(scr / "e4.json")
    assert sdoc["power"] is None and sdoc["gate"] is None and sdoc["style_rule"] == "not_run"

    # confirmation without a gate is refused (validate_e4 names the problem)
    with pytest.raises(report.ReportError, match="gate"):
        ledger_p, grades_p = make_campaign(tmp_path / "x", stage="confirmation")
        report.build_report(
            ledger_p,
            grades_p,
            stage="confirmation",
            out_dir=tmp_path / "x" / "out",
            gate=None,
            power=POWERED,
            n_boot=N_BOOT,
        )


# --------------------------------------------------------------------------- publish


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_publish_writes_docs_evidence_only_for_confirmation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    scr = build(tmp_path / "s", stage="screening")
    written = report.publish(scr, repo)
    assert _files(repo) == {
        "docs/benchmarks/c1-screening/report.md",
        "docs/benchmarks/c1-screening/e4.json",
        "docs/benchmarks/c1-screening/figures/cost_ratio.svg",
        "docs/benchmarks/c1-screening/figures/resolved_diff.svg",
        "docs/benchmarks/c1-screening/figures/pareto.svg",
    }
    assert sorted(written) == sorted(repo / f for f in _files(repo))
    assert not (repo / "docs" / "evidence").exists()

    conf = build(tmp_path / "c")
    report.publish(conf, repo)
    evidence = repo / "docs" / "evidence" / "e4.json"
    assert evidence.read_bytes() == (conf / "e4.json").read_bytes()
    assert (repo / "docs/benchmarks/c1-confirmation/report.md").is_file()

    # refused: a stage with a pending (never finished) key is not completely graded
    pend = build(tmp_path / "p", pending=True)
    repo2 = tmp_path / "repo2"
    with pytest.raises(report.ReportError, match="graded"):
        report.publish(pend, repo2)
    assert not repo2.exists()

    # refused: e4.json no longer valid
    broken = json.loads((conf / "e4.json").read_text(encoding="utf-8"))
    broken["source"] = "E1"
    (conf / "e4.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(e4.E4Error):
        report.publish(conf, repo2)

    # refused: confirmation price table never confirmed against live pricing
    hdr = _header("confirmation")
    del hdr["price_confirmed_on"]
    unconf = build(tmp_path / "u", header=hdr)
    with pytest.raises(report.ReportError, match="live pricing"):
        report.publish(unconf, repo2)
    assert not repo2.exists()


# --------------------------------------------------------------------------- figures


def test_svg_well_formed(tmp_path: Path) -> None:
    rows = [
        svg.Row("token-sparen <vs> baseline & co", -0.5, -3.1, 2.0, 80),
        svg.Row("rtk", 1.2, -4.0, 6.5, 79),
    ]
    points = [svg.Point("token-sparen", 0.8, -0.5), svg.Point("rtk", 0.9, 1.2)]
    for text in (
        svg.forest(rows, ref=-5.0, title="Resolved difference (pp) & margin"),
        svg.scatter(points, title="Cost vs quality"),
    ):
        root = ET.fromstring(text)
        assert root.tag.endswith("svg")
        assert "prefers-color-scheme" in text
        assert "<title>" in text
        labels = [t.text or "" for t in root.iter() if t.tag.endswith("text")]
        assert labels
        colours = set(re.findall(r"#[0-9A-Fa-f]{6}\b", text.split("@media", 1)[0]))
        assert len(colours - set(svg.NEUTRALS)) <= 2  # two-tone data palette
    assert any(
        "token-sparen <vs> baseline & co" in (t.text or "")
        for t in ET.fromstring(svg.forest(rows, ref=0.0, title="t")).iter()
    )
    out = build(tmp_path)
    for name in ("cost_ratio", "resolved_diff", "pareto"):
        ET.fromstring((out / "figures" / f"{name}.svg").read_text(encoding="utf-8"))
