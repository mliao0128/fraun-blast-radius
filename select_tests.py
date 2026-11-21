"""Compose baseline execution, import, fixture and binding evidence for each edit."""
import ast
import functools
import gzip
import json
import re
from io import BytesIO
from tokenize import detect_encoding
from collections import defaultdict
from pathlib import Path

import dispatch_map
import predicate_map

HERE = Path(__file__).resolve().parent
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def unpack(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


@functools.lru_cache(maxsize=48)
def parsed(source):
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    # Only statements and lambdas can establish the execution scope.
    spans = sorted((n for n in parents if isinstance(n, (ast.stmt, ast.Lambda))),
                   key=lambda n: (n.end_lineno - n.lineno, -n.col_offset))
    return tree, parents, spans


class Impact:
    def __init__(self, inventory):
        m = unpack(HERE / "impact.json.gz")
        self.number = {test["id"]: test["number"] for test in inventory}
        self.all = set(self.number.values())
        known_ids = set(m["nodeids"])
        self.unmapped = {v for k, v in self.number.items() if k not in known_ids}
        self.nodeids = m["nodeids"]
        self.pools = [set(self.number[self.nodeids[i]] for i in pool if self.nodeids[i] in self.number)
                      for pool in m["pools"]]
        self.files = {p: {int(i): group for i, group in rows.items()} for p, rows in m["files"].items()}
        self.reads = m["reads"]
        self.imports = m["imports"]
        self.by_file = defaultdict(set)
        for t in inventory:
            self.by_file[t["file"]].add(t["number"])
        self.initializations = set()
        self.ref_path = HERE / "refs.json.gz"
        self.refs = None
        self.bindings = unpack(HERE / "bindings.json.gz") if (HERE / "bindings.json.gz").is_file() else {}
        self.bind_ids = [self.number.get(n) for n in self.bindings.get("ids", [])]

    def from_pool(self, index, fallback=()):
        return set(fallback) if index is None else self.pools[index].copy()

    def lookup_lines(self, path, lines):
        rows = self.files.get(path, {})
        hits = set()
        for line in lines:
            if line in rows:
                hits.update(self.pools[rows[line]])
        return hits

    def imported(self, path):
        return self.from_pool(self.imports.get(path), self.by_file.get(path, self.all))

    def named_reads(self, name):
        if self.refs is None:
            self.refs = unpack(self.ref_path)
        hits = set()
        for file, lines in self.refs.get(name, {}).items():
            hits.update(self.lookup_lines(file, lines))
        return hits

    def dict_candidates(self, change, old_tree, spans, new_tree):
        if len(change.edits) != 1:
            return None
        edit = change.edits[0]
        if not edit.removed or not edit.added:
            return None
        def assignment(nodes, size):
            return next((n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Dict)
                         and n.lineno <= edit.start + 1 and n.end_lineno >= edit.start + size), None)
        old = assignment(spans, len(edit.removed))
        if old is None:
            return None
        current = assignment(ast.walk(new_tree), len(edit.added))
        mark = f"{change.path}:{old.lineno}"
        reads = self.bindings.get("dicts", {}).get(mark)
        if current is None or reads is None:
            return None
        def name(node):
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            return ast.dump(target)
        try:
            before = ast.literal_eval(old.value)
            after = ast.literal_eval(current.value)
        except (ValueError, TypeError):
            return None
        if name(old) != name(current) or list(before) != list(after):
            return None
        original_value = old.value
        try:
            old.value = current.value
            if ast.dump(old_tree) != ast.dump(new_tree):
                return None
        finally:
            old.value = original_value
        changed = [k for k in before if type(before[k]) is not type(after[k]) or repr(before[k]) != repr(after[k])]
        indices = set(reads.get("*", ()))
        for key in changed:
            indices.update(reads.get(key, ()))
        return {self.bind_ids[i] for i in indices if self.bind_ids[i] is not None}

    def statement(self, path, parents, spans, line, direct, insertion):
        stmt = next((s for s in spans if s.lineno <= line <= s.end_lineno), None)
        if stmt is None:
            return self.imported(path)
        chain = [stmt]
        while chain[-1] in parents:
            chain.append(parents[chain[-1]])
        func = next((n for n in chain if isinstance(n, FUNCTION)), None)
        is_test = path in self.by_file
        # A literal/default/decorator on the function header is evaluated at definition.
        if func and (isinstance(func, ast.Lambda) or line >= func.body[0].lineno):
            if is_test and getattr(func, "name", "").startswith("test_"):
                return {number for name, number in self.number.items()
                        if name.split("::")[-1] == func.name and name.split("::")[0] == path} | direct
            if insertion:
                return self.lookup_lines(path, range(func.lineno, func.end_lineno + 1))
            if line in self.files.get(path, {}):
                return direct
            if isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try, ast.With, *FUNCTION)):
                return self.lookup_lines(path, [stmt.lineno])
            return self.lookup_lines(path, range(stmt.lineno, stmt.end_lineno + 1))
        self.initializations.add(path)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            binding = f"{path}:{stmt.lineno}"
            bind_indices = self.bindings.get("access", {}).get(binding, [])
            binds = {self.bind_ids[i] for i in bind_indices if self.bind_ids[i] is not None}
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [n.id for n in targets if isinstance(n, ast.Name)]
            # Scalars can be read from subclasses and inherited dialects. Locations
            # are tied to the defining class by the baseline attribute profiler.
            if binds and isinstance(stmt.value, (ast.Constant, ast.UnaryOp)):
                return binds | direct
            if names:
                reads = set.union(set(), *(self.named_reads(n) for n in names))
                cls = next((n for n in chain if isinstance(n, ast.ClassDef)), None)
                if cls and names == ["arg_types"]:
                    # The required/optional flags describe instances of this specific expression.
                    # Name uses also include construction and type inspection outside the defining file.
                    dispatch = self.named_reads(cls.name)
                    return dispatch | binds | direct
                if isinstance(stmt.value, ast.Dict):
                    return self.imported(path) | direct
                if binds:
                    return binds | direct
                if cls and path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")):
                    dialect = Path(path).stem
                    same = set()
                    for file, rows in self.files.items():
                        if file.startswith("sqlglot/") and Path(file).stem == dialect:
                            same.update(self.lookup_lines(file, rows))
                    same.update(self.by_file.get("tests/dialects/test_" + dialect + ".py", ()))
                    return direct | same | (reads & self.imported(path))
                return reads | direct if reads else self.imported(path)
        return self.imported(path)

    def select(self, changes):
        self.initializations.clear()
        selected = set(self.unmapped)
        probe = False
        for c in changes:
            if not c.path.endswith(".py"):
                selected.update(self.from_pool(self.reads.get(c.path), self.all))
                continue
            if c.new is None or not c.old:
                selected.update(self.imported(c.path))
                continue
            try:
                if detect_encoding(BytesIO(c.old.encode()).readline)[0] != detect_encoding(BytesIO(c.new.encode()).readline)[0]:
                    selected.update(self.imported(c.path))
                    continue
                old, parents, spans = parsed(c.old)
                new = ast.parse(c.new)
                compile(new, c.path, "exec")
                if all(not text.strip() or text.lstrip().startswith("#")
                       for e in c.edits for text in e.removed + e.added):
                    if ast.dump(old) == ast.dump(new):
                        raw_readers = self.from_pool(self.reads.get(c.path))
                        selected.update(raw_readers)
                        probe |= bool(raw_readers)
                        continue
            except (SyntaxError, ValueError):
                selected.update(self.imported(c.path))
                continue
            probe = True
            selected.update(self.from_pool(self.reads.get(c.path)))
            dispatch = dispatch_map.candidates(c)
            if dispatch is not None:
                self.initializations.add(c.path)
                selected.update(self.number[n] for n in dispatch if n in self.number)
                continue
            by_key = self.dict_candidates(c, old, spans, new)
            if by_key is not None:
                self.initializations.add(c.path)
                selected.update(by_key)
                continue
            for edit in c.edits:
                old_lines = range(edit.start + 1, edit.start + len(edit.removed) + 1)
                if not edit.removed:
                    # Either side can be in another suite or start a different function.
                    old_lines = sorted({max(1, edit.start), min(len(c.old.splitlines()), edit.start + 1)})
                if len(edit.added) == len(edit.removed) == 1:
                    observed = predicate_map.candidates(c.path, edit.start + 1, edit.removed[0], edit.added[0])
                    if observed is not None:
                        direct = self.lookup_lines(c.path, old_lines)
                        selected.update(self.number[n] for n in observed if n in self.number and self.number[n] in direct)
                        continue
                for line in old_lines:
                    # Re-grouping short-circuit expressions can evaluate an operand
                    # even on inputs that previously skipped that source line.
                    if len(edit.removed) == len(edit.added) == 1:
                        if re.findall(r"\b(?:and|or)\b", edit.removed[0]) != re.findall(r"\b(?:and|or)\b", edit.added[0]):
                            exprs = [n for n in ast.walk(old) if isinstance(n, ast.BoolOp) and n.lineno <= line <= n.end_lineno]
                            for expr in exprs:
                                selected.update(self.lookup_lines(c.path, [expr.lineno]))
                    direct = self.lookup_lines(c.path, [line])
                    selected.update(self.statement(c.path, parents, spans, line, direct, not edit.removed))
        return selected, probe
