"""Regenerate the synthetic E2 corpus and needles.toml (deterministic, seeded).

Usage: python evals/e2/make_corpus.py [out_dir]   (default: this directory)
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
CORPUS = OUT / "corpus"
CORPUS.mkdir(parents=True, exist_ok=True)
CASES: list[dict] = []
WORDS = (
    "cache index worker queue session token render stream buffer config module handler "
    "request response schema record shard batch policy window cursor socket thread pool "
    "bucket filter mapper reducer loader parser encoder decoder router gateway adapter"
).split()


def sentence(r: random.Random, n: int = 12) -> str:
    return " ".join(r.choice(WORDS) for _ in range(n)).capitalize() + "."


def add(file, text, types, needles, must_inline=(), command=None):
    for n in needles:
        assert n in text, (file, n)
    for n in must_inline:
        assert n in needles, (file, n)
    (CORPUS / file).write_text(text, encoding="utf-8")
    CASES.append(
        dict(
            file=file,
            command=command,
            types=list(types),
            needles=list(needles),
            must_inline=list(must_inline),
        )
    )


# ------------------------------------------------------------------ json
def json_cases():
    r = random.Random(1)
    users = [
        {
            "id": f"u-{i:04d}",
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "plan": r.choice(["free", "pro", "team"]),
            "active": r.random() > 0.2,
            "profile": {"city": r.choice(["Oslo", "Lima", "Pune", "Kyiv"]), "age": 20 + i % 40},
        }
        for i in range(160)
    ]
    users[137]["email"] = "special.target@example.com"
    add(
        "json_api_users.json",
        json.dumps({"total": 160, "users": users}, indent=2),
        ["json"],
        ['"total": 160', "special.target@example.com", "u-0137"],
    )

    events = [
        {
            "ts": f"2026-03-0{1 + i % 9}T10:{i % 60:02d}:00Z",
            "kind": r.choice(["click", "view", "scroll"]),
            "target": f"#el-{i}",
            "meta": {"x": i, "y": i * 2},
        }
        for i in range(220)
    ]
    events[211]["meta"]["note"] = "failed: quota exceeded for tenant acme-42"
    add(
        "json_deep_error.json",
        json.dumps({"session": "s-9", "events": events}, indent=2),
        ["json"],
        ["failed: quota exceeded for tenant acme-42", '"session": "s-9"'],
        must_inline=["failed: quota exceeded for tenant acme-42"],
    )

    cfg = {
        f"service_{i}": {
            "host": f"10.0.{i}.5",
            "port": 8000 + i,
            "retries": 3,
            "tags": ["a", "b", "c"],
            "enabled": True,
        }
        for i in range(90)
    }
    cfg["api_version"] = "2026-11-01"
    add(
        "json_config_big.json",
        json.dumps(cfg, indent=2),
        ["json"],
        ['"api_version": "2026-11-01"', '"port": 8077'],
    )

    rows = [[i, i * 3, f"cell-{i}", {"ok": i % 7 != 0}] for i in range(300)]
    add(
        "json_matrix.json",
        json.dumps({"schema": "v3", "rows": rows}, indent=2),
        ["json"],
        ['"schema": "v3"', "cell-299"],
    )

    orders = [
        {
            "order_id": 90000 + i,
            "status": "shipped",
            "items": [{"sku": f"SKU-{i}-{j}", "qty": j + 1} for j in range(3)],
            "total": round(10 + i * 1.5, 2),
        }
        for i in range(110)
    ]
    orders[64]["status"] = "ERROR: payment gateway timeout"
    add(
        "json_orders.json",
        json.dumps({"orders": orders}),
        ["json"],
        ["ERROR: payment gateway timeout", "90064"],
        must_inline=["ERROR: payment gateway timeout"],
    )


# ------------------------------------------------------------------ diff
def diff_cases():
    for k, (nfiles, ctxn) in enumerate([(6, 40), (10, 25), (4, 80), (8, 30)]):
        r = random.Random(10 + k)
        parts = []
        needles, must = [], []
        for f in range(nfiles):
            path = f"src/app/module_{f}.py"
            body = [
                f"    value_{i} = compute_{i}(arg)  # {sentence(r, 4)}" for i in range(ctxn * 2 + 4)
            ]
            h = ["    pass_through = True"]
            old = f"    timeout = {10 + f}"
            new = f"    timeout = {45 + f}"
            lines = body[:ctxn] + [f"-{old}", f"+{new}"] + body[ctxn : ctxn * 2]
            lines = [(" " + ln if not ln.startswith(("-", "+")) else ln) for ln in lines]
            parts.append(
                f"diff --git a/{path} b/{path}\nindex 1a2b3c4..5d6e7f8 100644\n--- a/{path}\n+++ b/{path}\n"
                f"@@ -{10 + f},{ctxn * 2} +{10 + f},{ctxn * 2 + 1} @@ def handler_{f}():\n"
                + "\n".join(lines)
            )
            needles += [f"+{new}", f"diff --git a/{path} b/{path}"]
            must += [f"+{new}", f"-{old}"] if f == 0 else [f"+{new}"]
            needles.append(f"-{old}")
        add(
            f"diff_multi_{k}.diff",
            "\n".join(parts) + "\n",
            ["diff"],
            sorted(set(needles)),
            must_inline=sorted(set(must)),
        )


# ------------------------------------------------------------------ stacktrace
def stack_cases():
    r = random.Random(20)
    lib = "/usr/lib/python3.12/site-packages"

    def py_trace(n_lib_head, n_lib_mid, proj_frames, exc):
        fr = []
        for i in range(n_lib_head):
            fr.append(
                f'  File "{lib}/flask/app.py", line {1400 + i}, in dispatch_{i}\n    return self.call_{i}(req)'
            )
        for f in proj_frames[:1]:
            fr.append(f)
        for i in range(n_lib_mid):
            fr.append(
                f'  File "{lib}/sqlalchemy/engine/base.py", line {900 + i}, in _execute_{i}\n    return meth(self, args, kw)'
            )
        fr += proj_frames[1:]
        return "Traceback (most recent call last):\n" + "\n".join(fr) + "\n" + exc + "\n"

    proj = [
        '  File "/srv/app/service/handlers.py", line 57, in handle\n    result = repo.load(item)',
        '  File "/srv/app/java.py", line 88, in bridge\n    return native.call(payload)',
    ]
    add(
        "stack_python_java_py.txt",
        py_trace(30, 40, proj, "ValueError: invalid shard id 'x-17'"),
        ["stacktrace"],
        [
            "ValueError: invalid shard id 'x-17'",
            'File "/srv/app/java.py", line 88, in bridge',
            'File "/srv/app/service/handlers.py", line 57, in handle',
        ],
        must_inline=["ValueError: invalid shard id 'x-17'"],
    )
    # chained
    t1 = py_trace(25, 30, proj[:1], "KeyError: 'tenant'")
    t2 = py_trace(25, 30, proj[:1], "RuntimeError: lookup aborted")
    add(
        "stack_python_chained.txt",
        t1 + "\nThe above exception was the direct cause of the following exception:\n\n" + t2,
        ["stacktrace"],
        [
            "KeyError: 'tenant'",
            "RuntimeError: lookup aborted",
            'File "/srv/app/service/handlers.py", line 57, in handle',
        ],
        must_inline=["KeyError: 'tenant'", "RuntimeError: lookup aborted"],
    )
    # recursion
    rec = "\n".join(
        '  File "/srv/app/tree.py", line 12, in walk\n    return walk(node.child)'
        for _ in range(220)
    )
    add(
        "stack_python_recursion.txt",
        f"Traceback (most recent call last):\n{rec}\nRecursionError: maximum recursion depth exceeded\n",
        ["stacktrace"],
        [
            "RecursionError: maximum recursion depth exceeded",
            'File "/srv/app/tree.py", line 12, in walk',
        ],
        must_inline=["RecursionError: maximum recursion depth exceeded"],
    )

    # java
    jf = [
        f"\tat org.springframework.web.servlet.FrameworkServlet.process{i}(FrameworkServlet.java:{880 + i})"
        for i in range(45)
    ]
    jf += ["\tat com.acme.orders.OrderService.place(OrderService.java:142)"]
    jf += [
        f"\tat java.base/java.util.concurrent.ThreadPoolExecutor.runWorker{i}(ThreadPoolExecutor.java:{1100 + i})"
        for i in range(30)
    ]
    jf += ["\tat com.acme.orders.Main.run(Main.java:31)"]
    jt = (
        'Exception in thread "main" java.lang.IllegalStateException: inventory missing for sku 9913\n'
        + "\n".join(jf)
    )
    jt += (
        "\nCaused by: java.sql.SQLException: connection reset\n"
        + "\n".join(
            f"\tat org.postgresql.core.v3.QueryExecutorImpl.step{i}(QueryExecutorImpl.java:{300 + i})"
            for i in range(40)
        )
        + "\n"
    )
    add(
        "stack_java.txt",
        jt,
        ["stacktrace"],
        [
            "java.lang.IllegalStateException: inventory missing for sku 9913",
            "java.sql.SQLException: connection reset",
            "at com.acme.orders.OrderService.place(OrderService.java:142)",
            "at com.acme.orders.Main.run(Main.java:31)",
        ],
        must_inline=[
            "java.lang.IllegalStateException: inventory missing for sku 9913",
            "at com.acme.orders.OrderService.place(OrderService.java:142)",
        ],
    )

    # node
    nf = [
        f"    at Layer.handle [as handle_request] (/srv/web/node_modules/express/lib/router/layer.js:{95 + i}:5)"
        for i in range(150)
    ]
    nf.insert(3, "    at renderCart (/srv/web/src/cart.js:48:11)")
    add(
        "stack_node.txt",
        "TypeError: Cannot read properties of undefined (reading 'items')\n" + "\n".join(nf) + "\n",
        ["stacktrace"],
        [
            "TypeError: Cannot read properties of undefined (reading 'items')",
            "at renderCart (/srv/web/src/cart.js:48:11)",
        ],
        must_inline=["TypeError: Cannot read properties of undefined (reading 'items')"],
    )
    # go panic
    gf = "".join(
        f"main.worker{i}(0xc000{i:04x})\n\t/srv/go/worker.go:{20 + i} +0x{i * 16:x}\n"
        for i in range(200)
    )
    add(
        "stack_go.txt",
        "panic: runtime error: index out of range [5] with length 3\n\ngoroutine 1 [running]:\n"
        + gf,
        ["stacktrace"],
        ["panic: runtime error: index out of range [5] with length 3", "goroutine 1 [running]:"],
        must_inline=["panic: runtime error: index out of range [5] with length 3"],
    )


# ------------------------------------------------------------------ test_report
def pytest_report(seed, n_files, fails, verbose, header=True):
    r = random.Random(seed)
    out = []
    if header:
        out += [
            "============================= test session starts ==============================",
            "platform linux -- Python 3.12.1, pytest-8.2.0, pluggy-1.5.0",
            "rootdir: /srv/app",
            f"collected {n_files * 25} items",
            "",
        ]
    failing = []
    for f in range(n_files):
        name = f"tests/test_unit_{f}.py"
        if verbose:
            for t in range(25):
                tid = f"{name}::test_case_{t}"
                bad = any(tid == x[0] for x in fails)
                out.append(
                    f"{tid} {'FAILED' if bad else 'PASSED'} [{(f * 25 + t) * 100 // (n_files * 25):3d}%]"
                )
        else:
            out.append(f"{name} " + "." * 25 + f" [{(f + 1) * 100 // n_files:3d}%]")
    out.append("")
    out.append("=================================== FAILURES ===================================")
    for tid, msg, detail in fails:
        out += [
            f"_______ {tid.split('::')[1]} _______",
            "",
            f"    def {tid.split('::')[1]}():",
            f">       {detail}",
            f"E       {msg}",
            f"{tid.split('::')[0]}:{r.randint(10, 90)}: AssertionError",
            "",
        ]
    out.append("=========================== short test summary info ============================")
    out += [f"FAILED {tid} - {msg}" for tid, msg, _ in fails]
    out.append(
        f"========================= {len(fails)} failed, {n_files * 25 - len(fails)} passed in 12.31s ========================="
    )
    return "\n".join(out) + "\n"


def test_cases():
    f1 = [
        (
            "tests/test_unit_3.py::test_case_7",
            "assert 1999 == 2000",
            "assert refund(19.99) == 2000",
        ),
        (
            "tests/test_unit_9.py::test_case_20",
            "assert 'paid' == 'due'",
            "assert status(inv) == 'due'",
        ),
    ]
    t = pytest_report(30, 18, f1, verbose=True)
    add(
        "pytest_fail_verbose.txt",
        t,
        ["test_report"],
        [
            "FAILED tests/test_unit_3.py::test_case_7 - assert 1999 == 2000",
            "FAILED tests/test_unit_9.py::test_case_20 - assert 'paid' == 'due'",
            "2 failed, 448 passed",
        ],
        must_inline=[
            "FAILED tests/test_unit_3.py::test_case_7 - assert 1999 == 2000",
            "FAILED tests/test_unit_9.py::test_case_20 - assert 'paid' == 'due'",
        ],
        command="pytest -v",
    )
    f2 = [
        (
            "tests/test_unit_11.py::test_case_2",
            "AssertionError: expected 3 rows, got 4",
            "assert len(rows) == 3",
        )
    ]
    add(
        "pytest_fail_quiet.txt",
        pytest_report(31, 300, f2, verbose=False),
        ["test_report"],
        [
            "FAILED tests/test_unit_11.py::test_case_2 - AssertionError: expected 3 rows, got 4",
            "1 failed, 7499 passed",
        ],
        must_inline=[
            "FAILED tests/test_unit_11.py::test_case_2 - AssertionError: expected 3 rows, got 4"
        ],
        command="pytest -q",
    )
    # error buried mid-output (sniffed, no command)
    t = pytest_report(32, 25, [], verbose=True).splitlines()
    mid = len(t) // 2
    t.insert(
        mid,
        "tests/test_unit_12.py::test_case_9 ERROR at setup: fixture 'db_conn' raised OSError: disk quota exceeded",
    )
    add(
        "pytest_error_buried.txt",
        "\n".join(t) + "\n",
        ["test_report"],
        ["fixture 'db_conn' raised OSError: disk quota exceeded"],
        must_inline=["fixture 'db_conn' raised OSError: disk quota exceeded"],
    )
    # jest
    lines = []
    for i in range(300):
        lines.append(f"PASS src/components/widget{i}.test.js (1.{i % 10} s)")
        if i == 77:
            lines.append("FAIL src/cart/cart.test.js")
            lines += [
                "  ● Cart › applies coupon to subtotal",
                "",
                "    expect(received).toBe(expected)",
                "",
                "    Expected: 90",
                "    Received: 100",
                "",
                "      at Object.<anonymous> (src/cart/cart.test.js:41:23)",
                "",
            ]
    lines += [
        "Test Suites: 1 failed, 120 passed, 121 total",
        "Tests:       1 failed, 480 passed, 481 total",
    ]
    add(
        "jest_fail.txt",
        "\n".join(lines) + "\n",
        ["test_report"],
        [
            "FAIL src/cart/cart.test.js",
            "● Cart › applies coupon to subtotal",
            "Expected: 90",
            "Tests:       1 failed, 480 passed, 481 total",
        ],
        must_inline=["FAIL src/cart/cart.test.js", "● Cart › applies coupon to subtotal"],
        command="npx jest",
    )
    # go test
    gl = []
    for i in range(320):
        gl += [f"=== RUN   TestCase{i}", f"--- PASS: TestCase{i} (0.0{i % 10}s)"]
        if i == 101:
            gl += [
                "=== RUN   TestLedgerBalance",
                "    ledger_test.go:58: balance mismatch: got 41, want 42",
                "--- FAIL: TestLedgerBalance (0.01s)",
            ]
    gl += ["FAIL", "FAIL\tgithub.com/example/ledger\t0.482s"]
    add(
        "go_test_fail.txt",
        "\n".join(gl) + "\n",
        ["test_report"],
        [
            "--- FAIL: TestLedgerBalance (0.01s)",
            "ledger_test.go:58: balance mismatch: got 41, want 42",
            "FAIL\tgithub.com/example/ledger\t0.482s",
        ],
        must_inline=[
            "--- FAIL: TestLedgerBalance (0.01s)",
            "ledger_test.go:58: balance mismatch: got 41, want 42",
        ],
        command="go test ./...",
    )
    # cargo test
    cl = ["running 480 tests"] + [f"test engine::case_{i} ... ok" for i in range(480) if i != 150]
    cl.insert(150, "test engine::case_150 ... FAILED")
    cl += [
        "",
        "failures:",
        "",
        "---- engine::case_150 stdout ----",
        "thread 'engine::case_150' panicked at src/engine.rs:77:9:",
        "assertion `left == right` failed: ticks",
        "  left: 12",
        " right: 13",
        "",
        "failures:",
        "    engine::case_150",
        "",
        "test result: FAILED. 479 passed; 1 failed; 0 ignored",
    ]
    add(
        "cargo_test_fail.txt",
        "\n".join(cl) + "\n",
        ["test_report"],
        [
            "test engine::case_150 ... FAILED",
            "panicked at src/engine.rs:77:9:",
            "test result: FAILED. 479 passed; 1 failed; 0 ignored",
        ],
        must_inline=["test engine::case_150 ... FAILED"],
        command="cargo test",
    )


# ------------------------------------------------------------------ compiler
def compiler_cases():
    r = random.Random(40)
    # gcc
    out = []
    for i in range(140):
        out += [
            f"src/net/conn_{i % 9}.c:{20 + i}:{5 + i % 7}: warning: unused variable 'tmp{i}' [-Wunused-variable]",
            f"   {20 + i} |     int tmp{i} = 0;",
            "      |         ^~~~" + str(i),
        ]
    out += [
        "src/net/conn_3.c:301:13: error: 'sock_fd' undeclared (first use in this function)",
        "  301 |     close(sock_fd);",
        "      |           ^~~~~~~",
        "src/net/conn_4.c:12:1: error: expected ';' before '}' token",
        "make: *** [Makefile:44: build/conn.o] Error 1",
    ]
    add(
        "gcc_warnings.txt",
        "\n".join(out) + "\n",
        ["compiler"],
        [
            "src/net/conn_3.c:301:13: error: 'sock_fd' undeclared (first use in this function)",
            "src/net/conn_4.c:12:1: error: expected ';' before '}' token",
        ],
        must_inline=[
            "src/net/conn_3.c:301:13: error: 'sock_fd' undeclared (first use in this function)",
            "src/net/conn_4.c:12:1: error: expected ';' before '}' token",
        ],
        command="gcc -Wall -c src/net/conn.c",
    )
    # tsc
    ts = []
    for i in range(130):
        ts.append(
            f"src/ui/panel{i}.tsx({10 + i},{4 + i % 5}): warning TS6133: 'unused{i}' is declared but its value is never read."
        )
    ts.insert(
        31,
        "src/ui/panel31.tsx(41,9): error TS2322: Type 'string' is not assignable to type 'number'.",
    )
    ts.insert(
        50,
        "src/api/client.ts(7,1): error TS2307: Cannot find module './missing' or its corresponding type declarations.",
    )
    ts += ["", "Found 2 errors in 2 files."]
    ts = ts + [f"  src/ui/panel{i}.tsx:{10 + i}" for i in range(90)]
    add(
        "tsc_errors.txt",
        "\n".join(ts) + "\n",
        ["compiler"],
        [
            "src/ui/panel31.tsx(41,9): error TS2322: Type 'string' is not assignable to type 'number'.",
            "src/api/client.ts(7,1): error TS2307: Cannot find module './missing' or its corresponding type declarations.",
            "Found 2 errors in 2 files.",
        ],
        must_inline=[
            "src/ui/panel31.tsx(41,9): error TS2322: Type 'string' is not assignable to type 'number'.",
            "src/api/client.ts(7,1): error TS2307: Cannot find module './missing' or its corresponding type declarations.",
        ],
    )
    # rustc
    rs = []
    for i in range(55):
        rs += [
            f"warning: unused variable: `v{i}`",
            f" --> src/lib.rs:{10 + i}:9",
            "  |",
            f"{10 + i} |     let v{i} = {i};",
            "  |         ^^ help: if this is intentional, prefix it with an underscore",
            "  |",
            "  = note: `#[warn(unused_variables)]` on by default",
            "",
        ]
    rs += [
        "error[E0308]: mismatched types",
        " --> src/lib.rs:140:20",
        "  |",
        '140 |     let n: u32 = "seven";',
        "  |            ---   ^^^^^^^ expected `u32`, found `&str`",
        "",
        "error: aborting due to 1 previous error; 55 warnings emitted",
    ]
    add(
        "rustc_errors.txt",
        "\n".join(rs) + "\n",
        ["compiler"],
        [
            "error[E0308]: mismatched types",
            "--> src/lib.rs:140:20",
            "error: aborting due to 1 previous error; 55 warnings emitted",
        ],
        must_inline=[
            "error[E0308]: mismatched types",
            "error: aborting due to 1 previous error; 55 warnings emitted",
        ],
        command="cargo build",
    )
    # javac column-0 snippet
    jv = []
    for i in range(70):
        jv += [
            f"src/main/java/com/acme/Legacy{i}.java:{20 + i}: warning: [deprecation] oldCall{i}() in Helper has been deprecated",
            f"        Helper.oldCall{i}();",
            "              ^",
        ]
    jv += [
        "src/main/java/com/acme/Foo.java:3: error: package com.acme.util does not exist",
        "import com.acme.util.Missing;",
        "                    ^",
        "src/main/java/com/acme/Foo.java:19: error: cannot find symbol",
        "int total = count + offset;",
        "^",
        "  symbol:   variable offset",
        "  location: class Foo",
        "2 errors",
        "70 warnings",
    ]
    add(
        "javac_col0.txt",
        "\n".join(jv) + "\n",
        ["compiler"],
        [
            "src/main/java/com/acme/Foo.java:3: error: package com.acme.util does not exist",
            "import com.acme.util.Missing;",
            "src/main/java/com/acme/Foo.java:19: error: cannot find symbol",
            "symbol:   variable offset",
            "2 errors",
        ],
        must_inline=[
            "src/main/java/com/acme/Foo.java:3: error: package com.acme.util does not exist",
            "src/main/java/com/acme/Foo.java:19: error: cannot find symbol",
            "import com.acme.util.Missing;",
        ],
        command="javac -Xlint:all src/main/java/com/acme/Foo.java",
    )
    # clang mixed
    cl = []
    for i in range(80):
        cl += [
            f"lib/util/str{i % 12}.cpp:{30 + i}:11: warning: comparison of integers of different signs [-Wsign-compare]",
            f"    if (n{i} < size) {{",
            "           ~~ ^ ~~~~",
        ]
    cl.insert(120, "lib/util/str5.cpp:77:3: error: use of undeclared identifier 'normalise_key'")
    cl.insert(121, "  normalise_key(buf);")
    cl.insert(122, "  ^")
    cl.append("1 error and 80 warnings generated.")
    add(
        "clang_mixed.txt",
        "\n".join(cl) + "\n",
        ["compiler"],
        [
            "lib/util/str5.cpp:77:3: error: use of undeclared identifier 'normalise_key'",
            "1 error and 80 warnings generated.",
        ],
        must_inline=["lib/util/str5.cpp:77:3: error: use of undeclared identifier 'normalise_key'"],
        command="clang++ -c lib/util/str5.cpp",
    )


# ------------------------------------------------------------------ build_log
def build_cases():
    n = [
        f"npm WARN deprecated pkg-{i}@1.{i}.0: this package is no longer maintained, use pkg-{i}-next"
        for i in range(140)
    ]
    n += [
        "npm ERR! code ERESOLVE",
        "npm ERR! ERESOLVE could not resolve",
        "npm ERR! Found: react@18.3.1",
        'npm ERR! Could not resolve dependency: peer react@"^17.0.0" from old-widgets@2.1.0',
        "npm ERR! A complete log of this run can be found in: /home/ci/.npm/_logs/run-0.log",
    ]
    add(
        "npm_install_fail.txt",
        "\n".join(n) + "\n",
        ["build_log"],
        [
            "npm ERR! code ERESOLVE",
            "npm ERR! Found: react@18.3.1",
            'peer react@"^17.0.0" from old-widgets@2.1.0',
        ],
        must_inline=["npm ERR! code ERESOLVE", "npm ERR! Found: react@18.3.1"],
        command="npm install",
    )
    p = []
    for i in range(70):
        p += [
            f"Collecting libpkg{i}==2.{i}.0",
            f"  Downloading libpkg{i}-2.{i}.0-py3-none-any.whl (1{i} kB)",
            f"Requirement already satisfied: six>=1.{i} in /usr/lib/python3/site-packages (from libpkg{i})",
        ]
    p += [
        "ERROR: Could not find a version that satisfies the requirement foo==9.9.9 (from versions: 1.0, 1.1)",
        "ERROR: No matching distribution found for foo==9.9.9",
    ]
    add(
        "pip_install_fail.txt",
        "\n".join(p) + "\n",
        ["build_log"],
        [
            "ERROR: Could not find a version that satisfies the requirement foo==9.9.9",
            "ERROR: No matching distribution found for foo==9.9.9",
        ],
        must_inline=[
            "ERROR: Could not find a version that satisfies the requirement foo==9.9.9",
            "ERROR: No matching distribution found for foo==9.9.9",
        ],
        command="pip install -r requirements.txt",
    )
    d = []
    for i in range(1, 11):
        d.append(f"#{i} [{i}/12] RUN apt-get install -y package-{i}")
        d += [
            f"#{i} {j * 0.3:.1f} Get:{j} http://deb.example.org/debian bookworm/main amd64 lib{i}-{j} amd64 1.{j} [{j * 11} kB]"
            for j in range(18)
        ]
        d.append(f"#{i} DONE 4.{i}s")
    d += [
        "#11 [11/12] RUN make install",
        "#11 2.1 make: *** [Makefile:9: all] Error 2",
        '#11 ERROR: process "/bin/sh -c make install" did not complete successfully: exit code: 2',
    ]
    add(
        "docker_build_fail.txt",
        "\n".join(d) + "\n",
        ["build_log"],
        [
            '#11 ERROR: process "/bin/sh -c make install" did not complete successfully: exit code: 2',
            "#11 [11/12] RUN make install",
        ],
        must_inline=[
            '#11 ERROR: process "/bin/sh -c make install" did not complete successfully: exit code: 2'
        ],
        command="docker build .",
    )
    g = []
    for m in range(70):
        g += [
            f"> Task :module{m}:compileJava",
            f"> Task :module{m}:processResources NO-SOURCE",
            f"> Task :module{m}:classes",
            f"> Task :module{m}:jar UP-TO-DATE",
            f"Resolved 12 dependencies for :module{m}",
        ] * 1
    g += [
        "> Task :app:compileTestJava FAILED",
        "",
        "FAILURE: Build failed with an exception.",
        "",
        "* What went wrong:",
        "Execution failed for task ':app:compileTestJava'.",
        "> Compilation failed; see the compiler error output for details.",
        "",
        "BUILD FAILED in 41s",
    ]
    add(
        "gradle_fail.txt",
        "\n".join(g) + "\n",
        ["build_log"],
        [
            "> Task :app:compileTestJava FAILED",
            "FAILURE: Build failed with an exception.",
            "BUILD FAILED in 41s",
        ],
        must_inline=[
            "> Task :app:compileTestJava FAILED",
            "FAILURE: Build failed with an exception.",
        ],
        command="./gradlew build",
    )


# ------------------------------------------------------------------ search
def search_cases():
    r = random.Random(60)
    # 50th file adversarial
    out = []
    for f in range(70):
        path = "src/billing/invoice_export.py" if f == 49 else f"src/pkg{f}/handler.py"
        for m in range(1, 7):
            out.append(
                f"{path}:{m * 17 + f}:    result = process_batch(items, size={m})  # TODO tune"
            )
    add(
        "grep_50th_file.txt",
        "\n".join(out) + "\n",
        ["search"],
        ["src/billing/invoice_export.py:", "src/pkg69/handler.py:"],
        must_inline=["src/billing/invoice_export.py:"],
        command="grep -rn TODO src",
    )
    # rg with context
    out = []
    for f in range(40):
        p = f"lib/mod{f}.rs"
        for m in range(4):
            ln = 10 + m * 30
            out += [
                f"{p}-{ln - 1}-    // setup",
                f"{p}:{ln}:    fn dispatch_event(&self) {{",
                f"{p}-{ln + 1}-        self.bus.send();",
                "--",
            ]
    out.append("lib/special_case.rs:999:    fn dispatch_event(&self) {")
    add(
        "rg_context.txt",
        "\n".join(out) + "\n",
        ["search"],
        ["lib/special_case.rs:999:", "lib/mod39.rs:100:"],
        must_inline=["lib/special_case.rs:999:"],
        command="rg -n -C1 dispatch_event",
    )
    # sniffed, no command
    out = [
        f'app/views/page{f}.html:{m + 3}:  <div class="row-{m}">{sentence(r, 5)}</div>'
        for f in range(80)
        for m in range(5)
    ]
    add(
        "grep_html_sniffed.txt",
        "\n".join(out) + "\n",
        ["search"],
        ["app/views/page79.html:", "app/views/page0.html:3:"],
        must_inline=["app/views/page79.html:"],
    )
    # match line with error pattern in many files
    out = []
    for f in range(75):
        for m in range(5):
            out.append(f'svc/api{f}.go:{m * 11 + 4}:    log.Printf("retry %d", {m})')
    out.insert(100, 'svc/api20.go:77:    return fmt.Errorf("upstream failure: %w", err)')
    add(
        "grep_error_line.txt",
        "\n".join(out) + "\n",
        ["search"],
        ['svc/api20.go:77:    return fmt.Errorf("upstream failure: %w", err)', "svc/api74.go:"],
        must_inline=[
            'svc/api20.go:77:    return fmt.Errorf("upstream failure: %w", err)',
            "svc/api74.go:",
        ],
        command="grep -rn retry svc",
    )


# ------------------------------------------------------------------ pplx
def pplx_cases():
    r = random.Random(70)

    def res(i, **extra):
        d = {
            "title": f"Guide to {r.choice(WORDS)} {i}",
            "url": f"https://docs.example.org/guide/{i}",
            "date": "2026-02-1" + str(i % 9),
            "snippet": sentence(r, 30),
            "summary": sentence(r, 45),
        }
        d.update(extra)
        return d

    docs = []
    rs = [res(i) for i in range(30)]
    rs[22]["snippet"] = "error: source rate limit exceeded, partial results"
    add(
        "pplx_error_beyond_topk.json",
        json.dumps({"results": rs, "usage": {"tokens": 912}}, indent=2),
        ["pplx"],
        [
            "https://docs.example.org/guide/22",
            "https://docs.example.org/guide/29",
            "https://docs.example.org/guide/0",
            "error: source rate limit exceeded, partial results",
        ],
        must_inline=[
            "https://docs.example.org/guide/22",
            "https://docs.example.org/guide/29",
            "https://docs.example.org/guide/0",
        ],
        command="pplx search 'rate limit'",
    )
    rs = [res(i) for i in range(30)]
    rs[19] = {
        "title": "Source down",
        "url": "https://docs.example.org/guide/19",
        "error": "upstream fetch failed: HTTP 503",
    }
    add(
        "pplx_error_key_beyond_topk.json",
        json.dumps({"results": rs}, indent=2),
        ["pplx"],
        [
            "upstream fetch failed: HTTP 503",
            "https://docs.example.org/guide/19",
            "https://docs.example.org/guide/29",
        ],
        must_inline=[
            "upstream fetch failed: HTTP 503",
            "https://docs.example.org/guide/19",
            "https://docs.example.org/guide/29",
        ],
        command="pplx search 'status page'",
    )
    rs = [res(i) for i in range(25)]
    add(
        "pplx_list.json",
        json.dumps(rs, indent=2),
        ["pplx"],
        [f"https://docs.example.org/guide/{i}" for i in (0, 12, 24)],
        must_inline=[f"https://docs.example.org/guide/{i}" for i in (0, 12, 24)],
        command="pplx search 'cache policy'",
    )
    rs = [
        res(i, source="web", score=round(0.9 - i * 0.01, 2), extra_text=sentence(r, 20))
        for i in range(35)
    ]
    add(
        "pplx_web_results.json",
        json.dumps({"web_results": rs}, indent=2),
        ["pplx"],
        ["https://docs.example.org/guide/34", "https://docs.example.org/guide/17"],
        must_inline=["https://docs.example.org/guide/34", "https://docs.example.org/guide/17"],
        command="pplx ask 'queue backoff'",
    )
    rs = [res(i) for i in range(18)]
    rs[0]["title"] = "Official reference: sharding"
    add(
        "pplx_indent4.json",
        json.dumps({"results": rs}, indent=4),
        ["pplx"],
        ["Official reference: sharding", "https://docs.example.org/guide/17"],
        must_inline=["Official reference: sharding", "https://docs.example.org/guide/17"],
        command="pplx search sharding",
    )


# ------------------------------------------------------------------ log
def log_cases():
    r = random.Random(80)

    def line(i, lvl, msg):
        return f"2026-03-04T10:{i // 60 % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z {lvl} {msg}"

    L = [line(i, "INFO", f"heartbeat ok seq={i} peer=node-{i % 4}") for i in range(300)]
    L.insert(
        140,
        line(140, "ERROR", "db pool exhausted: 50/50 connections in use, request rid=7f3a dropped"),
    )
    add(
        "log_error_mid.txt",
        "\n".join(L) + "\n",
        ["log"],
        ["db pool exhausted: 50/50 connections in use, request rid=7f3a dropped"],
        must_inline=["db pool exhausted: 50/50 connections in use, request rid=7f3a dropped"],
    )
    L = [line(i, "INFO", f"retry attempt {i} of 400 for job j-81") for i in range(400)]
    L[217] = line(217, "INFO", "retry attempt 217 of 400 for job j-81 (backoff reset by operator)")
    add(
        "log_folded_run.txt",
        "\n".join(L) + "\n",
        ["log"],
        ["backoff reset by operator", "retry attempt 399 of 400"],
    )
    L = []
    for i in range(260):
        L.append(
            line(i, r.choice(["INFO", "DEBUG"]), f"GET /api/v1/items/{i} 200 {r.randint(2, 40)}ms")
        )
    L.insert(0, line(0, "INFO", "server started on :8080 build=2026.11.3"))
    L.append(line(300, "WARN", "slow request /api/v1/items/9 took 4012ms"))
    L.append(line(301, "FATAL", "unrecoverable: config reload failed"))
    add(
        "log_access.txt",
        "\n".join(L) + "\n",
        ["log"],
        ["server started on :8080 build=2026.11.3", "unrecoverable: config reload failed"],
        must_inline=["unrecoverable: config reload failed"],
    )
    L = [
        f"Nov  3 04:{i // 60 % 60:02d}:{i % 60:02d} host01 sshd[{2000 + i}]: Accepted publickey for deploy from 10.0.0.{i % 200}"
        for i in range(280)
    ]
    L.insert(
        90,
        "Nov  3 04:01:30 host01 kernel: Out of memory: Killed process 4412 (java) total-vm:8123456kB",
    )
    L.insert(91, "Nov  3 04:01:31 host01 systemd[1]: app.service: Failed with result 'oom-kill'.")
    add(
        "log_syslog.txt",
        "\n".join(L) + "\n",
        ["log"],
        ["Killed process 4412 (java)", "app.service: Failed with result 'oom-kill'."],
        must_inline=["app.service: Failed with result 'oom-kill'."],
    )


# ------------------------------------------------------------------ table
def table_cases():
    r = random.Random(90)
    rows = ["id,name,email,plan,amount,status"]
    for i in range(300):
        rows.append(
            f"c-{i:04d},Customer {i},cust{i}@example.com,{r.choice(['free', 'pro', 'team'])},{r.randint(5, 900)}.00,active"
        )
    rows[204] = "c-0203,Customer 203,vip.target@example.com,team,777.00,active"
    add(
        "table_customers.csv",
        "\n".join(rows) + "\n",
        ["table"],
        ["id,name,email,plan,amount,status", "vip.target@example.com"],
        must_inline=["id,name,email,plan,amount,status"],
    )
    rows = ["sku\twarehouse\tqty\tbin"] + [
        f"SKU-{i:05d}\tW{i % 5}\t{r.randint(0, 99)}\tA-{i % 40}" for i in range(500)
    ]
    rows[300] = "SKU-00299\tW4\t13\tZ-99-special"
    add(
        "table_inventory.tsv",
        "\n".join(rows) + "\n",
        ["table"],
        ["sku\twarehouse\tqty\tbin", "Z-99-special"],
        must_inline=["sku\twarehouse\tqty\tbin"],
    )
    rows = ["| id | service | p95_ms | owner_team |", "|----|---------|--------|------------|"]
    rows += [f"| {i} | svc-{i} | {r.randint(10, 900)} | team-{i % 6} |" for i in range(300)]
    rows[150] = "| 148 | svc-148 | 4021 | team-payments |"
    add(
        "table_pipes.md",
        "\n".join(rows) + "\n",
        ["table"],
        ["| id | service | p95_ms | owner_team |", "| 148 | svc-148 | 4021 | team-payments |"],
        must_inline=["| id | service | p95_ms | owner_team |"],
    )
    rows = ["minute,metric,value,unit"] + [
        f"m-{i:04d},cpu,{r.randint(1, 99)},pct" for i in range(600)
    ]
    rows[180] = "m-0179,cpu,failed: sensor offline,pct"
    add(
        "table_metrics_error_row.csv",
        "\n".join(rows) + "\n",
        ["table"],
        ["failed: sensor offline", "minute,metric,value,unit"],
        must_inline=["failed: sensor offline", "minute,metric,value,unit"],
    )


# ------------------------------------------------------------------ text
def text_cases():
    r = random.Random(100)
    paras = [sentence(r, 40) + " " + sentence(r, 30) for _ in range(60)]
    paras[41] = "Set the environment variable WIDGET_CACHE_TTL=900 to change how long entries live."
    add(
        "text_readme.md",
        "\n\n".join(f"## Section {i}\n\n{p}" for i, p in enumerate(paras)) + "\n",
        ["text"],
        ["WIDGET_CACHE_TTL=900", "## Section 0"],
    )
    L = [f"Processing record {i} ... done" for i in range(500)]
    L.insert(250, "Error: connection reset by peer while uploading shard 17")
    add(
        "text_script_output_error.txt",
        "\n".join(L) + "\n",
        ["text"],
        [
            "Error: connection reset by peer while uploading shard 17",
            "Processing record 499 ... done",
        ],
        must_inline=["Error: connection reset by peer while uploading shard 17"],
    )
    code = ['"""Billing helpers."""', "", "import math", ""]
    for i in range(100):
        code += [
            f"def helper_{i}(value):",
            f'    """{sentence(r, 6)}"""',
            f"    return math.floor(value * {i + 1}) + {i}",
            "",
        ]
    code += ["def settle_invoice(invoice):", "    return invoice.total - invoice.credit", ""]
    add(
        "text_source_file.txt",
        "\n".join(code),
        ["text"],
        ["def settle_invoice(invoice):", "def helper_0(value):"],
    )
    L = []
    for i in range(160):
        L.append(f"commit {r.getrandbits(160):040x}")
        L.append("Author: Dev Person <dev@example.com>")
        L.append(f"Date:   Mon Mar {1 + i % 28} 10:00:00 2026 +0000")
        L.append("")
        L.append(f"    {sentence(r, 7)}")
        L.append("")
    L[34 * 6 + 4] = '    Revert "bump ledger schema to v9"'
    add(
        "text_git_log.txt",
        "\n".join(L) + "\n",
        ["text"],
        ['Revert "bump ledger schema to v9"', "Author: Dev Person"],
        command="git log",
    )


for fn in (
    json_cases,
    diff_cases,
    stack_cases,
    test_cases,
    compiler_cases,
    build_cases,
    search_cases,
    pplx_cases,
    log_cases,
    table_cases,
    text_cases,
):
    fn()


def q(s):
    return json.dumps(s, ensure_ascii=False)


toml = [
    "# E2 needles; schema in corpus/README.md. types = expected detect() result.",
    "# Generated by make_corpus.py; edit there, not here.",
    "# pplx policy: per-result `error` keys (real fetch failures) are must_inline. Snippet text that",
    "# merely mentions an error is search content: its URL is must_inline, the text only recoverable.",
    "",
]
for c in CASES:
    toml.append("[[case]]")
    toml.append(f"file = {q(c['file'])}")
    if c["command"]:
        toml.append(f"command = {q(c['command'])}")
    toml.append("types = [" + ", ".join(q(t) for t in c["types"]) + "]")
    toml.append("needles = [\n" + "".join(f"  {q(n)},\n" for n in c["needles"]) + "]")
    if c["must_inline"]:
        toml.append("must_inline = [\n" + "".join(f"  {q(n)},\n" for n in c["must_inline"]) + "]")
    toml.append("")
(OUT / "needles.toml").write_text("\n".join(toml), encoding="utf-8")
print(len(CASES), "cases")
