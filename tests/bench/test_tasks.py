"""Task pool, streams and the seeded extension (M5 Task 4). Offline: no network, no Docker."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from collections import Counter
from email.message import Message
from pathlib import Path

import pytest

from bench import tasks as tk
from bench.data import fetch_pool as fp
from bench.tasks import Task


def _t(iid: str, repo: str, created: str, *, mini: bool = False) -> Task:
    return Task(iid, repo, "0" * 40, created, tk.arm64_image(iid), mini)


def test_arm64_image_name() -> None:
    assert (
        tk.arm64_image("django__django-12774")
        == "swebench/sweb.eval.arm64.django_1776_django-12774:latest"
    )
    assert tk.arm64_image("sphinx-doc__sphinx-8721").startswith(
        "swebench/sweb.eval.arm64.sphinx-doc_1776_sphinx-8721"
    )


def test_streams_in_commit_order() -> None:
    a1 = _t("r__a-2", "r/a", "2020-01-02T00:00:00Z")
    a0 = _t("r__a-9", "r/a", "2019-05-01T00:00:00Z")
    a_tie = _t("r__a-1", "r/a", "2020-01-02T00:00:00Z")
    b0 = _t("r__b-1", "r/b", "2018-01-01T00:00:00Z")
    got = tk.streams([a1, b0, a0, a_tie])
    assert list(got) == ["r/a", "r/b"]
    assert [t.instance_id for t in got["r/a"]] == ["r__a-9", "r__a-1", "r__a-2"]
    assert got["r/b"] == [b0]


def test_mini_is_two_streams_of_25() -> None:
    """Mini = 25 django + 25 sphinx tasks: pooled ones (arm64 image) plus the recorded ones
    without an image; the pooled Mini tasks form exactly two streams."""
    header = tk.read_pool_header()
    pool = tk.load_pool()
    mini = [t for t in pool if t.mini]
    by_repo = tk.streams(mini)
    assert set(by_repo) == {"django/django", "sphinx-doc/sphinx"}
    missing = Counter(iid.split("__")[0] for iid in header["mini_without_arm64"])
    for repo, group in by_repo.items():
        assert len(group) + missing[repo.split("/")[0]] == 25
    assert len(mini) + len(header["mini_without_arm64"]) == 50
    assert not set(header["mini_without_arm64"]) & {t.instance_id for t in pool}


def test_pool_file_under_256k() -> None:
    assert tk.POOL_FILE.stat().st_size < 256 * 1024
    header = tk.read_pool_header()
    assert len(header["verified_revision"]) == 40 and len(header["mini_revision"]) == 40
    text = tk.POOL_FILE.read_text(encoding="utf-8")
    for forbidden in ("problem_statement", "patch", "hints_text", "FAIL_TO_PASS"):
        assert forbidden not in text
    pool = tk.load_pool()
    assert len(pool) > 50
    assert all(t.image == tk.arm64_image(t.instance_id) for t in pool)


def test_extend_is_seeded_and_stratified() -> None:
    pool = [
        *[_t(f"m__one-{i}", "m/one", f"2020-01-{i + 1:02d}", mini=i < 2) for i in range(6)],
        *[_t(f"m__two-{i}", "m/two", f"2020-02-{i + 1:02d}", mini=i < 2) for i in range(6)],
        *[_t(f"o__x-{i}", "o/x", f"2020-03-{i + 1:02d}") for i in range(6)],
    ]
    have = [t for t in pool if t.mini]
    first = tk.extend(pool, have, 6, seed=7)
    assert first == tk.extend(pool, have, 6, seed=7)
    assert first != tk.extend(pool, have, 6, seed=8)
    ids = {t.instance_id for t in first}
    assert not ids & {t.instance_id for t in have}
    assert len(ids) == 6
    # Mini repositories first, balanced between them; no other repo before they run dry
    assert Counter(t.repo for t in first) == {"m/one": 3, "m/two": 3}
    more = tk.extend(pool, have, 10, seed=7)
    assert Counter(t.repo for t in more) == {"m/one": 4, "m/two": 4, "o/x": 2}
    assert more[:6] == first  # a larger draw extends the smaller one
    assert len(tk.extend(pool, have, 100, seed=7)) == len(pool) - len(have)
    with pytest.raises(ValueError):
        tk.extend(pool, have, -1, seed=7)


def test_extend_repo_draw_independent_of_other_repos() -> None:
    base = [_t(f"m__one-{i}", "m/one", "2020", mini=True) for i in range(8)]
    other = [_t(f"z__z-{i}", "z/z", "2020") for i in range(5)]
    alone = [t for t in tk.extend(base, [], 3, seed=1)]
    with_other = [t for t in tk.extend([*base, *other], [], 3, seed=1)]
    assert alone == with_other


def test_load_pool_rejects_bad_rows(tmp_path: Path) -> None:
    good = {"verified_revision": "a" * 40, "mini_revision": "b" * 40, "fetched": "2026-11-27"}
    row = {
        "instance_id": "x__y-1",
        "repo": "x/y",
        "base_commit": "c" * 40,
        "created_at": "2020",
        "image": tk.arm64_image("x__y-1"),
        "mini": False,
    }
    cases = {
        "header": [{"fetched": "x"}, row],
        "missing": [good, {k: v for k, v in row.items() if k != "repo"}],
        "image": [good, {**row, "image": "other:latest"}],
        "mini": [good, {**row, "mini": "no"}],
        "dup": [good, row, row],
    }
    for name, lines in cases.items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        with pytest.raises(tk.PoolError):
            tk.load_pool(path)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(tk.PoolError):
        tk.load_pool(bad)
    with pytest.raises(tk.PoolError):
        tk.load_pool(tmp_path / "absent.jsonl")


def test_fake_tasks_two_streams_of_two(tmp_path: Path) -> None:
    by_repo = tk.streams(tk.FAKE_TASKS)
    assert [len(v) for v in by_repo.values()] == [2, 2]
    assert {t.base_commit for t in tk.FAKE_TASKS} == {tk.FAKE_BASE_COMMIT}
    repo = tmp_path / "fake"
    assert tk.make_fake_repo(repo) == tk.FAKE_BASE_COMMIT  # deterministic commit id
    assert subprocess.run([sys.executable, "test_calc.py"], cwd=repo, check=False).returncode
    subprocess.run(
        ["git", "-C", str(repo), "apply", str(tk.FAKE_DIR / "fix.patch")],
        check=True,
        capture_output=True,
    )
    assert subprocess.run([sys.executable, "test_calc.py"], cwd=repo, check=False).returncode == 0


def test_fake_agent_fixes_bug_and_writes_stream(tmp_path: Path) -> None:
    repo = tmp_path / "fake"
    tk.make_fake_repo(repo)
    config = tmp_path / "config"
    env = {"CLAUDE_CONFIG_DIR": str(config), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(tk.FAKE_DIR / "agent.py"), "-p", "fix", "--model", "m1"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [e["type"] for e in events] == ["system", "assistant", "result"]
    assert events[0]["model"] == "m1" and events[-1]["subtype"] == "success"
    assert list((config / "projects").rglob("*.jsonl"))
    assert subprocess.run([sys.executable, "test_calc.py"], cwd=repo, check=False).returncode == 0


def test_dataset_rows_roundtrip_and_hash_checked(tmp_path: Path) -> None:
    revision = tk.read_pool_header()["verified_revision"]
    rows = [{"instance_id": "a__b-1", "problem_statement": "p", "patch": "d"}]
    tk.write_dataset_rows(tmp_path, rows, revision)
    assert tk.load_dataset_rows(tmp_path) == {"a__b-1": rows[0]}
    if sys.platform != "win32":
        assert (tmp_path / tk.DATASET_FILE).stat().st_mode & 0o777 == 0o600
    data = tmp_path / tk.DATASET_FILE
    data.write_bytes(data.read_bytes() + b"\n")
    with pytest.raises(tk.PoolError, match="sha256"):
        tk.load_dataset_rows(tmp_path)
    tk.write_dataset_rows(tmp_path, rows, "f" * 40)
    with pytest.raises(tk.PoolError, match="revision"):
        tk.load_dataset_rows(tmp_path)
    with pytest.raises(tk.PoolError):
        tk.load_dataset_rows(tmp_path / "nowhere")


def test_build_pool_offline() -> None:
    verified = [
        {
            "instance_id": "a__a-1",
            "repo": "a/a",
            "base_commit": "x",
            "created_at": "1",
            "patch": "p",
        },
        {"instance_id": "a__a-2", "repo": "a/a", "base_commit": "y", "created_at": "2"},
        {"instance_id": "b__b-1", "repo": "b/b", "base_commit": "z", "created_at": "3"},
    ]
    images = {"sweb.eval.arm64.a_1776_a-1", "sweb.eval.arm64.b_1776_b-1"}
    rows = fp.build_pool(verified, ["a__a-1", "a__a-2"], images, header={"fetched": "d"})
    head, *body = rows
    assert head["mini_without_arm64"] == ["a__a-2"] and head["verified_without_arm64"] == 1
    assert [(r["instance_id"], r["mini"]) for r in body] == [("a__a-1", True), ("b__b-1", False)]
    assert "patch" not in body[0]


def test_fetch_helpers_with_fake_http() -> None:
    pages = {
        "hub": {"sha": "1" * 40},
        0: {"num_rows_total": 3, "rows": [{"row": {"i": 0}}, {"row": {"i": 1}}]},
        2: {"num_rows_total": 3, "rows": [{"row": {"i": 2}}]},
    }

    def fetch(url: str) -> object:
        if url.startswith(fp.HUB_API):
            return pages["hub"]
        return pages[int(url.split("offset=")[1].split("&")[0])]

    rev, rows = fp.pinned_rows("d/s", fetch)
    assert rev == "1" * 40 and [r["i"] for r in rows] == [0, 1, 2]

    def dfetch(url: str) -> object:
        assert url.endswith("/tags/latest")
        if "arm64.b_1776_b-1" in url:
            raise urllib.error.HTTPError(url, 404, "not found", Message(), None)
        if "arm64.c_1776_c-1" in url:
            raise urllib.error.HTTPError(url, 500, "boom", Message(), None)
        return {"name": "latest"}

    assert fp.arm64_repositories(["a__a-1", "b__b-1"], dfetch) == {"sweb.eval.arm64.a_1776_a-1"}
    with pytest.raises(urllib.error.HTTPError):
        fp.arm64_repositories(["c__c-1"], dfetch)
