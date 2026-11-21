"""Target the SQL fixtures within the upstream executor's two multiprocessing tests."""

import ast
import functools
import gzip
import json
import os
from pathlib import Path
import sys


PREFIX = "tests/test_executor.py::TestExecutor::"
TESTS = [PREFIX + method for method in ("test_execute_tpch", "test_execute_tpcds")]
MAP_FILE = Path(__file__).with_name("fixture_cases.json.gz")


@functools.cache
def _read_map():
    if not MAP_FILE.is_file():
        return {}
    with gzip.open(MAP_FILE, "rt", encoding="utf8") as stream:
        return json.load(stream)


@functools.lru_cache(maxsize=8)
def _touched(path, old_source, new_source):
    """Use only replacements within executable statements of an existing function."""
    if not path.startswith("sqlglot/") or not path.endswith(".py") or not new_source:
        return None
    try:
        # The parser must honor encoding declarations on the actual UTF-8
        # bytes that the patched tree writes, including a newly invalid codec.
        old, new = ast.parse(old_source.encode()), ast.parse(new_source.encode())
        compile(new_source.encode(), path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    if ast.dump(old) == ast.dump(new):
        return []
    lines = set()
    def visit(a, b, func=None, statement=None):
        if isinstance(a, ast.expr) and isinstance(b, ast.expr) and func and statement:
            if ast.dump(a) == ast.dump(b):
                return True
            # A replacement that introduces a local declaration can change names
            # earlier in the function. Limit this path to ordinary expressions.
            exceptional = (ast.NamedExpr, ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await,
                           ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)
            if not any(isinstance(x, exceptional) or (
                isinstance(x, ast.Name) and not isinstance(x.ctx, ast.Load)
            ) for expr in (a, b) for x in ast.walk(expr)):
                return record(func, statement)
        if type(a) is not type(b):
            if isinstance(a, (ast.operator, ast.cmpop, ast.unaryop, ast.boolop)) and (
                isinstance(b, (ast.operator, ast.cmpop, ast.unaryop, ast.boolop))
            ):
                return record(func, statement)
            return False
        if isinstance(a, list):
            return len(a) == len(b) and all(visit(x, y, func, statement) for x, y in zip(a, b))
        if not isinstance(a, ast.AST):
            return a == b
        if isinstance(a, ast.stmt):
            statement = a
        if isinstance(a, ast.Constant):
            return ast.dump(a) == ast.dump(b) or record(func, statement)
        if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Load) and isinstance(b.ctx, ast.Load):
            return a.id == b.id or record(func, statement)
        for field in a._fields:
            nested = func
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested = a if field == "body" else None
            elif isinstance(a, ast.ClassDef):
                nested = None
            if not visit(getattr(a, field), getattr(b, field), nested, statement):
                return False
        return True

    def record(func, stmt):
        if func is None or not isinstance(stmt, (
            ast.Return, ast.Assign, ast.AnnAssign, ast.Expr, ast.If, ast.While, ast.Assert,
            ast.Raise, ast.For, ast.AugAssign,
        )):
            return False
        if any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in ast.walk(func)):
            return False
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return False
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and (
                node.func.attr in {"setdefault", "update", "clear", "add", "discard", "remove",
                                   "append", "extend", "insert", "pop", "popitem"}
            ):
                return False
        header = stmt.test if isinstance(stmt, (ast.If, ast.While)) else (
            stmt.iter if isinstance(stmt, ast.For) else stmt
        )
        lines.update(range(stmt.lineno, header.end_lineno + 1))
        return True
    return sorted(lines) if visit(old, new) and lines else None


def plan(changes, nodeid):
    """Return zero-based fixture indices, or None for edits or setup outside this profile."""
    table = _read_map().get(nodeid)
    if table is None:
        return None
    mask = 0
    for change in changes:
        lines = _touched(change.path, change.old, change.new)
        if lines is None:
            return None
        if not lines:
            continue
        rows = table["paths"].get(change.path, {})
        for line in lines:
            values = rows.get(str(line))
            if values is None:
                continue
            value = table["masks"][values]
            if value & 1:  # Setup/teardown is shared by every case.
                return None
            mask |= value >> 1
    return [i for i in range(table["count"]) if mask & (1 << i)]


class LocalPool:
    """Use the upstream query worker and preserve the framework's pickle boundary."""

    def __init__(self, *, initializer, initargs, **kwargs):
        from concurrent.futures import Future
        from multiprocessing.reduction import ForkingPickler
        import pickle
        self.future = Future
        self.transfer = lambda value: pickle.loads(ForkingPickler.dumps(value))
        initializer(*self.transfer(initargs))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args):
        future = self.future()
        try:
            value = function(*self.transfer(args))
            future.set_result(self.transfer(value))
        except Exception as error:
            future.set_exception(error)
        return future


def replay(nodeid, indices=None, recorder=None):
    """Patched SQLGlot and its tests must be importable before calling this function."""
    if nodeid not in TESTS:
        raise ValueError(nodeid)
    from unittest.mock import patch
    from tests import test_executor
    suite = test_executor.TestExecutor
    method = nodeid.split("::")[-1]
    field = "tpch_sqls" if nodeid == TESTS[0] else "tpcds_sqls"
    suite.setUpClass()
    try:
        case = suite(method)
        original = getattr(suite, field)
        requested = range(len(original)) if indices is None else indices
        with patch.object(test_executor, "ProcessPoolExecutor", LocalPool):
            for i in requested:
                if recorder:
                    recorder.switch(i + 1)
                setattr(case, field, [original[i]])
                getattr(case, method)()
        return len(original)
    finally:
        if recorder:
            recorder.switch(0)
        suite.tearDownClass()


def main(args):
    if len(args) != 4 or args[0] != "--run":
        raise SystemExit("Usage: fixture_cases.py --run PATCHED_ROOT NODEID JSON_INDICES")
    root = Path(args[1]).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    replay(args[2], json.loads(args[3]))


if __name__ == "__main__":
    main(sys.argv[1:])
