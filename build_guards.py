"""Observe side-effect-free truth tests and explicit reads of pristine Python sources."""

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


class Guards(Recorder):
    def switch(self, mask):
        self.active = mask

    def capture_source(self, event, args):
        if (
            not self.active
            or event != "open"
            or not isinstance(args[0], str)
            or not args[0].endswith(".py")
        ):
            return
        path = self.relative(os.path.abspath(args[0]))
        if not path:
            return
        frame = sys._getframe(1)
        while frame:
            mod = frame.f_globals.get("__name__", "")
            if (
                mod.startswith(("importlib", "_frozen_importlib"))
                or frame.f_code.co_name == "get_code"
            ):
                return
            if mod == "sqlglot" or mod.startswith(("sqlglot.", "tests.")):
                self.reads[path] |= self.active
                return
            frame = frame.f_back


def main(root, inventory, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    ids = [t["id"] for t in json.loads(Path(inventory).read_text())["tests"]]
    os.chdir(root)
    sys.path.insert(0, str(root))
    trace = Guards(root, ids)
    safe, uncertain = defaultdict(int), defaultdict(int)
    basic = {
        type(None),
        bool,
        str,
        bytes,
        int,
        float,
        list,
        dict,
        set,
        tuple,
        frozenset,
    }

    @functools.cache
    def transparent(cls, name):
        if cls.__getattribute__ is not object.__getattribute__:
            return False
        for base in cls.__mro__:
            if name in vars(base):
                slot = vars(base)[name]
                return isinstance(slot, types.MemberDescriptorType) or not hasattr(
                    type(slot), "__get__"
                )
        return not any("__getattr__" in vars(base) for base in cls.__mro__)

    def observe(site, obj, name):
        try:
            value = getattr(obj, name)
        except Exception:
            uncertain[site] |= trace.active
            raise
        if trace.active:
            if type(value) in basic and transparent(type(obj), name):
                safe[site] |= trace.active
            else:
                uncertain[site] |= trace.active
        return value

    class Instrument(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path

        def visit_If(self, node):
            self.generic_visit(node)
            val = node.test
            if (
                not node.orelse
                and isinstance(val, ast.Attribute)
                and isinstance(val.value, ast.Name)
            ):
                site = f"{self.path}:{node.lineno}"
                node.test = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_guard_value", ast.Load()),
                        [ast.Constant(site), val.value, ast.Constant(val.attr)],
                        [],
                    ),
                    val,
                )
            return node

    raw = SourceFileLoader.get_code

    def get_code(loader, name):
        if name == "sqlglot" or name.startswith("sqlglot."):
            path = Path(loader.get_filename(name))
            tree = Instrument(path.relative_to(root).as_posix()).visit(
                ast.parse(path.read_bytes(), str(path))
            )
            return compile(
                ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True
            )
        return raw(loader, name)

    builtins._br_guard_value = observe
    sys.addaudithook(trace.capture_source)
    with (
        patch.object(SourceFileLoader, "get_code", get_code),
        patch.object(ProcessPoolExecutor, "__new__", serializing_pool),
    ):
        status = pytest.main(
            ["-q", "--tb=line", "-p", "no:cacheprovider", "tests"], plugins=[trace]
        )
    if status != 0 or len(trace.status) != len(ids):
        raise RuntimeError("baseline test suite did not pass")

    def numbered(mask):
        return [i for i in range(len(ids)) if mask & (1 << i)]

    encode(
        {
            "ids": ids,
            "readers": {p: numbered(mask) for p, mask in trace.reads.items()},
            "pure": {
                site: numbered(mask & ~uncertain[site]) for site, mask in safe.items()
            },
        },
        output,
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
