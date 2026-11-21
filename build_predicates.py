"""Record operand relationships observed on the unmodified, pinned SQLGlot test suite.

Run with the suite dependencies installed:
  python build_predicates.py PINNED_SOURCE tests.json predicates.json.gz
The profile contains no patch texts, modification outcomes or failure labels.
"""

import argparse
import ast
import builtins
from collections import defaultdict
import enum
import functools
import gzip
import importlib.abc
import importlib.machinery
import json
import operator
import os
from pathlib import Path
import pickle
import re
import sys
import tempfile

import pytest


OPS = {
    ast.Is: ("is", operator.is_),
    ast.IsNot: ("is not", operator.is_not),
    ast.Eq: ("==", operator.eq),
    ast.NotEq: ("!=", operator.ne),
    ast.Lt: ("<", operator.lt),
    ast.LtE: ("<=", operator.le),
    ast.Gt: (">", operator.gt),
    ast.GtE: (">=", operator.ge),
}
OP_PATTERN = re.compile(r"\bis\s+not\b|\bis\b|<=|>=|==|!=|<|>")
NUMBERS = (int, bool, float)


@functools.cache
def scalar_type(cls):
    if cls in (str, bytes, int, bool, float):
        return cls
    if issubclass(cls, enum.Enum):
        for base in (str, int):
            if issubclass(cls, base) and all(
                getattr(cls, name) is getattr(base, name)
                for name in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
            ):
                return base
        if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
            return enum.Enum
    return None


def relation(left, right):
    identity = int(left is right)
    if left is None or right is None:
        other = type(right if left is None else left)
        if other in (
            type(None),
            int,
            bool,
            float,
            str,
            bytes,
            list,
            tuple,
            set,
            dict,
        ) or (other.__eq__ is object.__eq__ and other.__ne__ is object.__ne__):
            return 32 | identity | (identity << 1)
        return 0
    a, b = type(left), type(right)
    if a is tuple and b is tuple:
        if not all(type(item) is int for item in left + right):
            return 0
    else:
        a, b = scalar_type(a), scalar_type(b)
        if a is None or b is None:
            return 0
        if a is enum.Enum or b is enum.Enum:
            return 32 | identity | (identity << 1) if a is b else 0
        if a != b and not (a in NUMBERS and b in NUMBERS):
            return 32 | identity
    return (
        32
        | 16
        | identity
        | (int(left == right) << 1)
        | (int(left < right) << 2)
        | (int(left > right) << 3)
    )


class Recorder:
    def __init__(self, root, ids):
        self.root = root
        self.ids = {nodeid: i for i, nodeid in enumerate(ids)}
        self.current = None
        self.sites = []
        self.ops = []
        self.observed = []
        self.unscoped = set()
        self.completed = set()
        self.failed = set()

    def compare(self, index, left, right):
        result = self.ops[index](left, right)
        if self.current is None:
            self.unscoped.add(index)
        else:
            per_test = self.observed[index]
            per_test[self.current] = per_test.get(self.current, 0) | (
                1 << relation(left, right)
            )
        return result

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        self.current = self.ids.get(item.nodeid)
        try:
            return (yield)
        finally:
            self.current = None

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.ids:
            return
        index = self.ids[report.nodeid]
        if report.failed or report.skipped:
            self.failed.add(index)
        if report.when == "call" and report.passed:
            self.completed.add(index)

    def merge_workers(self, directory):
        indices = {site: i for i, site in enumerate(self.sites)}
        for worker in Path(directory).glob("*.json"):
            for site, observed, shared in json.loads(worker.read_text()):
                site = tuple(site)
                if site not in indices:
                    indices[site] = len(self.sites)
                    self.sites.append(site)
                    self.observed.append({})
                index = indices[site]
                if shared:
                    self.unscoped.add(index)
                per_test = self.observed[index]
                for nodeid, mask in observed.items():
                    test = self.ids[nodeid]
                    per_test[test] = per_test.get(test, 0) | mask

    def save(self, path):
        sites = defaultdict(lambda: defaultdict(list))
        for index, (file, line, prefix, op, suffix) in enumerate(self.sites):
            if index in self.unscoped or not self.observed[index]:
                continue
            groups = defaultdict(list)
            for test, mask in self.observed[index].items():
                groups[mask].append(test)
            sites[file][line].append([prefix, op, suffix, groups])
        result = {
            "ids": list(self.ids),
            "incomplete": sorted(
                set(self.ids.values()) - (self.completed - self.failed)
            ),
            "sites": sites,
        }
        with gzip.GzipFile(path, "wb", compresslevel=6, mtime=0) as out:
            out.write(json.dumps(result, separators=(",", ":")).encode())
        print(
            f"Recorded {sum(len(lines) for lines in sites.values())} predicate lines; "
            f"{len(result['incomplete'])} incomplete tests",
            file=sys.stderr,
        )


class Instrument(ast.NodeTransformer):
    def __init__(self, recorder, path, src):
        self.recorder = recorder
        self.path = path
        self.lines = src.splitlines()
        self.in_function = False

    def visit_FunctionDef(self, node):
        prior = self.in_function
        self.in_function = True
        self.generic_visit(node)
        self.in_function = prior
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not self.in_function or len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        lhs, rhs = node.left, node.comparators[0]
        if not isinstance(rhs, (ast.Constant, ast.Tuple, ast.Attribute)):
            return node
        if lhs.end_lineno != rhs.lineno:
            return node
        op, run = OPS[type(node.ops[0])]
        line = self.lines[rhs.lineno - 1]
        left_end = len(line.encode()[: lhs.end_col_offset].decode())
        right_start = len(line.encode()[: rhs.col_offset].decode())
        matches = list(OP_PATTERN.finditer(line, left_end, right_start))
        if len(matches) != 1 or matches[0].group() != op:
            return node
        m = matches[0]
        site = len(self.recorder.sites)
        self.recorder.sites.append(
            (self.path, rhs.lineno, line[: m.start()], op, line[m.end() :])
        )
        self.recorder.ops.append(run)
        self.recorder.observed.append({})
        wrapped = ast.Call(
            ast.Name("_br_record_comparison", ast.Load()),
            [ast.Constant(site), lhs, rhs],
            [],
        )
        return ast.copy_location(wrapped, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        src = self.get_data(self.path).decode("utf-8")
        tree = ast.parse(src, self.path)
        rel = Path(self.path).relative_to(self.recorder.root).as_posix()
        tree = Instrument(self.recorder, rel, src).visit(tree)
        return compile(
            ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True
        )


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, recorder):
        self.recorder = recorder

    def find_spec(self, fullname, path, target=None):
        if fullname.split(".", 1)[0] != "sqlglot":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec and spec.origin and spec.origin.endswith(".py"):
            loader = Loader(fullname, spec.origin)
            loader.recorder = self.recorder
            spec.loader = loader
        return spec


def install(recorder):
    sys.path.insert(0, str(recorder.root))
    sys.meta_path.insert(0, Finder(recorder))
    builtins._br_record_comparison = recorder.compare


def worker_profile():
    """Pytest's executor tests spawn a fresh interpreter to execute SQL."""
    from multiprocessing.util import Finalize

    global worker
    worker = Recorder(Path(os.environ["BR_PRED_SOURCE"]), [])
    install(worker)

    def save():
        rows = [
            [site, obs, i in worker.unscoped]
            for i, (site, obs) in enumerate(zip(worker.sites, worker.observed))
            if obs or i in worker.unscoped
        ]
        out = Path(os.environ["BR_PRED_WORK"]) / f"{os.getpid()}.json"
        out.write_text(json.dumps(rows, separators=(",", ":")))

    Finalize(None, save, exitpriority=0)


def _in_worker(nodeid, serialized):
    # The optimizer reuses one pool for many tests. Tag each individual job;
    # retain the tag while multiprocessing serializes its returned expression.
    worker.current = nodeid
    function, args, kwargs = pickle.loads(serialized)
    return function(*args, **kwargs)


def tag_jobs():
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing.reduction import ForkingPickler

    original = ProcessPoolExecutor.submit

    def submit(pool, function, /, *args, **kwargs):
        nodeid = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
        serialized = bytes(ForkingPickler.dumps((function, args, kwargs)))
        return original(pool, _in_worker, nodeid or None, serialized)

    ProcessPoolExecutor.submit = submit


def main():
    args = argparse.ArgumentParser()
    args.add_argument("source", type=Path)
    args.add_argument("catalog", type=Path)
    args.add_argument("output", type=Path)
    options = args.parse_args()
    source, output = options.source.resolve(), options.output.resolve()
    catalog = json.loads(options.catalog.read_text())["tests"]
    ids = [row["id"] for row in catalog]
    recorder = Recorder(source, ids)
    os.chdir(source)
    install(recorder)
    tag_jobs()
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    with tempfile.TemporaryDirectory() as directory:
        os.environ["BR_PRED_SOURCE"] = str(source)
        os.environ["BR_PRED_WORK"] = directory
        status = pytest.main(["-q", "-o", "addopts=", *ids], plugins=[recorder])
        if status in (0, 1):
            recorder.merge_workers(directory)
            recorder.save(output)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
elif __name__ == "__mp_main__" and os.environ.get("BR_PRED_WORK"):
    worker_profile()
