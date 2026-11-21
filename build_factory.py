"""Profile calls to locally constructed closures with a delayed string parameter."""

import ast
import builtins
import json
import os
from pathlib import Path
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from importlib.machinery import SourceFileLoader
from unittest.mock import patch
import types
import weakref

import pytest

from build_map import Recorder, encode
from config_profile import serializing_pool


def proven(module):
    # A fresh counter has no external side effects and gives each closure a
    # unique capture. The string is used exclusively in the returned lambda.
    counters = {
        name.asname or name.name
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module == "itertools"
        for name in node.names
        if name.name == "count"
    }
    result = set()
    for fn in module.body:
        if (
            not isinstance(fn, ast.FunctionDef)
            or fn.decorator_list
            or len(fn.args.args) != 1
        ):
            continue
        body = list(fn.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            body.pop(0)
        if (
            len(body) != 2
            or not isinstance(body[0], ast.Assign)
            or not isinstance(body[1], ast.Return)
        ):
            continue
        init, closure = body[0], body[1].value
        if (
            len(init.targets) != 1
            or not isinstance(init.targets[0], ast.Name)
            or not isinstance(init.value, ast.Call)
            or not isinstance(init.value.func, ast.Name)
            or init.value.func.id not in counters
            or init.value.args
            or init.value.keywords
            or not isinstance(closure, ast.Lambda)
            or closure.args.defaults
            or closure.args.kw_defaults
        ):
            continue
        reads = {n.id for n in ast.walk(closure.body) if isinstance(n, ast.Name)}
        if {fn.args.args[0].arg, init.targets[0].id} <= reads:
            result.add(fn.name)
    return result


def edges(tree):
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def direct_consumer(node, parent):
    return isinstance(parent, ast.Call) and parent.func is node


def safe_local(scope, name, parents, helpers, visited):
    key = id(scope), name
    if key in visited:
        return False
    for use in ast.walk(scope):
        if (
            not isinstance(use, ast.Name)
            or use.id != name
            or not isinstance(use.ctx, ast.Load)
        ):
            continue
        owner = parents[use]
        if direct_consumer(use, owner):
            continue
        if (
            not isinstance(owner, ast.Call)
            or not isinstance(owner.func, ast.Name)
            or use not in owner.args
        ):
            return False
        helper = helpers.get(owner.func.id)
        pos = owner.args.index(use)
        if not helper or len(helper.args.args) <= pos:
            return False
        if not safe_local(
            helper, helper.args.args[pos].arg, parents, helpers, visited | {key}
        ):
            return False
    return True


def discover(root):
    trees = {
        path.relative_to(root).as_posix(): ast.parse(path.read_bytes())
        for base in ("sqlglot", "tests")
        for path in (root / base).rglob("*.py")
    }
    factories = {
        path[:-3].replace("/", "."): proven(tree)
        for path, tree in trees.items()
        if path.startswith("sqlglot/")
    }
    sites = defaultdict(set)
    for path, tree in trees.items():
        if not path.startswith("sqlglot/"):
            continue
        imported = set(factories[path[:-3].replace("/", ".")])
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in factories:
                imported.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in factories[node.module]
                )
        if not imported:
            continue
        parents = edges(tree)
        helpers = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        for assign in ast.walk(tree):
            if not isinstance(assign, ast.Assign) or len(assign.targets) != 1:
                continue
            fn, target = assign.value, assign.targets[0]
            if not (
                isinstance(fn, ast.Call)
                and isinstance(fn.func, ast.Name)
                and fn.func.id in imported
                and len(fn.args) == 1
                and isinstance(fn.args[0], ast.Constant)
                and type(fn.args[0].value) is str
                and not fn.keywords
            ):
                continue
            scope = assign
            while scope in parents and not isinstance(scope, ast.FunctionDef):
                scope = parents[scope]
            if not isinstance(scope, ast.FunctionDef):
                continue
            if isinstance(target, ast.Name):
                ok = safe_local(scope, target.id, parents, helpers, set())
            elif isinstance(target, ast.Attribute):
                # A member can be read in another subclass or in a test.
                ok = True
                for unit in trees.values():
                    for consumer in ast.walk(unit):
                        if (
                            isinstance(consumer, ast.Attribute)
                            and consumer.attr == target.attr
                            and isinstance(consumer.ctx, ast.Load)
                        ):
                            if not direct_consumer(consumer, edges(unit)[consumer]):
                                ok = False
                        if (
                            isinstance(consumer, ast.Call)
                            and isinstance(consumer.func, ast.Name)
                            and consumer.func.id in {"getattr", "hasattr"}
                        ):
                            if (
                                len(consumer.args) > 1
                                and isinstance(consumer.args[1], ast.Constant)
                                and consumer.args[1].value == target.attr
                            ):
                                ok = False
            else:
                ok = False
            if ok:
                sites[path].add((fn.lineno, fn.col_offset))
    return sites


class Invocations(Recorder):
    def __init__(self, root, ids):
        super().__init__(root, ids)
        self.codes = {}
        self.live = {}
        self.references = {}
        self.calls = defaultdict(int)
        self.unknown = set()
        self.mon.use_tool_id(self.tool, "closure invocation")
        self.mon.register_callback(self.tool, self.mon.events.PY_START, self.enter)

    def switch(self, mask):
        self.active = mask

    def register(self, site, function):
        if not isinstance(function, types.FunctionType):
            self.unknown.add(site)
            return function
        code = function.__code__
        cells = tuple(id(cell.cell_contents) for cell in function.__closure__ or ())
        key = id(code), cells
        if key in self.live:
            self.unknown.update((site, self.live[key]))
            return function
        self.live[key] = site
        ident = id(function)

        def release(ref):
            self.live.pop(key, None)
            self.references.pop(ident, None)

        self.references[ident] = weakref.ref(function, release)
        if id(code) not in self.codes:
            self.codes[id(code)] = (code, code.co_freevars)
            self.mon.set_local_events(self.tool, code, self.mon.events.PY_START)
        return function

    def enter(self, code, offset):
        frame = sys._getframe(1)
        local = frame.f_locals
        site = self.live.get(
            (id(code), tuple(id(local[x]) for x in self.codes[id(code)][1]))
        )
        if site:
            if self.active:
                self.calls[site] |= self.active
            else:
                self.unknown.add(site)


def main(root, inventory, output):
    root = Path(root).resolve()
    ids = [v["id"] for v in json.loads(Path(inventory).read_text())["tests"]]
    output = Path(output).resolve()
    if (
        root / ".git/HEAD"
    ).read_text().strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Expected pinned detached checkout")
    sites = discover(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    tracer = Invocations(root, ids)
    builtins._br_origin = tracer.register
    original = SourceFileLoader.get_code

    def load(loader, module):
        path = Path(loader.get_filename(module))
        if not path.is_relative_to(root):
            return original(loader, module)
        rel = path.relative_to(root).as_posix()
        if rel not in sites:
            return original(loader, module)
        tree = ast.parse(path.read_bytes(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if (call.lineno, call.col_offset) in sites[rel]:
                    label = f"{rel}|{call.lineno}:{call.col_offset}"
                    node.value = ast.copy_location(
                        ast.Call(
                            ast.Name("_br_origin", ast.Load()),
                            [ast.Constant(label), call],
                            [],
                        ),
                        call,
                    )
        return compile(
            ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True
        )

    with (
        patch.object(SourceFileLoader, "get_code", load),
        patch.object(ProcessPoolExecutor, "__new__", serializing_pool),
    ):
        rc = pytest.main(["-q", "--tb=short", "tests"], plugins=[tracer])
    if (
        rc
        or len(tracer.status) != len(ids)
        or set(tracer.status.values()) != {"passed"}
    ):
        raise RuntimeError(f"Failed baseline {rc}: {len(tracer.status)} tests")
    files = defaultdict(dict)
    for path, marks in sites.items():
        for line, col in marks:
            label = f"{path}|{line}:{col}"
            if label not in tracer.unknown:
                mask = tracer.calls[label]
                files[path][f"{line}:{col}"] = [
                    i for i in range(len(ids)) if mask & (1 << i)
                ]
    encode({"ids": ids, "files": files}, output)
    print(
        f"Verified {len(ids)} tests, {sum(map(len, files.values()))} factory call sites"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
