from pathlib import Path

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
