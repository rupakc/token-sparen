"""Release workflow lint (M6 Task 10): triggers, permissions, environments, pinned actions, job
order and the dry-run boundary, read from a parse of ``.github/workflows/release.yml``.

The parser is a small stdlib-only reader for the YAML subset the workflows use (block mappings
and sequences, plain/quoted scalars, ``{}``/``[...]`` flow values, ``|``/``>`` block scalars,
comments); no YAML dependency is added to the dev extra.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
PUBLISHING = ("draft-release", "provenance", "push-tree", "publish-pypi", "finalize")
PUSH_ONLY = ("verify-tag", *PUBLISHING)
SHA_PIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")

# --- a tiny YAML-subset parser ---------------------------------------------------------------

_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s"'#][^:#]*?)\s*:(?:\s+(?P<rest>.*))?$""")
_BLOCK = re.compile(r"^[|>][+-]?$")


def _strip_comment(text: str) -> str:
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"" and (i == 0 or text[i - 1] in " \t:[{,"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _scalar(text: str) -> Any:
    text = _strip_comment(text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if text in ("", "~", "null"):
        return None
    if text == "{}":
        return {}
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.i = 0

    def _peek(self) -> tuple[int, str] | None:
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                return len(raw) - len(raw.lstrip(" ")), stripped
            self.i += 1
        return None

    def node(self, parent: int) -> Any:
        nxt = self._peek()
        if nxt is None or nxt[0] <= parent:
            return None
        indent, content = nxt
        return self._seq(indent) if content.startswith("-") else self._map(indent)

    def _map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while (nxt := self._peek()) is not None and nxt[0] == indent:
            if nxt[1].startswith("- ") or nxt[1] == "-":
                break
            m = _KEY.match(nxt[1])
            assert m, f"line {self.i + 1}: cannot parse {nxt[1]!r}"
            key = m.group("key").strip().strip("'\"")
            assert key not in out, f"line {self.i + 1}: duplicate key {key!r}"
            self.i += 1
            out[key] = self._value(m.group("rest") or "", indent)
        return out

    def _seq(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while (nxt := self._peek()) is not None and nxt[0] == indent and nxt[1].startswith("-"):
            item = nxt[1][1:].strip()
            if not item:
                self.i += 1
                out.append(self.node(indent))
            elif _KEY.match(item) and not item.startswith(("'", '"')):
                # "- key: value" opens a mapping indented past the dash
                self.lines[self.i] = " " * (indent + 2) + item
                out.append(self._map(indent + 2))
            else:
                self.i += 1
                out.append(_scalar(item))
        return out

    def _value(self, rest: str, indent: int) -> Any:
        rest = _strip_comment(rest).strip()
        if _BLOCK.fullmatch(rest):
            return self._block(rest, indent)
        if rest:
            return _scalar(rest)
        nxt = self._peek()
        if nxt is not None and nxt[0] == indent and nxt[1].startswith("- "):
            return self._seq(indent)  # "key:" followed by a sequence at the same indent
        return self.node(indent)

    def _block(self, style: str, indent: int) -> str:
        body: list[str] = []
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= indent:
                break
            body.append(raw)
            self.i += 1
        while body and not body[-1].strip():
            body.pop()
        cut = min((len(b) - len(b.lstrip(" ")) for b in body if b.strip()), default=0)
        lines = [b[cut:] for b in body]
        text = "\n".join(lines) if style[0] == "|" else " ".join(x.strip() for x in lines)
        return text if style.endswith("-") else text + "\n"


def parse_yaml(text: str) -> Any:
    parser = _Parser(text)
    value = parser.node(-1)
    assert parser._peek() is None, f"unparsed input at line {parser.i + 1}"
    return value


def test_yaml_subset_parser() -> None:
    doc = parse_yaml(
        "a: 1  # note\n"
        "b:\n"
        "  - x\n"
        "  - k: 'v # not a comment'\n"
        "    n: true\n"
        "c: {}\n"
        "d: [p, q]\n"
        "e: |\n"
        "  line 1\n"
        "    line 2\n"
        "f: >-\n"
        "  folded\n"
        "  text\n"
        "g:\n"
        "- same-indent item\n"
        'h: "${{ x }}"\n'
    )
    assert doc == {
        "a": 1,
        "b": ["x", {"k": "v # not a comment", "n": True}],
        "c": {},
        "d": ["p", "q"],
        "e": "line 1\n  line 2\n",
        "f": "folded text",
        "g": ["same-indent item"],
        "h": "${{ x }}",
    }


def test_parser_reads_every_workflow() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = parse_yaml(path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict) and "jobs" in doc, path.name


# --- the release workflow --------------------------------------------------------------------


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    doc = parse_yaml(RELEASE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _jobs(wf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = wf["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _uses(job: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [s for s in job.get("steps", []) if str(s.get("uses", "")).startswith(prefix)]


def _github_filter(pattern: str) -> re.Pattern[str]:
    """GitHub's tag filter pattern as a regex (``*``, ``+``, ``?``, ``[...]``; else literal)."""
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            end = pattern.index("]", i)
            out += pattern[i : end + 1]
            i = end
        elif ch == "*":
            out += "[^/]*"
        elif ch in "+?":
            out += ch
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out)


def test_release_tag_filter_is_strict_semver(wf: dict[str, Any]) -> None:
    on = wf["on"]
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["workflow_dispatch"] is None  # no inputs: a dispatch is always a dry run
    assert set(on["push"]) == {"tags"}  # never a branch push
    (pattern,) = on["push"]["tags"]
    rx = _github_filter(pattern)
    for tag in ("v1.0.0", "v0.1.0", "v10.20.300"):
        assert rx.fullmatch(tag), tag
    for tag in ("v1.0.0-rc1", "v0.4.0-m4", "v0.5.0-screen1", "v1.0", "1.0.0", "v1.0.0.1", "vX.0.0"):
        assert not rx.fullmatch(tag), tag
    # verify-tag re-checks the exact regex (defence in depth beside the trigger filter)
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in _runs(_jobs(wf)["verify-tag"])


def test_permissions_least_privilege(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {}
    expected = {
        "gate": {"contents": "read"},
        "verify-tag": {"contents": "read"},
        "build": {"contents": "read", "actions": "read"},
        "draft-release": {"contents": "write"},
        "provenance": {"contents": "read", "id-token": "write", "attestations": "write"},
        "push-tree": {"contents": "write"},
        "publish-pypi": {"id-token": "write"},
        "finalize": {"contents": "write"},
    }
    jobs = _jobs(wf)
    assert set(jobs) == set(expected)
    for name, job in jobs.items():
        assert job.get("permissions") == expected[name], name


def test_job_order(wf: dict[str, Any]) -> None:
    jobs = _jobs(wf)
    assert {name: _needs(job) for name, job in jobs.items()} == {
        "gate": set(),
        "verify-tag": set(),
        "build": {"gate", "verify-tag"},
        "draft-release": {"build"},
        "provenance": {"draft-release"},
        "push-tree": {"build", "provenance"},
        "publish-pypi": {"build", "push-tree"},
        "finalize": {"build", "publish-pypi"},
    }


def test_dry_run_has_no_tag_checks_or_publishing(wf: dict[str, Any]) -> None:
    jobs = _jobs(wf)
    for name in PUSH_ONLY:
        assert jobs[name]["if"] == "github.event_name == 'push'", name
    assert "if" not in jobs["gate"]
    assert jobs["build"]["if"] == (
        "always() && needs.gate.result == 'success' && "
        "(github.event_name != 'push' || needs.verify-tag.result == 'success')"
    )
    steps = jobs["build"]["steps"]
    tag_check = [s for s in steps if "--mode tag" in str(s.get("run", ""))]
    dry_check = [s for s in steps if "--mode dry-run" in str(s.get("run", ""))]
    assert [s["if"] for s in tag_check] == ["github.event_name == 'push'"]
    assert [s["if"] for s in dry_check] == ["github.event_name != 'push'"]
    # build only uploads: no release, push, attestation or index upload outside push-only jobs
    for name in ("gate", "build"):
        text = _runs(jobs[name])
        for word in ("gh release", "git push", "attest", "pypi-publish", "twine upload"):
            assert word not in text, f"{name}: {word}"
        assert not _uses(jobs[name], "actions/attest-build-provenance")
        assert not _uses(jobs[name], "pypa/")


def test_environments_only_on_approval_gated_jobs(wf: dict[str, Any]) -> None:
    envs = {}
    for name, job in _jobs(wf).items():
        env = job.get("environment")
        if env is not None:
            envs[name] = env["name"] if isinstance(env, dict) else env
    assert envs == {"provenance": "release", "publish-pypi": "pypi"}


def test_provenance_only_after_release_approval(wf: dict[str, Any]) -> None:
    jobs = _jobs(wf)
    prov = jobs["provenance"]
    assert prov["environment"] == "release"
    assert _needs(prov) == {"draft-release"}
    (attest,) = _uses(prov, "actions/attest-build-provenance@")
    assert attest["with"]["subject-path"].split() == ["dist/*.whl", "dist/*.tar.gz"]
    for name, job in jobs.items():
        if name != "provenance":
            assert not _uses(job, "actions/attest-build-provenance"), name
            assert "attestations" not in job.get("permissions", {}), name


def test_publish_order_pypi_after_tree_push(wf: dict[str, Any]) -> None:
    jobs = _jobs(wf)

    def ancestors(name: str) -> set[str]:
        seen: set[str] = set()
        todo = list(_needs(jobs[name]))
        while todo:
            cur = todo.pop()
            if cur not in seen:
                seen.add(cur)
                todo.extend(_needs(jobs[cur]))
        return seen

    assert {"gate", "verify-tag", "build", "draft-release", "provenance"} <= ancestors("push-tree")
    assert "push-tree" in ancestors("publish-pypi")
    assert "publish-pypi" in ancestors("finalize")
    assert "publish-pypi" not in ancestors("push-tree")
    # only push-tree writes release/claude
    for name, job in jobs.items():
        text = _runs(job) + str(job.get("env", {}))
        if name != "push-tree":
            assert "release/claude" not in text and " push " not in text, name


def test_pypi_skip_existing_and_hash_check(wf: dict[str, Any]) -> None:
    jobs = _jobs(wf)
    pub = jobs["publish-pypi"]
    assert pub["environment"]["name"] == "pypi"
    assert pub["runs-on"] == "ubuntu-latest" and "container" not in pub
    (step,) = _uses(pub, "pypa/gh-action-pypi-publish@")
    assert step["with"] == {"packages-dir": "pypi-dist/", "skip-existing": True}
    run = _runs(pub)
    assert "cp release-dist/*.whl release-dist/*.tar.gz pypi-dist/" in run
    assert "-eq 2" in run and "sha256sum --check --strict SHA256SUMS" in run
    assert "password" not in str(step) and "secrets." not in str(pub)
    fin = _runs(jobs["finalize"])
    assert "https://pypi.org/pypi/token-sparen/$VERSION/json" in fin
    assert ".digests.sha256" in fin and "dist/SHA256SUMS" in fin
    assert 'gh release edit "$TAG" --draft=false "$latest"' in fin
    # `latest` only when this tag is the highest vX.Y.Z release (a patch on an older line is not)
    assert "sort -V | tail -n 1" in fin and "--exclude-drafts" in fin
    assert "then latest=--latest; else latest=--latest=false; fi" in fin
    assert fin.index("SHA256SUMS") < fin.index("--draft=false")


def test_draft_release_step_is_idempotent(wf: dict[str, Any]) -> None:
    run = _runs(_jobs(wf)["draft-release"])
    assert re.search(
        r'gh release view "\$TAG" >/dev/null 2>&1 \\\n\s*\|\| gh release create "\$TAG" --draft '
        r"--verify-tag .*--notes-file dist/NOTES.md",
        run,
    ), run
    assert 'gh release upload "$TAG" --clobber dist/*.whl dist/*.tar.gz dist/SHA256SUMS' in run
    assert "--draft=false" not in run  # only finalize publishes the release


def test_verify_tag_checks_signer_and_main_ancestry(wf: dict[str, Any]) -> None:
    job = _jobs(wf)["verify-tag"]
    run = _runs(job)
    env = job["env"]
    assert env["RELEASE_TAGGER"] == "${{ vars.RELEASE_TAGGER }}"
    assert env["RELEASE_SIGNING_KEY"] == "${{ vars.RELEASE_SIGNING_KEY }}"
    for needle in (
        'gh api "repos/$REPO/git/ref/tags/$TAG"',
        '\'.object.type\' <<<"$ref")" = "tag"',  # annotated tags only
        'gh api "repos/$REPO/git/tags/$tag_sha"',
        ".verification.verified == true",
        '.verification.reason == "valid"',
        "(.tagger.email | ascii_downcase) == ($tagger | ascii_downcase)",
        '"$commit" = "$GITHUB_SHA"',
        # the owner's key is the only trusted key; the tag must verify against it
        'scripts/release_trust.py trust --email "$RELEASE_TAGGER" --key-env RELEASE_SIGNING_KEY',
        'scripts/release_trust.py verify-tag "$TAG"',
        'git merge-base --is-ancestor "$commit" origin/main',
        '"chore(release): $TAG"',
    ):
        assert needle in run, needle
    assert run.index("release_trust.py trust") < run.index("release_trust.py verify-tag")
    (checkout,) = _uses(job, "actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    # build's tag-mode release_check verifies the gate-decision signature with the same key only
    build = _runs(_jobs(wf)["build"])
    assert build.index("release_trust.py trust") < build.index("--mode tag")


def test_tree_push_is_fast_forward_only(wf: dict[str, Any]) -> None:
    job = _jobs(wf)["push-tree"]
    assert job["env"]["BRANCH"] == "release/claude"
    run = _runs(job)
    pushes = [line for line in run.splitlines() if "git" in line and " push " in line]
    assert [p.strip() for p in pushes] == [
        'git -c "http.https://github.com/.extraheader=$auth" push origin '
        '"$commit:refs/heads/$BRANCH"'
    ]
    for bad in ("--force", " -f ", "+$commit", "+refs/heads/$BRANCH", "--delete", "--mirror"):
        assert bad not in pushes[0], bad
    assert "--force-with-lease" not in run and "push -f" not in run
    assert 'parent=(-p "$base")' in run  # parent = the current head (first release: none)
    assert 'git commit-tree "$tree_sha" "${parent[@]}"' in run
    assert 'msg="release: $TAG (plugin ${PLUGIN_HASH:0:12})"' in run
    # the head is re-read right before the push; a moved head refuses
    assert run.count('git ls-remote --heads origin "refs/heads/$BRANCH"') == 2
    assert '[ "$now" = "$base" ]' in run
    assert run.index('[ "$now" = "$base" ]') < run.index(" push origin ")
    assert '"$PLUGIN_HASH" ]' in run  # the pushed tree is the built tree


def test_gate_passes_only_the_denylist_secret(wf: dict[str, Any]) -> None:
    gate = _jobs(wf)["gate"]
    assert gate["uses"] == "./.github/workflows/ci.yml"
    assert gate["secrets"] == {"TOKEN_SPAREN_DENYLIST": "${{ secrets.TOKEN_SPAREN_DENYLIST }}"}
    text = RELEASE.read_text(encoding="utf-8")
    assert "secrets: inherit" not in text
    assert set(re.findall(r"secrets\.[A-Za-z_]+", text)) == {"secrets.TOKEN_SPAREN_DENYLIST"}


def test_build_checks_and_artifact(wf: dict[str, Any]) -> None:
    build = _jobs(wf)["build"]
    run = _runs(build)
    order = [
        'python scripts/release_check.py --mode tag --tag "$TAG"',
        "python -m build --no-isolation --outdir dist",
        "twine check --strict",
        "python scripts/check_wheel.py",
        "python scripts/build_claude_tree.py --out dist/tree",
        "python scripts/release_notes.py",
        "sha256sum -- *.whl *.tar.gz > SHA256SUMS",
        "python scripts/check_denylist.py --path dist --require-private-words",
    ]
    positions = [run.index(needle) for needle in order]
    assert positions == sorted(positions)
    assert "SOURCE_DATE_EPOCH=$(git log -1 --format=%ct HEAD)" in run
    # no pip cache and hash-pinned build tools in the job whose output is attested and published
    (setup,) = _uses(build, "actions/setup-python@")
    assert "cache" not in setup["with"]
    installs = [s["run"] for s in build["steps"] if "pip install" in str(s.get("run", ""))]
    assert installs == [
        "pip install --no-cache-dir --require-hashes --no-deps -r requirements/release.txt"
    ]
    pins = (ROOT / "requirements" / "release.txt").read_text(encoding="utf-8")
    reqs = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\$", pins, re.M)
    assert {"build", "twine", "setuptools"} <= {r.lower() for r in reqs}
    assert pins.count("--hash=sha256:") >= len(reqs) > 0
    (deny,) = [s for s in build["steps"] if "check_denylist" in str(s.get("run", ""))]
    assert deny["env"] == {"TOKEN_SPAREN_DENYLIST": "${{ secrets.TOKEN_SPAREN_DENYLIST }}"}
    (upload,) = _uses(build, "actions/upload-artifact@")
    assert upload["with"]["name"] == "dist" and upload["with"]["path"] == "dist"
    assert upload["with"]["retention-days"] == 30
    assert upload["with"]["include-hidden-files"] is True  # .claude-plugin/ in dist/tree


def test_actions_pinned_and_checkouts_drop_the_token(wf: dict[str, Any]) -> None:
    for name, job in _jobs(wf).items():
        if "uses" in job:
            assert job["uses"].startswith("./"), name  # only this repository's CI workflow
            continue
        for step in job["steps"]:
            ref = step.get("uses")
            if ref is None:
                continue
            assert SHA_PIN.fullmatch(ref), f"{name}: {ref} is not pinned to a commit SHA"
            if ref.startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


def test_e4_anchor_check_runs_in_the_release_job(wf: dict[str, Any]) -> None:
    """M5 Task 10 ruling N7: the authoritative anchor check runs in CI on a fresh fetch of the
    public branch with server receipts, before the build, for tag pushes only."""
    build = _jobs(wf)["build"]
    (step,) = [s for s in build["steps"] if "bench.anchors verify-release" in str(s.get("run", ""))]
    assert step["if"] == "github.event_name == 'push'"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "+refs/heads/bench-anchors:refs/remotes/origin/bench-anchors" in step["run"]
    run = _runs(build)
    assert run.index("verify-release") < run.index("python -m build --no-isolation")
    receipt = parse_yaml((WORKFLOWS / "anchor-receipt.yml").read_text(encoding="utf-8"))
    assert isinstance(receipt, dict)
    assert receipt["on"] == {"push": {"branches": ["bench-anchors"]}}
    assert receipt["permissions"] == {"contents": "read"}
    (job,) = receipt["jobs"].values()
    assert job["permissions"] == {"contents": "read"}
    assert all("uses" not in s for s in job["steps"])  # no checkout, no third-party action
