"""`e4.json`: the published E4 evidence schema and its stdlib validator (M5 Task 3, ruling M6-B1).

`e4.schema.json` (JSON Schema 2020-12) describes the document; `validate_e4` checks it with a
small stdlib interpreter of the keyword subset the schema uses (`SUPPORTED_KEYWORDS`) plus the
cross-field rules no plain schema keyword expresses (see the schema's `description`). M6 reads
`docs/evidence/e4.json` through `load_e4`.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("e4.schema.json")
SOURCE = "E4"
MARGIN_PP = -5
CONFIRMATORY_METRIC = "resolved_diff_pp"
TREATMENT_ARM = "token-sparen"
BASELINE_ARM = "baseline"

ANNOTATIONS = frozenset({"$schema", "$id", "$defs", "$comment", "title", "description"})
SUPPORTED_KEYWORDS = ANNOTATIONS | {
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "pattern",
    "format",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class E4Error(ValueError):
    """An `e4.json` document (or the schema itself) is invalid."""


def load_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return schema


def check_schema(schema: Mapping[str, Any]) -> None:
    """Raise `E4Error` if ``schema`` uses a keyword outside the supported subset."""

    def walk(node: Any, where: str) -> None:
        if not isinstance(node, Mapping):
            return
        unknown = sorted(set(node) - SUPPORTED_KEYWORDS)
        if unknown:
            raise E4Error(f"unsupported schema keyword(s) at {where or '$'}: {unknown}")
        for key in ("properties", "$defs"):
            for name, sub in node.get(key, {}).items():
                walk(sub, f"{where}/{key}/{name}")
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), Mapping):
                walk(node[key], f"{where}/{key}")

    walk(schema, "")


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path or '$'}[{key}]"
    return f"{path}.{key}" if path else key


def _is_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if name == "number":
        return (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, Mapping)
    raise E4Error(f"unsupported schema type {name!r}")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    return bool(a == b)


class _Validator:
    def __init__(self, root: Mapping[str, Any]) -> None:
        self.root = root
        self.errors: list[str] = []

    def _resolve(self, ref: str) -> Mapping[str, Any]:
        if not ref.startswith("#/"):
            raise E4Error(f"unsupported $ref {ref!r} (local refs only)")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node  # type: ignore[no-any-return]

    def _err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path or '$'}: {msg}")

    def check(self, value: Any, schema: Mapping[str, Any], path: str) -> None:
        if "$ref" in schema:
            self.check(value, self._resolve(schema["$ref"]), path)
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(value, n) for n in names):
                self._err(path, f"expected {' or '.join(names)}, got {type(value).__name__}")
                return
        if "const" in schema and not _same(value, schema["const"]):
            self._err(path, f"must be {schema['const']!r}, got {value!r}")
        if "enum" in schema and not any(_same(value, v) for v in schema["enum"]):
            self._err(path, f"must be one of {schema['enum']!r}, got {value!r}")
        if isinstance(value, str):
            self._check_string(value, schema, path)
        if _is_type(value, "number"):
            if "minimum" in schema and value < schema["minimum"]:
                self._err(path, f"must be >= {schema['minimum']}, got {value!r}")
            if "maximum" in schema and value > schema["maximum"]:
                self._err(path, f"must be <= {schema['maximum']}, got {value!r}")
        if isinstance(value, list):
            self._check_array(value, schema, path)
        if isinstance(value, Mapping):
            self._check_object(value, schema, path)

    def _check_string(self, value: str, schema: Mapping[str, Any], path: str) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            self._err(path, f"must have at least {schema['minLength']} character(s)")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            self._err(path, f"{value!r} does not match {schema['pattern']!r}")
        if schema.get("format") == "date" and _parse_date(value) is None:
            self._err(path, f"{value!r} is not an ISO date (YYYY-MM-DD)")

    def _check_array(self, value: list[Any], schema: Mapping[str, Any], path: str) -> None:
        if "minItems" in schema and len(value) < schema["minItems"]:
            self._err(path, f"must have at least {schema['minItems']} item(s), got {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            self._err(path, f"must have at most {schema['maxItems']} item(s), got {len(value)}")
        if isinstance(schema.get("items"), Mapping):
            for i, item in enumerate(value):
                self.check(item, schema["items"], _join(path, i))

    def _check_object(self, value: Mapping[str, Any], schema: Mapping[str, Any], path: str) -> None:
        for key in schema.get("required", ()):
            if key not in value:
                self._err(path, f"missing required key {key!r}")
        props: Mapping[str, Any] = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                self.check(value[key], sub, _join(path, key))
        extra = schema.get("additionalProperties", True)
        for key in value:
            if key in props:
                continue
            if extra is False:
                self._err(path, f"unexpected key {key!r}")
            elif isinstance(extra, Mapping):
                self.check(value[key], extra, _join(path, key))


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not _DATE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _rules(doc: Mapping[str, Any]) -> list[str]:
    """Cross-field rules of the published interface (documented in the schema description)."""
    errors: list[str] = []
    date = doc.get("date")
    if isinstance(date, Mapping):
        start, end = _parse_date(date.get("from")), _parse_date(date.get("to"))
        if start and end and start > end:
            errors.append("date: 'from' is after 'to'")

    arms = doc.get("arms")
    names: list[str] = []
    if isinstance(arms, list):
        names = [
            a["name"] for a in arms if isinstance(a, Mapping) and isinstance(a.get("name"), str)
        ]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            errors.append(f"arms: duplicate arm name(s) {dupes}")
        for core in (BASELINE_ARM, TREATMENT_ARM):
            if core not in names:
                errors.append(f"arms: missing core arm {core!r}")

    confirmatory: list[Mapping[str, Any]] = []
    results = doc.get("results")
    for i, row in enumerate(results if isinstance(results, list) else []):
        if not isinstance(row, Mapping):
            continue
        where = f"results[{i}]"
        for field in ("arm", "baseline"):
            name = row.get(field)
            if isinstance(name, str) and names and name not in names:
                errors.append(f"{where}.{field}: {name!r} is not a listed arm")
        ci = row.get("ci95")
        if (
            isinstance(ci, list)
            and len(ci) == 2
            and all(_is_type(x, "number") for x in ci)
            and ci[0] > ci[1]
        ):
            errors.append(f"{where}.ci95: lower bound {ci[0]} above upper bound {ci[1]}")
        if row.get("metric") == CONFIRMATORY_METRIC and row.get("confirmatory") is True:
            confirmatory.append(row)
            errors += _confirmatory_rules(row, where)
        elif row.get("confirmatory") is True:
            errors.append(
                f"{where}.confirmatory: only {TREATMENT_ARM} vs {BASELINE_ARM} "
                f"{CONFIRMATORY_METRIC} is confirmatory; label this result descriptive (false)"
            )
    if isinstance(results, list) and len(confirmatory) != 1:
        errors.append(
            f"results: exactly one confirmatory {CONFIRMATORY_METRIC} result required, "
            f"found {len(confirmatory)}"
        )
    errors += _stage_rules(doc, confirmatory[0] if len(confirmatory) == 1 else None)
    return errors


def _confirmatory_rules(row: Mapping[str, Any], where: str) -> list[str]:
    errors: list[str] = []
    if row.get("arm") != TREATMENT_ARM:
        errors.append(f"{where}.arm: confirmatory arm must be {TREATMENT_ARM!r}")
    if row.get("baseline") != BASELINE_ARM:
        errors.append(f"{where}.baseline: confirmatory baseline must be {BASELINE_ARM!r}")
    margin = row.get("margin_pp")
    if not _is_type(margin, "number") or margin != MARGIN_PP:
        errors.append(f"{where}.margin_pp: must be {MARGIN_PP}, got {margin!r}")
    non_inferior = row.get("non_inferior")
    ci = row.get("ci95")
    if not isinstance(non_inferior, bool):
        errors.append(f"{where}.non_inferior: required boolean")
    elif isinstance(ci, list) and len(ci) == 2 and _is_type(ci[0], "number"):
        if non_inferior != (ci[0] > MARGIN_PP):
            errors.append(
                f"{where}.non_inferior: {non_inferior} contradicts ci95 lower bound {ci[0]} "
                f"vs margin {MARGIN_PP}"
            )
    return errors


def _stage_rules(doc: Mapping[str, Any], row: Mapping[str, Any] | None) -> list[str]:
    errors: list[str] = []
    stage, power, gate = doc.get("stage"), doc.get("power"), doc.get("gate")
    verdict = gate.get("verdict") if isinstance(gate, Mapping) else None
    power_verdict = power.get("verdict") if isinstance(power, Mapping) else None
    if stage == "screening":
        if power is not None:
            errors.append("power: must be null in screening")
        if verdict == "passed":
            errors.append("gate: screening never passes the release gate")
        if doc.get("style_rule") == "recommend_opt_in":
            errors.append("style_rule: recommend_opt_in needs confirmation evidence")
    elif stage == "confirmation":
        if power is None and "power" in doc:
            errors.append("power: required (not null) in confirmation")
        if gate is None and "gate" in doc:
            errors.append("gate: required (not null) in confirmation")
    non_inferior = row.get("non_inferior") if row is not None else None
    if verdict == "passed" and (non_inferior is not True or power_verdict != "powered"):
        errors.append("gate: 'passed' needs non_inferior true and power verdict 'powered'")
    if verdict == "underpowered" and power_verdict != "underpowered":
        errors.append("gate: 'underpowered' needs power verdict 'underpowered'")
    return errors


def validate_e4(doc: Mapping[str, Any]) -> list[str]:
    """Return every problem with ``doc`` (empty list = valid `e4.json`)."""
    if not isinstance(doc, Mapping):
        return [f"$: expected object, got {type(doc).__name__}"]
    schema = load_schema()
    check_schema(schema)
    validator = _Validator(schema)
    validator.check(doc, schema, "")
    return validator.errors + _rules(doc)


def load_e4(path: Path) -> dict[str, Any]:
    """Read and validate an `e4.json`; raise `E4Error` naming every problem."""
    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise E4Error(f"{path}: not valid JSON ({exc})") from exc
    problems = validate_e4(doc)
    if problems:
        raise E4Error(f"{path}: invalid e4.json: " + "; ".join(problems))
    return doc  # type: ignore[no-any-return]


__all__: Sequence[str] = (
    "SCHEMA_FILE",
    "SCHEMA_VERSION",
    "E4Error",
    "check_schema",
    "load_e4",
    "load_schema",
    "validate_e4",
)
