"""Profile literal parser match calls on an unmodified checkout with the official catalog."""

import ast
import builtins
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from build_map import Recorder, encode
from config_profile import serializing_pool
from match_map import literals


class Tokens(Recorder):
    def __init__(self, root, ids):
        super().__init__(root, ids)
        self.sites = []
        self.known = []
        self.uncertain = []
        self.shared = set()
        self.pooled = set()
        self.parsers = None

    def switch(self, mask):
        self.active = mask

    def register(self, path, method, nodes):
        index = len(self.sites)
        self.sites.append(
            (
                path,
                method,
                [x.value for x in nodes],
                [(x.lineno, x.col_offset, x.end_col_offset) for x in nodes],
            )
        )
        self.known.append(defaultdict(int))
        self.uncertain.append(0)
        return index

    def observe(self, index, fn, *args, **kwargs):
        if not self.active:
            self.shared.add(index)
            return fn(*args, **kwargs)
        site = self.sites[index]
        if self.parsers is None:
            from sqlglot.parser import Parser, SENTINEL_NONE

            self.parsers = (
                Parser,
                SENTINEL_NONE,
                Parser._advance,
                Parser._retreat,
                {
                    name: getattr(Parser, name)
                    for name in ("_match_text_seq", "_match_texts")
                },
            )
        base, sentinel, advance, retreat, matchers = self.parsers
        obj = getattr(fn, "__self__", None)
        method = site[1]
        if (
            not isinstance(obj, base)
            or getattr(fn, "__func__", None) is not matchers[method]
            or type(obj)._advance is not advance
            or type(obj)._retreat is not retreat
        ):
            self.uncertain[index] |= self.active
            return fn(*args, **kwargs)
        try:
            token = obj._curr
            excluded = obj.TEXT_MATCH_EXCLUDED_TOKENS
            if method == "_match_texts":
                key = (-1, None if token.token_type in excluded else token.text.upper())
                expected = key[1] in args[0]
            else:
                words = site[2]
                start = obj._index
                if start < 0:
                    raise ValueError("negative parser cursor")
                for k, word in enumerate(words):
                    if k:
                        at = start + k
                        token = obj._tokens[at] if at < obj._tokens_size else sentinel
                    observed = (
                        None if token.token_type in excluded else token.text.upper()
                    )
                    if observed != word:
                        key, expected = (k, observed), False
                        break
                else:
                    key, expected = (len(words), None), True
        except (AttributeError, TypeError, IndexError, ValueError):
            self.uncertain[index] |= self.active
            return fn(*args, **kwargs)
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            self.uncertain[index] |= self.active
            raise
        if result is not expected:
            self.uncertain[index] |= self.active
        else:
            self.known[index][key] |= self.active
        return result


def main(root, catalog, output):
    root = Path(root).resolve()
    ids = [r["id"] for r in json.loads(Path(catalog).read_text())["tests"]]
    output = Path(output).resolve()
    if (
        root / ".git/HEAD"
    ).read_text().strip() != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Expected the pinned source checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    trace = Tokens(root, ids)
    builtins._br_observe_token_match = trace.observe
    original = SourceFileLoader.get_code

    class Rewrite(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path

        def visit_Call(self, call):
            self.generic_visit(call)
            info = literals(call)
            if info:
                method, nodes = info
                index = trace.register(self.path, method, nodes)
                call.args = [ast.Constant(index), call.func, *call.args]
                call.func = ast.Name("_br_observe_token_match", ast.Load())
            return call

    def get_code(loader, name):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return original(loader, name)
        path = Path(loader.get_filename(name))
        src = path.read_bytes()
        if b"_match_text" not in src:
            return original(loader, name)
        tree = Rewrite(path.relative_to(root).as_posix()).visit(
            ast.parse(src, str(path))
        )
        return compile(
            ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True
        )

    def pool(cls, *args, **kwargs):
        instance = serializing_pool(cls, *args, **kwargs)
        submit = instance.submit

        def tracked(*args, **kwargs):
            trace.pooled.add(trace.active)
            return submit(*args, **kwargs)

        instance.submit = tracked
        return instance

    with (
        patch.object(SourceFileLoader, "get_code", get_code),
        patch.object(ProcessPoolExecutor, "__new__", pool),
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
            f"Profile incomplete: pytest={result}, {len(trace.status)} tests"
        )
    groups = {}

    def intern(mask):
        if mask not in groups:
            groups[mask] = len(groups)
        return groups[mask]

    entries = []
    merged = {}
    for i, site in enumerate(trace.sites):
        path, method, words, spans = site
        key = path, method, tuple(words), tuple(spans)
        merged.setdefault(key, []).append(i)
    for site, indices in merged.items():
        if any(i in trace.shared for i in indices):
            continue
        combined = defaultdict(int)
        unknown = 0
        for i in indices:
            unknown |= trace.uncertain[i]
            for k, mask in trace.known[i].items():
                combined[k] |= mask
        obs = [[k, word, intern(mask)] for (k, word), mask in combined.items()]
        entries.append([*site, obs, intern(unknown)])
    encode({"ids": ids, "pools": [hex(n) for n in groups], "sites": entries}, output)
    print(
        f"{len(entries)} call sites; {len(trace.pooled)} pooled test contexts; "
        f"{len(trace.shared)} shared exclusions; {sum(len(r) for r in trace.known)} token groups"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
