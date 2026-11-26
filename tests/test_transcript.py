from pathlib import Path

import pytest

from token_sparen.estimate import Usage
from token_sparen.transcript import iter_assistant_usage, iter_tool_results, session_burn

FIX = Path(__file__).parent / "fixtures" / "transcripts"


def test_dedupe_split_records() -> None:
    got = list(iter_assistant_usage(FIX / "split.jsonl"))
    assert [g[0] for g in got] == ["msg_1", "msg_2"]
    assert got[0][1] == Usage(input=10, cache_read=100, cache_write=5, output=40)
    assert got[0][2] == "model-a"


def test_session_burn_sums_context() -> None:
    assert session_burn(FIX / "split.jsonl") == (10 + 100 + 5) + (20 + 200)


def test_sidechain_excluded_by_default() -> None:
    ids = [g[0] for g in iter_assistant_usage(FIX / "split.jsonl")]
    assert "msg_s" not in ids
    ids = [g[0] for g in iter_assistant_usage(FIX / "split.jsonl", include_sidechains=True)]
    assert "msg_s" in ids and "msg_syn" not in ids


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert list(iter_assistant_usage(tmp_path / "nope.jsonl")) == []
    assert session_burn(tmp_path / "nope.jsonl") == 0


def test_tool_results() -> None:
    recs = list(iter_tool_results(FIX / "tools.jsonl"))
    assert [r.tool_use_id for r in recs] == ["toolu_1", "toolu_2", "toolu_3", "toolu_unknown"]
    assert recs[0].tool_name == "Bash" and recs[0].input == {"command": "ls"}
    assert recs[0].text == "file1\nfile2" and recs[0].turn_index == 1
    assert recs[1].text == "alpha\nbeta" and recs[1].tool_name == "Read"
    assert recs[2].turn_index == 2 and recs[2].text == "hit"
    assert recs[3].tool_name == "" and recs[3].input == {}


def _jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _use(tid: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "id": f"m_{tid}",
            "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "x"}}],
        },
    }


def _result(tid: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "out"}]},
    }


def test_turns_skip_sidechain_and_meta_prompts(tmp_path: Path) -> None:
    path = _jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": "real prompt"}},
            {"type": "user", "isSidechain": True, "message": {"content": "subagent brief"}},
            {"type": "user", "isMeta": True, "message": {"content": "caveat: local command"}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
            _use("t1"),
            _result("t1"),
            {"type": "user", "message": {"content": [{"type": "text", "text": "second"}]}},
            _use("t2"),
            _result("t2"),
        ],
    )
    assert [r.turn_index for r in iter_tool_results(path)] == [1, 2]


def test_tool_results_carry_error_flag_and_raw(tmp_path: Path) -> None:
    recs = list(iter_tool_results(FIX / "tools.jsonl"))
    assert recs[1].is_error and not recs[0].is_error
    assert recs[0].raw is None  # one raw response shared by two results: ambiguous
    single = {**_result("t1"), "toolUseResult": {"stdout": "out"}}
    path = _jsonl(tmp_path / "s.jsonl", [_use("t1"), single])
    assert [r.raw for r in iter_tool_results(path)] == [{"stdout": "out"}]


def test_deeply_nested_line_is_skipped(tmp_path: Path) -> None:
    import json

    deep = "[" * 100_000 + "]" * 100_000
    lines = [json.dumps(_use("t1")), deep, json.dumps(_result("t1"))]
    path = tmp_path / "deep.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert [r.tool_use_id for r in iter_tool_results(path)] == ["t1"]


def test_read_error_mid_file_ends_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json

    mp = monkeypatch
    path = tmp_path / "eio.jsonl"
    path.write_text("", encoding="utf-8")

    class Flaky(io.StringIO):
        def __iter__(self) -> "Flaky":
            return self

        def __next__(self) -> str:
            line = self.readline()
            if not line:
                raise OSError(5, "Input/output error")
            return line

    body = json.dumps(_use("t1")) + "\n" + json.dumps(_result("t1")) + "\n"
    real_open = Path.open

    def fake_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == path:
            return Flaky(body)
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    mp.setattr(Path, "open", fake_open)
    assert [r.tool_use_id for r in iter_tool_results(path)] == ["t1"]
    assert list(iter_assistant_usage(path)) == []
