"""Measure exact class-table dispatches on the unmodified contest baseline.

Python 3.13: python build_dispatch.py CHECKOUT tests.json dispatch.json.gz
Use the organizer's dependency lock and set SKIP_INTEGRATION=0.
"""

import ast
import builtins
import functools
import json
import os
import sys
import types
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import pytest

from build_map import Recorder, encode
from config_profile import serializing_pool
from dispatch_map import entries


@functools.cache
def factory_bodies(path):
    tree = ast.parse(Path(path).read_bytes())
    pure = set()
    for definition in ast.walk(tree):
        if not isinstance(definition, ast.FunctionDef) or definition.decorator_list:
            continue
        body = list(definition.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            body.pop(0)
        if not body or not isinstance(body[-1], ast.Return):
            continue
        value = body[-1].value
        if len(body) == 1 and isinstance(value, ast.Lambda):
            returned = value
        elif (
            len(body) == 2
            and isinstance(body[0], ast.FunctionDef)
            and not body[0].decorator_list
            and isinstance(value, ast.Name)
            and value.id == body[0].name
        ):
            returned = body[0]
        else:
            continue
        if not returned.args.defaults and not any(returned.args.kw_defaults):
            pure.add((definition.name, returned.lineno))
    return pure


class Dispatch(Recorder):
    def __init__(self, root, ids):
        super().__init__(root, ids)
        self.objects = []
        self.codes = {}
        self.observed = defaultdict(int)
        self.unknown = set()
        self.ends = {}
        self.pool_submitters = set()
        self.mon.use_tool_id(self.tool, "sqlglot class dispatch")
        self.mon.register_callback(self.tool, self.mon.events.PY_START, self.enter)

    def register(self, site, end, factory, fn):
        if not isinstance(fn, types.FunctionType):
            return fn
        code = fn.__code__
        if factory:
            parent = fn.__qualname__.split(".<locals>.")
            if (
                len(parent) < 2
                or parent[-2].split(".")[-1] != factory
                or not code.co_filename.startswith(self.root)
                or (factory, code.co_firstlineno)
                not in factory_bodies(code.co_filename)
            ):
                return fn
        try:
            captures = tuple(id(cell.cell_contents) for cell in (fn.__closure__ or ()))
        except ValueError:
            return fn
        self.objects.append(fn)
        self.ends[site] = end
        if id(code) not in self.codes:
            self.codes[id(code)] = (code.co_freevars, defaultdict(set))
            self.mon.set_local_events(self.tool, code, self.mon.events.PY_START)
        self.codes[id(code)][1][captures].add(site)
        return fn

    def enter(self, code, offset):
        freevars, sites = self.codes[id(code)]
        if freevars:
            frame = sys._getframe(1)
            local = frame.f_locals
            marks = sites.get(tuple(id(local[k]) for k in freevars), ())
        else:
            marks = sites[()]
        for mark in marks:
            if self.active:
                self.observed[mark] |= self.active
            else:
                self.unknown.add(mark)
        if not freevars:
            return self.mon.DISABLE


def main(root, inventory, output):
    root = Path(root).resolve()
    inventory = Path(inventory).resolve()
    output = Path(output).resolve()
    if (
        root / ".git" / "HEAD"
    ).read_text().strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Use the clean, pinned, detached checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    ids = [test["id"] for test in json.loads(inventory.read_text())["tests"]]
    trace = Dispatch(root, ids)
    builtins._br_dispatch_value = trace.register
    original = SourceFileLoader.get_code

    def get_code(loader, module):
        if not (module == "sqlglot" or module.startswith("sqlglot.")):
            return original(loader, module)
        path = Path(loader.get_filename(module))
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), str(path))
        for table, i, expr in list(entries(tree)):
            factory = ""
            if isinstance(expr, ast.Call):
                if not isinstance(expr.func, (ast.Name, ast.Attribute)):
                    continue
                factory = (
                    expr.func.id if isinstance(expr.func, ast.Name) else expr.func.attr
                )
            mark = f"{relative}|{expr.lineno}:{expr.col_offset}"
            wrapper = ast.Call(
                ast.Name("_br_dispatch_value", ast.Load()),
                [
                    ast.Constant(mark),
                    ast.Constant(expr.end_lineno),
                    ast.Constant(factory),
                    expr,
                ],
                [],
            )
            table.values[i] = ast.copy_location(wrapper, expr)
        return compile(
            ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True
        )

    def observed_pool(cls, *args, **kwargs):
        pool = serializing_pool(cls, *args, **kwargs)
        submit = pool.submit

        def record(fn, /, *args, **kwargs):
            trace.pool_submitters.add(trace.active)
            return submit(fn, *args, **kwargs)

        pool.submit = record
        return pool

    with (
        patch.object(SourceFileLoader, "get_code", get_code),
        patch.object(ProcessPoolExecutor, "__new__", observed_pool),
    ):
        result = pytest.main(
            ["-q", "--tb=short", "--disable-warnings", "tests"], plugins=[trace]
        )
    if (
        result != 0
        or len(trace.status) != len(ids)
        or set(trace.status.values()) != {"passed"}
    ):
        raise RuntimeError(
            f"Profiling run incomplete: {result}, {len(trace.status)}/{len(ids)}"
        )
    files = defaultdict(dict)
    for site, end in sorted(trace.ends.items()):
        if site in trace.unknown:
            continue
        path, pos = site.split("|")
        mask = trace.observed[site]
        files[path][pos] = [end, [i for i in range(len(ids)) if mask & (1 << i)]]
    encode({"ids": ids, "files": files}, output)
    print(
        f"Mapped {sum(map(len, files.values()))} dispatch entries across {len(files)} files, "
        f"{len(trace.pool_submitters)} pool submitters, {len(trace.unknown)} import-time exclusions"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
