"""Select invocations of a single changed callable in a class dispatch dictionary."""
import ast
import functools
import gzip
import json
from pathlib import Path


def entries(tree):
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for definition in cls.body:
            if not isinstance(definition, (ast.Assign, ast.AnnAssign)):
                continue
            targets = definition.targets if isinstance(definition, ast.Assign) else [definition.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            table = definition.value
            if not isinstance(table, ast.Dict):
                continue
            for i, (key, value) in enumerate(zip(table.keys, table.values)):
                if key is not None and isinstance(value, (ast.Lambda, ast.Call)):
                    yield table, i, value


@functools.cache
def _profile():
    path = Path(__file__).with_name("dispatch.json.gz")
    if not path.exists():
        return {}, []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    return data["files"], data["ids"]


def same_factory_argument(a, b):
    # A changed expression evaluated at import could affect the whole module.
    if ast.dump(a) == ast.dump(b):
        return True
    return isinstance(a, ast.Constant) and isinstance(b, ast.Constant) and type(a.value) is type(b.value)


def compatible(a, b):
    if isinstance(a, ast.Lambda) and isinstance(b, ast.Lambda):
        return ast.dump(a.args) == ast.dump(b.args)
    if not isinstance(a, ast.Call) or not isinstance(b, ast.Call):
        return False
    return (ast.dump(a.func) == ast.dump(b.func) and len(a.args) == len(b.args)
            and all(same_factory_argument(x, y) for x, y in zip(a.args, b.args))
            and len(a.keywords) == len(b.keywords)
            and all(x.arg == y.arg and same_factory_argument(x.value, y.value)
                    for x, y in zip(a.keywords, b.keywords)))


def candidates(change):
    """Return test node IDs only when a profiled value is the entire AST change."""
    files, ids = _profile()
    sites = files.get(change.path)
    if not sites or not change.new or not change.edits:
        return None
    lo = min(e.start + 1 for e in change.edits)
    hi = max(e.start + max(1, len(e.removed)) for e in change.edits)
    possible = {mark for mark, (end, _) in sites.items() if int(mark.split(":")[0]) <= lo and hi <= end}
    if not possible:
        return None
    old_tree, new_tree = ast.parse(change.old), ast.parse(change.new)
    old_entries, new_entries = list(entries(old_tree)), list(entries(new_tree))
    if len(old_entries) != len(new_entries):
        return None
    for (table, i, old), (_, _, new) in zip(old_entries, new_entries):
        mark = f"{old.lineno}:{old.col_offset}"
        if mark not in possible or ast.dump(old) == ast.dump(new) or not compatible(old, new):
            continue
        table.values[i] = new
        try:
            if ast.dump(old_tree) == ast.dump(new_tree):
                return {ids[n] for n in sites[mark][1]}
        finally:
            table.values[i] = old
    return None
