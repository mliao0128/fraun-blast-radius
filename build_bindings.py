"""Trace class configuration reads and dispatch-table keys against pristine SQLGlot."""

import ast
import builtins
import functools
import gzip
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch
from config_profile import Values, instrument_config, serializing_pool
import json
import os
import sys
from collections import defaultdict
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def main(root, inventory, output):
    root = Path(root).resolve()
    inventory = json.loads(Path(inventory).read_text())["tests"]
    output = Path(output).resolve()
    ids = [test["id"] for test in inventory]
    numbers = {name: i for i, name in enumerate(ids)}
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Only instrument declared configuration, excluding ubiquitous enum-member accesses.
    definitions = {}
    for file in (root / "sqlglot").rglob("*.py"):
        relative = file.relative_to(root).as_posix()
        module = relative[:-3].replace("/", ".").removesuffix(".__init__")
        tree = ast.parse(file.read_text(encoding="utf-8"))

        def collect(nodes, prefix):
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    collect(node.body, prefix + [node.name])
                if not prefix or not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                names = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in names:
                    if isinstance(target, ast.Name) and (
                        target.id.isupper() or target.id == "arg_types"
                    ):
                        definitions[(module, ".".join(prefix), target.id)] = (
                            relative,
                            node.lineno,
                        )

        collect(tree.body, [])
    attributes = {item[-1] for item in definitions}
    active = set()
    observed = defaultdict(set)
    keys = defaultdict(set)
    values = Values(active)
    builtins._br_config = values.dictionary
    visited = set()

    @functools.cache
    def owner(cls, name):
        for ancestor in cls.__mro__:
            if name in vars(ancestor):
                return definitions.get(
                    (ancestor.__module__, ancestor.__qualname__, name)
                )
        return None

    def read(obj, name):
        value = getattr(obj, name)
        if active:
            cls = obj if isinstance(obj, type) else type(obj)
            mark = owner(cls, name)
            if mark:
                visited.add(mark)
        return value

    # Track actual keys of direct named configuration dictionaries. The normal
    # dictionary lookup and exception behavior remain in the original code.
    def key_read(obj, name, key):
        if active and isinstance(key, (str, int)):
            mark = owner(obj if isinstance(obj, type) else type(obj), name)
            if mark:
                keys[(mark, str(key))].update(active)
        return key

    class Instrument(ast.NodeTransformer):
        def __init__(self, relative):
            self.relative = relative

        def visit_ClassDef(self, node):
            return instrument_config(self, node, self.relative)

        def visit_Attribute(self, node):
            self.generic_visit(node)
            if (
                isinstance(node.ctx, ast.Load)
                and node.attr in attributes
                and not (
                    isinstance(node.value, ast.Name)
                    and node.value.id in {"TokenType", "DType", "Dialects"}
                )
            ):
                return ast.copy_location(
                    ast.Call(
                        ast.Name("_br_read", ast.Load()),
                        [node.value, ast.Constant(node.attr)],
                        [],
                    ),
                    node,
                )
            return node

        def direct(self, target, key):
            # Duplicating a plain local name read has no user-defined side effects.
            if (
                isinstance(target, ast.Attribute)
                and target.attr in attributes
                and isinstance(target.value, ast.Name)
            ):
                return ast.copy_location(
                    ast.Call(
                        ast.Name("_br_key", ast.Load()),
                        [
                            ast.Name(target.value.id, ast.Load()),
                            ast.Constant(target.attr),
                            key,
                        ],
                        [],
                    ),
                    key,
                )
            return key

        def visit_Subscript(self, node):
            node.slice = (
                self.direct(node.value, node.slice)
                if not isinstance(node.slice, ast.Slice)
                else node.slice
            )
            return self.generic_visit(node)

        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                node.args[0] = self.direct(node.func.value, node.args[0])
            return self.generic_visit(node)

    builtins._br_read = read
    builtins._br_key = key_read
    original = SourceFileLoader.get_code

    def get_code(loader, name):
        if name == "sqlglot" or name.startswith("sqlglot."):
            path = loader.get_filename(name)
            tree = Instrument(Path(path).relative_to(root).as_posix()).visit(
                ast.parse(Path(path).read_bytes(), path)
            )
            return compile(
                ast.fix_missing_locations(tree), path, "exec", dont_inherit=True
            )
        return original(loader, name)

    SourceFileLoader.get_code = get_code

    scope_index = defaultdict(set)
    for name, number in numbers.items():
        parts = name.split("::")
        for length in range(len(parts) + 1):
            scope_index["::".join(parts[:length])].add(number)

    class Trace:
        @pytest.hookimpl(wrapper=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if fixturedef.scope == "function":
                return (yield)
            previous = active.copy()
            buffer = visited.copy()
            active.clear()
            active.update(scope_index.get(request.node.nodeid, previous))
            visited.clear()
            try:
                return (yield)
            finally:
                for mark in visited:
                    observed[mark].update(active)
                active.clear()
                active.update(previous)
                visited.clear()
                visited.update(buffer)

        def pytest_collection_modifyitems(self, items):
            items[:] = [item for item in items if item.nodeid in numbers]

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            active.add(numbers[item.nodeid])
            try:
                return (yield)
            finally:
                active.clear()
                for mark in visited:
                    observed[mark].add(numbers[item.nodeid])
                visited.clear()

    with patch.object(ProcessPoolExecutor, "__new__", serializing_pool):
        result = pytest.main(
            ["tests", "-q", "--tb=line", "-p", "no:cacheprovider"], plugins=[Trace()]
        )
    if result != 0:
        raise RuntimeError(result)

    def label(mark):
        return f"{mark[0]}:{mark[1]}"

    data = {
        "ids": ids,
        "access": {label(mark): sorted(indices) for mark, indices in observed.items()},
        "keys": {
            label(mark) + "\n" + key: sorted(indices)
            for (mark, key), indices in keys.items()
        },
    }
    data["dicts"] = {
        mark: {k: sorted(v) for k, v in observations.items()}
        for mark, observations in values.seen.items()
        if mark not in values.unknown
    }
    with gzip.open(output, "wt", encoding="utf-8") as out:
        json.dump(data, out, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
