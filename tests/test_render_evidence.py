"""Evidence renderer, release gate and the README Evidence block (M6 Task 6)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from bench.e4 import E4Error
from scripts import plugin_hash as ph
from scripts import render_evidence as ev

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = b'{"name": "demo", "version": "1.0.0"}\n'


def make_tree(root: Path, body: bytes = b"x = 1\n") -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_bytes(PLUGIN_JSON)
    (root / "token_sparen").mkdir()
    (root / "token_sparen" / "a.py").write_bytes(body)
    return root


def doc(plugin_hash: str, verdict: str = "passed") -> dict[str, Any]:
    powered = "powered" if verdict == "passed" else "underpowered"
    return {
        "schema_version": 1,
        "source": "E4",
        "suite": "swe-bench-verified-mini",
        "stage": "confirmation",
        "campaign": "demo-1",
        "n_tasks": 40,
        "seeds": 3,
        "model": "model-x",
        "claude_code_version": "2.1.332",
        "date": {"from": "2026-12-01", "to": "2026-12-09"},
        "plugin_version": "1.0.0",
        "plugin_hash": plugin_hash,
        "harness_commit": "a" * 40,
        "script": "python -m bench run",
        "grader": {"name": "swebench", "version": "4.0", "arch": "amd64"},
        "arms": [
            {"name": "baseline", "kind": "baseline", "pin": None},
            {"name": "token-sparen", "kind": "plugin", "pin": None},
        ],
        "exclusions": [],
        "power": {
            "required_tasks": 40,
            "seeds": 3,
            "achieved_power": 0.9,
            "verdict": powered,
        },
        "results": [
            {
                "metric": "resolved_diff_pp",
                "arm": "token-sparen",
                "baseline": "baseline",
                "estimate": -1.5,
                "ci95": [-3.0, 0.5],
                "n": 120,
                "method": "paired bootstrap",
                "confirmatory": True,
                "margin_pp": -5,
                "non_inferior": True,
            },
            {
                "metric": "cost_ratio_median",
                "arm": "token-sparen",
                "baseline": "baseline",
                "estimate": 0.8,
                "ci95": [0.7, 0.9],
                "n": 120,
                "method": "paired bootstrap",
                "confirmatory": False,
            },
        ],
        "gate": {"verdict": verdict, "reasons": []},
        "style_rule": "keep_off",
    }


def write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree")


def good(tmp_path: Path, tree: Path, verdict: str = "passed") -> Path:
    return write(tmp_path / "e4.json", doc(ph.plugin_hash(tree), verdict))


def test_evidence_renderer_rejects_non_e4(tmp_path: Path, tree: Path) -> None:
    data = doc(ph.plugin_hash(tree))
    data["source"] = "E1"
    with pytest.raises(E4Error):
        ev.render_evidence(write(tmp_path / "e1.json", data))


def test_evidence_requires_claims_fields(tmp_path: Path, tree: Path) -> None:
    for field in ("n_tasks", "seeds", "model", "claude_code_version", "date", "plugin_hash"):
        data = copy.deepcopy(doc(ph.plugin_hash(tree)))
        del data[field]
        with pytest.raises(E4Error, match=field):
            ev.render_evidence(write(tmp_path / f"{field}.json", data))


def test_placeholder_without_results(tmp_path: Path) -> None:
    text = ev.render_evidence(None)
    assert text.startswith("No published benchmark results yet.")
    assert not re.search(r"\d", text.replace("95 %", "").replace("(E4)", ""))
    assert ev.render_evidence(tmp_path / "missing.json") == text


def test_evidence_states_measured_version_and_hash(tmp_path: Path, tree: Path) -> None:
    text = ev.render_evidence(good(tmp_path, tree))
    for needle in (
        "40 tasks",
        "3 seeds",
        "model-x",
        "2.1.332",
        "2026-12-01",
        "2026-12-09",
        ph.plugin_hash(tree),
        "1.0.0",
        "a" * 40,
        "[-3.0, 0.5]",
        "95 % CI",
    ):
        assert needle in text
    assert "resolved_diff_pp" in text and "cost_ratio_median" in text


def test_gate_passed_ok(tmp_path: Path, tree: Path) -> None:
    res = ev.check_release_gate(good(tmp_path, tree), tree, None)
    assert res.ok, res.reason


def test_gate_missing_evidence_fails(tmp_path: Path, tree: Path) -> None:
    assert not ev.check_release_gate(None, tree, None).ok
    assert not ev.check_release_gate(tmp_path / "nope.json", tree, None).ok


def test_gate_failed_never_overridable(tmp_path: Path, tree: Path) -> None:
    data = doc(ph.plugin_hash(tree), "failed")
    data["results"][0].update(non_inferior=False, ci95=[-8.0, -2.0])
    path = write(tmp_path / "e4.json", data)
    decision = tmp_path / "d.toml"
    decision.write_text('version = "1.0.0"\nrationale = "ship anyway"\n')
    res = ev.check_release_gate(path, tree, decision)
    assert not res.ok and "failed" in res.reason


def test_gate_underpowered_needs_decision_file(tmp_path: Path, tree: Path) -> None:
    path = good(tmp_path, tree, "underpowered")
    assert not ev.check_release_gate(path, tree, None).ok
    decision = tmp_path / "d.toml"
    decision.write_text('version = "1.0.0"\nrationale = "owner accepts"\n')
    assert ev.check_release_gate(path, tree, decision).ok
    decision.write_text('version = "0.9.0"\nrationale = "old"\n')
    assert not ev.check_release_gate(path, tree, decision).ok
    decision.write_text('version = "1.0.0"\n')
    assert not ev.check_release_gate(path, tree, decision).ok
    decision.write_text("not = [toml")
    assert not ev.check_release_gate(path, tree, decision).ok
    assert not ev.check_release_gate(path, tree, tmp_path / "absent.toml").ok


def test_gate_hash_mismatch_fails(tmp_path: Path, tree: Path) -> None:
    path = good(tmp_path, tree)
    (tree / "token_sparen" / "a.py").write_bytes(b"x = 2\n")
    res = ev.check_release_gate(path, tree, None)
    assert not res.ok and "hash" in res.reason


def test_gate_invalid_evidence_fails(tmp_path: Path, tree: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert not ev.check_release_gate(bad, tree, None).ok


def test_cli(tmp_path: Path, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = good(tmp_path, tree)
    assert ev.main(["--check", "--tree", str(tree), "--evidence", str(path)]) == 0
    (tree / "token_sparen" / "a.py").write_bytes(b"x = 3\n")
    assert ev.main(["--check", "--tree", str(tree), "--evidence", str(path)]) == 1
    assert "hash" in capsys.readouterr().err
    assert ev.main(["--stdout", "--evidence", str(path)]) == 0
    assert "model-x" in capsys.readouterr().out
    readme = tmp_path / "R.md"
    readme.write_text(f"a\n{ev.START}\nold\n{ev.END}\nb\n")
    assert ev.main(["--write", str(readme), "--evidence", str(path)]) == 0
    assert "model-x" in readme.read_text() and "old" not in readme.read_text()
    readme.write_text("no markers\n")
    assert ev.main(["--write", str(readme)]) == 1


def test_readme_evidence_block_matches_renderer() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    published = ROOT / "docs" / "evidence" / "e4.json"
    expected = ev.render_evidence(published if published.is_file() else None)
    assert ev.replace_block(text, expected) == text


def test_readme_keeps_operational_sections() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    heads = set(re.findall(r"^## (.+)$", text, re.M))
    for name in (
        "Quick start",
        "Status",
        "Evidence",
        "Requirements",
        "Install",
        "What changes in your session",
        "Recovering trimmed output",
        "Turning it off",
        "What it writes to disk",
        "Privacy",
        "Documentation",
        "Credits and licence",
    ):
        assert name in heads, name
    assert any(h.startswith("Settings it may change") for h in heads)
    assert "TOKEN_SPAREN_DISABLE=1" in text and "token-sparen show" in text
