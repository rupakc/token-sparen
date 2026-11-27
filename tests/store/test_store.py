from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
from pathlib import Path

import pytest

from token_sparen.store import jsonl, jsonstore, paths, session

KEY = "0123456789abcdef"


def _incr(d: dict) -> dict:
    d["n"] = d.get("n", 0) + 1
    return d


def worker(path: str, count: int) -> None:
    for _ in range(count):
        jsonstore.update(Path(path), _incr, {"n": 0})


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_home_honours_env(tmp_path: Path) -> None:
    assert paths.home({"TOKEN_SPAREN_HOME": str(tmp_path)}) == tmp_path


def test_project_dir_for(tmp_path: Path) -> None:
    assert paths.project_dir_for(tmp_path, "a" * 16) == tmp_path / "projects" / ("a" * 16)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("git@github.com:a/b.git", "https://github.com/a/b"),
        ("https://user@GitHub.com/a/b.git/", "ssh://git@github.com/a/b"),
    ],
)
def test_normalise_remote(a: str, b: str) -> None:
    assert paths.normalise_remote(a) == paths.normalise_remote(b) == "github.com/a/b"


def test_project_key_same_for_ssh_and_https(tmp_path: Path) -> None:
    keys = []
    for i, url in enumerate(["git@github.com:a/b.git", "https://github.com/a/b"]):
        d = tmp_path / f"r{i}"
        d.mkdir()
        _git(d, "init", "-q")
        _git(d, "remote", "add", "origin", url)
        keys.append(paths.project_key(d))
    assert keys[0] == keys[1]
    assert len(keys[0]) == 16
    int(keys[0], 16)


def test_project_key_fallback_realpath(tmp_path: Path) -> None:
    key = paths.project_key(tmp_path)
    assert len(key) == 16
    assert key == paths.project_key(tmp_path / ".")
    assert str(tmp_path) not in key


def test_project_key_git_without_origin_falls_back(tmp_path: Path) -> None:
    plain = paths.project_key(tmp_path)
    _git(tmp_path, "init", "-q")
    assert paths.project_key(tmp_path) == plain


def test_project_key_git_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(paths.subprocess, "run", boom)
    assert len(paths.project_key(tmp_path)) == 16


def test_parallel_writers_no_lost_update(tmp_path: Path) -> None:
    target = tmp_path / "counter.json"
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=worker, args=(str(target), 50)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    assert jsonstore.read(target, {})["n"] == 400


def test_update_creates_with_default(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "x.json"
    out = jsonstore.update(p, _incr, {"n": 10})
    assert out == {"n": 11}
    assert json.loads(p.read_text(encoding="utf-8")) == {"n": 11}
    assert not [f for f in p.parent.iterdir() if ".tmp-" in f.name]


def test_read_missing_returns_default(tmp_path: Path) -> None:
    assert jsonstore.read(tmp_path / "nope.json", {"a": 1}) == {"a": 1}


def test_corrupt_file_recovers_to_default(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    out = jsonstore.update(p, _incr, {"n": 5}, env=env)
    assert out == {"n": 6}
    assert json.loads(p.read_text(encoding="utf-8")) == {"n": 6}
    assert len(list(tmp_path.glob("s.json.corrupt-*"))) == 1
    assert (tmp_path / "home" / "logs" / "errors.jsonl").exists()


def test_read_never_renames(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    assert jsonstore.read(p, {"ok": True}, env=env) == {"ok": True}
    assert p.read_text(encoding="utf-8") == "{not json"
    assert not list(tmp_path.glob("*.corrupt-*"))
    assert (tmp_path / "home" / "logs" / "errors.jsonl").exists()


def test_quarantines_in_tight_loop_do_not_collide(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    for _ in range(3):
        p.write_text("garbage")
        jsonstore.update(p, _incr, {})
    assert len(list(tmp_path.glob("s.json.corrupt-*"))) == 3


def test_quarantine_name_collision_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonstore.time, "time_ns", lambda: 1)
    monkeypatch.setattr(jsonstore.os, "getpid", lambda: 2)
    tokens = iter(["aa", "aa", "bb"])
    monkeypatch.setattr(jsonstore.secrets, "token_hex", lambda n: next(tokens))
    p = tmp_path / "s.json"
    (tmp_path / "s.json.corrupt-1-2-aa").write_text("old")
    p.write_text("garbage")
    jsonstore._quarantine(p, "x", {"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    assert (tmp_path / "s.json.corrupt-1-2-aa").read_text() == "old"
    assert (tmp_path / "s.json.corrupt-1-2-bb").read_text() == "garbage"


def test_lock_files_are_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_S", 0.2)
    with jsonstore.locked(tmp_path / "a.jsonl"), jsonstore.locked(tmp_path / "a.json"):
        pass


def test_project_dir_for_rejects_bad_key(tmp_path: Path) -> None:
    for bad in ("../x", "abc", "G" * 16, "A" * 16, ""):
        with pytest.raises(ValueError):
            paths.project_dir_for(tmp_path, bad)


def test_jsonl_rotation_failure_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "log.jsonl"
    jsonl.append(p, {"pad": "x" * 50}, 60)

    def deny(src: object, dst: object) -> None:
        raise PermissionError

    monkeypatch.setattr(jsonstore.os, "replace", deny)
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    jsonl.append(p, {"pad": "y" * 50}, 60, env=env)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2
    assert (tmp_path / "home" / "logs" / "errors.jsonl").exists()


def test_non_dict_json_is_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    p.write_text("[1]", encoding="utf-8")
    assert jsonstore.read(p, {}) == {}


def test_update_on_corrupt_uses_default(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("garbage", encoding="utf-8")
    assert jsonstore.update(p, _incr, {"n": 0}) == {"n": 1}


def test_lock_timeout_raises_store_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "b.json"
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_S", 0.2)
    with jsonstore.locked(p):
        with pytest.raises(jsonstore.StoreBusy):
            jsonstore.update(p, _incr, {})


def test_replace_retries_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace
    calls = {"n": 0}

    def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError
        real(src, dst)

    monkeypatch.setattr(jsonstore.os, "replace", flaky)
    assert jsonstore.update(tmp_path / "r.json", _incr, {}) == {"n": 1}
    assert calls["n"] == 3


def test_replace_gives_up_and_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always(src: object, dst: object) -> None:
        raise PermissionError

    monkeypatch.setattr(jsonstore.os, "replace", always)
    with pytest.raises(PermissionError):
        jsonstore.update(tmp_path / "r.json", _incr, {})
    assert not [f for f in tmp_path.iterdir() if ".tmp-" in f.name]


def test_jsonl_rotation(tmp_path: Path) -> None:
    p = tmp_path / "log.jsonl"
    for i in range(40):
        jsonl.append(p, {"i": i, "pad": "x" * 20}, 500)
    rotated = tmp_path / "log.jsonl.1"
    assert rotated.exists()
    assert p.stat().st_size < 500
    assert json.loads(p.read_text(encoding="utf-8").splitlines()[-1])["i"] == 39


def test_jsonl_append_creates_dirs(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b.jsonl"
    jsonl.append(p, {"k": 1}, 1000)
    assert json.loads(p.read_text(encoding="utf-8")) == {"k": 1}


def test_session_path_sanitises_traversal(tmp_path: Path) -> None:
    sp = session.session_path(tmp_path, KEY, "../../etc/passwd")
    assert sp.parent == tmp_path / "projects" / KEY / "sessions"
    assert sp.name == ".._.._etc_passwd.json"
    assert session.session_path(tmp_path, KEY, "ok-1.2_x").name == "ok-1.2_x.json"
    assert session.session_path(tmp_path, KEY, "..").parent == sp.parent
    assert session.session_path(tmp_path, KEY, "..").name != "...json"


def test_session_roundtrip(tmp_path: Path) -> None:
    sp = session.session_path(tmp_path, KEY, "s1")
    assert session.read_session(sp) == {}
    assert session.update_session(sp, _incr) == {"n": 1}
    assert session.read_session(sp) == {"n": 1}


def test_lock_poll_backs_off_exponentially(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    fails = {"n": 6}

    def try_lock(fh: object) -> bool:
        fails["n"] -= 1
        return fails["n"] < 0

    monkeypatch.setattr(jsonstore, "_try_lock", try_lock)
    monkeypatch.setattr(jsonstore, "_jitter", lambda: 1.0)
    monkeypatch.setattr(jsonstore.time, "sleep", sleeps.append)
    with jsonstore.locked(tmp_path / "c.json"):
        pass
    assert sleeps == pytest.approx([0.001, 0.002, 0.004, 0.005, 0.005, 0.005])


def test_lock_jitter_in_unit_interval() -> None:
    assert all(0.0 < jsonstore._jitter() <= 1.0 for _ in range(200))


def test_non_utf8_file_is_corrupt_not_a_crash(tmp_path: Path) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    p = tmp_path / "s.json"
    p.write_bytes(b'{"n": "\xff\xfe"}')
    assert jsonstore.read(p, {"n": 0}, env) == {"n": 0}
    assert jsonstore.update(p, _incr, {"n": 0}, env) == {"n": 1}
    assert json.loads(p.read_text(encoding="utf-8")) == {"n": 1}
    assert any(q.name.startswith("s.json.corrupt-") for q in tmp_path.iterdir())


def test_default_is_never_mutated(tmp_path: Path) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    default: dict = {"arms": {}}

    def add(d: dict) -> dict:
        d["arms"]["x"] = 1
        return d

    jsonstore.update(tmp_path / "a.json", add, default, env)
    assert default == {"arms": {}}
    assert jsonstore.read(tmp_path / "b.json", default, env) == {"arms": {}}
    jsonstore.read(tmp_path / "b.json", default, env)["arms"]["y"] = 2
    assert default == {"arms": {}}
