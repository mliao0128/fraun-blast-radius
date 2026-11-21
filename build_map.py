"""Build a per-test impact map from an unmodified SQLGlot checkout (Python 3.13).

Usage: python build_map.py CHECKOUT tests.json impact.json.gz
Install the organizer's test dependencies in this interpreter before running.
"""

import argparse
import functools
import gzip
import importlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from unittest.mock import patch

import pytest


def encode(data, target):
    with (
        open(target, "wb") as handle,
        gzip.GzipFile(
            fileobj=handle, mode="wb", filename="", compresslevel=6, mtime=0
        ) as out,
    ):
        out.write(json.dumps(data, separators=(",", ":")).encode())


def imported_files(module):
    importlib.import_module(module)
    root = Path.cwd()
    paths = set()
    for item in tuple(sys.modules.values()):
        source = getattr(item, "__file__", None)
        if source:
            path = Path(source).resolve()
            if path.is_relative_to(root) and path.suffix == ".py":
                paths.add(path.relative_to(root).as_posix())
    print(json.dumps(sorted(paths)))


class Recorder:
    def __init__(self, root, ids):
        self.ids = ids
        self.index = {name: i for i, name in enumerate(ids)}
        self.root = str(root) + os.sep
        self.active = 0
        self.lines = defaultdict(dict)
        self.reads = defaultdict(int)
        self.status = {}
        self.scope_masks = defaultdict(int)
        for i, name in enumerate(ids):
            parts = name.split("::")
            for length in range(1, len(parts) + 1):
                self.scope_masks["::".join(parts[:length])] |= 1 << i
        self.scope_masks[""] = (1 << len(ids)) - 1
        self.mon = sys.monitoring
        self.tool = 3

    @functools.lru_cache(None)
    def relative(self, name):
        if name.startswith(self.root):
            return name[len(self.root) :].replace(os.sep, "/")
        return ""

    def line(self, code, line):
        path = self.relative(code.co_filename)
        if path and self.active:
            rows = self.lines[path]
            rows[line] = rows.get(line, 0) | self.active
        return self.mon.DISABLE

    def audit(self, event, arguments):
        if event == "open" and self.active:
            name = arguments[0]
            if isinstance(name, str):
                rel = self.relative(os.path.abspath(name))
                if rel and "__pycache__" not in rel and not rel.startswith(".git/"):
                    self.reads[rel] |= self.active

    def start(self):
        self.mon.use_tool_id(self.tool, "sqlglot per-test coverage")
        self.mon.register_callback(self.tool, self.mon.events.LINE, self.line)
        self.mon.set_events(self.tool, self.mon.events.LINE)
        sys.addaudithook(self.audit)

    def switch(self, mask):
        if mask != self.active:
            self.active = mask
            self.mon.restart_events()

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.index]

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.switch(1 << self.index[item.nodeid])
        try:
            return (yield)
        finally:
            self.active = 0

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.active
        if fixturedef.scope != "function":
            self.switch(self.scope_masks.get(request.node.nodeid, previous))
        try:
            return (yield)
        finally:
            self.switch(previous)

    def pytest_runtest_logreport(self, report):
        if report.skipped or report.failed:
            self.status[report.nodeid] = report.outcome
        elif report.when == "call":
            self.status.setdefault(report.nodeid, "passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    ids = [row["id"] for row in json.loads(args.inventory.read_text())["tests"]]
    # The official slim image omits git. A pinned detached checkout stores the
    # full object name in .git/HEAD, so the map needs no extra OS packages.
    head = (root / ".git" / "HEAD").read_text().strip()
    rev = (
        head
        if not head.startswith("ref:")
        else subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
    )
    if rev != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError(
            "Coverage must be generated on the competition's pinned revision"
        )
    os.chdir(root)
    sys.path.insert(0, str(root))
    recorder = Recorder(root, ids)
    recorder.start()
    pytest_args = ["-q", "--tb=short", "--disable-warnings"]
    used_pool = 0
    native_submit = ProcessPoolExecutor.submit

    def watch_submission(executor, *args, **kwargs):
        nonlocal used_pool
        used_pool |= recorder.active
        return native_submit(executor, *args, **kwargs)

    with patch.object(ProcessPoolExecutor, "submit", watch_submission):
        code = pytest.main([*pytest_args, "tests"], plugins=[recorder])

    # Spawned interpreters do not inherit the parent's PEP 669 callbacks.
    # Replay only tests which submitted process work, keeping the pool's
    # initializer and future interface and running callbacks in this interpreter.
    pool_nodes = [name for i, name in enumerate(ids) if used_pool & (1 << i)]
    if pool_nodes:

        def transit(value):
            return ForkingPickler.loads(ForkingPickler.dumps(value))

        class TracedPool(ThreadPoolExecutor):
            def submit(self, fn, /, *args, **kwargs):
                def deliver():
                    target, positional, named = transit((fn, args, kwargs))
                    return transit(target(*positional, **named))

                return super().submit(deliver)

        def same_interpreter_pool(cls, *args, initializer=None, initargs=(), **kwargs):
            if initializer is not None:
                initializer, initargs = transit((initializer, initargs))
            return TracedPool(max_workers=1, initializer=initializer, initargs=initargs)

        with patch.object(ProcessPoolExecutor, "__new__", same_interpreter_pool):
            replay = pytest.main([*pytest_args, *pool_nodes], plugins=[recorder])
        if replay != 0:
            raise RuntimeError(
                "Child-work replay failed, please inspect the pytest log"
            )
    recorder.mon.set_events(recorder.tool, 0)
    if (
        code != 0
        or len(recorder.status) != len(ids)
        or set(recorder.status.values()) != {"passed"}
    ):
        raise RuntimeError(
            f"Incomplete test run: pytest={code}, recorded={len(recorder.status)}/{len(ids)}"
        )
    imports = defaultdict(int)
    modules = sorted({name.split("::")[0] for name in ids})

    def import_probe(path):
        module_name = path[:-3].replace("/", ".")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", module_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return path, json.loads(proc.stdout.splitlines()[-1])

    with ThreadPoolExecutor(max_workers=4) as pool:
        for path, imported in pool.map(import_probe, modules):
            for source in imported:
                imports[source] |= recorder.scope_masks[path]
    for source, rows in recorder.lines.items():
        for mask in rows.values():
            imports[source] |= mask
    pool_ids = {}
    pools = []

    def intern(mask):
        if mask not in pool_ids:
            pool_ids[mask] = len(pools)
            members = []
            while mask:
                bit = mask & -mask
                members.append(bit.bit_length() - 1)
                mask ^= bit
            pools.append(members)
            return len(pools) - 1
        return pool_ids[mask]

    result = {
        "nodeids": ids,
        "pool_nodeids": pool_nodes,
        "files": {
            path: {line: intern(mask) for line, mask in sorted(rows.items())}
            for path, rows in sorted(recorder.lines.items())
        },
        "reads": {path: intern(mask) for path, mask in sorted(recorder.reads.items())},
        "imports": {path: intern(mask) for path, mask in sorted(imports.items())},
        "pools": pools,
        "baseline_nonpass": {
            key: value for key, value in recorder.status.items() if value != "passed"
        },
        "revision": rev,
    }
    encode(result, output)
    print(
        f"Mapped {len(ids)} tests in {len(result['files'])} files ({len(pools)} unique sets)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        sys.path.insert(0, os.getcwd())
        imported_files(sys.argv[2])
    else:
        main()
