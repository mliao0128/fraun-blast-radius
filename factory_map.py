"""Use actual closure consumers when just a proven factory's string argument changes."""

import ast
import functools
import gzip
import json
from pathlib import Path


@functools.cache
def _profile():
    path = Path(__file__).with_name("factories.json.gz")
    if not path.is_file():
        return {}, []
    with gzip.open(path, "rt") as source:
        result = json.load(source)
    return result["files"], result["ids"]


def candidates(change):
    files, ids = _profile()
    sites = files.get(change.path)
    if not sites or not change.new or len(change.edits) != 1:
        return None
    edit = change.edits[0]
    line = edit.start + 1
    if not any(key.startswith(str(line) + ":") for key in sites):
        return None
    if len(edit.removed) != 1 or len(edit.added) != 1:
        return None
    original, replacement = edit.removed[0], edit.added[0]
    indentation = original[: len(original) - len(original.lstrip())]
    if indentation != replacement[: len(replacement) - len(replacement.lstrip())]:
        return None
    try:
        before, after = ast.parse(original.lstrip()), ast.parse(replacement.lstrip())
    except SyntaxError:
        return None
    for tree in (before, after):
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
            return None
        call = tree.body[0].value
        if (
            not isinstance(call, ast.Call)
            or call.keywords
            or len(call.args) != 1
            or not isinstance(call.args[0], ast.Constant)
            or type(call.args[0].value) is not str
        ):
            return None
        if tree is before:
            old = call
        else:
            new = call
    site = f"{line}:{len(indentation.encode('utf-8')) + old.col_offset}"
    if site not in sites or old.args[0].value == new.args[0].value:
        return None
    old.args[0].value = new.args[0].value
    if ast.dump(before) != ast.dump(after):
        return None
    return {ids[i] for i in sites[site]}
