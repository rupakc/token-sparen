"""`e4.json` schema (published interface; M6 reads `docs/evidence/e4.json`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bench import e4

FIXTURE = Path(__file__).parent / "fixtures" / "e4_example.json"
REQUIRED = (
    "schema_version",
    "source",
    "suite",
    "stage",
    "campaign",
    "n_tasks",
    "seeds",
    "model",
    "claude_code_version",
    "date",
    "plugin_version",
    "plugin_hash",
    "harness_commit",
    "script",
    "grader",
    "arms",
    "exclusions",
    "power",
    "results",
    "gate",
    "style_rule",
)


def _doc() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _confirmatory(doc: dict[str, Any]) -> dict[str, Any]:
    (row,) = [r for r in doc["results"] if r["metric"] == "resolved_diff_pp"]
    return row  # type: ignore[no-any-return]


def test_e4_example_valid() -> None:
    assert e4.SCHEMA_VERSION == 1
    assert e4.validate_e4(_doc()) == []
    assert e4.load_e4(FIXTURE)["plugin_hash"] == _doc()["plugin_hash"]


def test_schema_file_is_2020_12_and_lists_required_keys() -> None:
    schema = e4.load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert tuple(schema["required"]) == REQUIRED
    assert schema["properties"]["schema_version"]["const"] == e4.SCHEMA_VERSION
    assert e4.SCHEMA_FILE.stat().st_size < 256 * 1024


@pytest.mark.parametrize("key", REQUIRED)
def test_e4_missing_key_named(key: str) -> None:
    doc = _doc()
    del doc[key]
    errors = e4.validate_e4(doc)
    assert any(f"'{key}'" in err and "missing" in err for err in errors), errors


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        (("source",), "E1", "source"),
        (("schema_version",), 2, "schema_version"),
        (("plugin_hash",), "ABC", "plugin_hash"),
        (("harness_commit",), "deadbeef", "harness_commit"),
        (("date", "from"), "2027-13-40", "date.from"),
        (("date", "to"), "2027-01-01", "date"),
        (("grader", "name"), "sb-cli", "grader.name"),
        (("n_tasks",), 0, "n_tasks"),
        (("n_tasks",), True, "n_tasks"),
        (("stage",), "pilot", "stage"),
        (("style_rule",), "maybe", "style_rule"),
        (("script",), "python run.py", "script"),
        (("power", "verdict"), "meh", "power.verdict"),
        (("power", "achieved_power"), 1.5, "power.achieved_power"),
        (("gate", "verdict"), "ok", "gate.verdict"),
        (("arms", 1, "kind"), "magic", "arms[1].kind"),
        (("results", 1, "metric"), "tokens_median", "results[1].metric"),
        (("results", 1, "ci95"), [0.9, 0.7], "results[1].ci95"),
        (("results", 1, "ci95"), [0.7], "results[1].ci95"),
        (("results", 1, "arm"), "nobody", "results[1].arm"),
    ],
)
def test_e4_bad_values_named(path: tuple[str | int, ...], value: Any, needle: str) -> None:
    doc = _doc()
    target: Any = doc
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    errors = e4.validate_e4(doc)
    assert any(needle in err for err in errors), errors


def test_exactly_one_confirmatory_resolved_diff() -> None:
    doc = _doc()
    doc["results"] = [r for r in doc["results"] if r["metric"] != "resolved_diff_pp"]
    assert any("resolved_diff_pp" in e for e in e4.validate_e4(doc))
    doc = _doc()
    doc["results"].append(copy.deepcopy(_confirmatory(doc)))
    assert any("exactly one" in e for e in e4.validate_e4(doc))


@pytest.mark.parametrize(
    ("field", "value"),
    [("arm", "baseline"), ("baseline", "token-sparen"), ("margin_pp", -3), ("confirmatory", False)],
)
def test_confirmatory_row_is_token_sparen_vs_baseline_at_minus_5(field: str, value: Any) -> None:
    doc = _doc()
    _confirmatory(doc)[field] = value
    assert e4.validate_e4(doc)


def test_confirmatory_row_needs_non_inferior_bool_consistent_with_ci() -> None:
    doc = _doc()
    del _confirmatory(doc)["non_inferior"]
    assert any("non_inferior" in e for e in e4.validate_e4(doc))
    doc = _doc()
    _confirmatory(doc)["ci95"] = [-6.0, 2.4]  # lower bound below the margin
    assert any("non_inferior" in e for e in e4.validate_e4(doc))


def test_only_token_sparen_vs_baseline_is_confirmatory() -> None:
    doc = _doc()
    doc["results"][1]["confirmatory"] = True
    assert any("descriptive" in e for e in e4.validate_e4(doc))


def test_gate_consistent_with_non_inferiority_and_power() -> None:
    doc = _doc()
    _confirmatory(doc)["ci95"] = [-6.0, 2.4]
    _confirmatory(doc)["non_inferior"] = False
    assert any("gate" in e for e in e4.validate_e4(doc))  # passed while not non-inferior
    doc["gate"] = {"verdict": "failed", "reasons": ["lower_bound_not_above_margin"]}
    assert e4.validate_e4(doc) == []
    doc = _doc()
    doc["power"]["verdict"] = "underpowered"
    assert any("gate" in e for e in e4.validate_e4(doc))
    doc["gate"] = {"verdict": "underpowered", "reasons": ["underpowered_owner_decides"]}
    assert e4.validate_e4(doc) == []


def test_screening_has_null_power_and_no_gate_pass_or_style_recommendation() -> None:
    doc = _doc()
    doc["stage"] = "screening"
    doc["suite"] = "swe-bench-verified-mini"
    doc["power"] = None
    doc["gate"] = None
    assert e4.validate_e4(doc) == []
    doc["style_rule"] = "recommend_opt_in"
    assert any("style_rule" in e for e in e4.validate_e4(doc))
    doc = _doc()
    doc["stage"] = "screening"
    assert any("power" in e for e in e4.validate_e4(doc))
    doc = _doc()
    doc["power"] = None
    assert any("power" in e for e in e4.validate_e4(doc))
    doc = _doc()
    doc["gate"] = None
    assert any("gate" in e for e in e4.validate_e4(doc))


def test_arms_unique_and_core_arms_present() -> None:
    doc = _doc()
    doc["arms"].append({"name": "baseline", "kind": "baseline", "pin": None})
    assert any("duplicate" in e for e in e4.validate_e4(doc))
    doc = _doc()
    doc["arms"] = [doc["arms"][0]]
    assert e4.validate_e4(doc)


def test_validate_rejects_non_object_and_load_raises(tmp_path: Path) -> None:
    assert e4.validate_e4([])  # type: ignore[arg-type]
    bad = tmp_path / "e4.json"
    bad.write_text(json.dumps({"source": "E1"}), encoding="utf-8")
    with pytest.raises(e4.E4Error, match="source"):
        e4.load_e4(bad)
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(e4.E4Error, match="JSON"):
        e4.load_e4(bad)


def test_validator_supports_only_its_subset() -> None:
    with pytest.raises(e4.E4Error, match="unsupported"):
        e4.check_schema({"type": "object", "oneOf": []})
    e4.check_schema(e4.load_schema())  # the shipped schema uses only the subset


def test_unknown_extra_fields_allowed_for_additive_changes() -> None:
    doc = _doc()
    doc["per_repo"] = {"django": {}}
    doc["results"][1]["label"] = "descriptive"
    assert e4.validate_e4(doc) == []
