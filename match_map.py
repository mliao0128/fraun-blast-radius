"""Infer parser-match dependence from the lookahead observed on the pinned tests."""

import ast
import functools
import gzip
import io
import json
from pathlib import Path
import tokenize


def literals(call):
    if not isinstance(call.func, ast.Attribute) or not isinstance(
        call.func.value, ast.Name
    ):
        return None
    method = call.func.attr
    if method == "_match_text_seq":
        nodes = call.args
    elif (
        method == "_match_texts"
        and len(call.args) == 1
        and isinstance(call.args[0], (ast.Tuple, ast.Set, ast.List))
    ):
        nodes = call.args[0].elts
    else:
        return None
    if not nodes or not all(
        isinstance(x, ast.Constant)
        and type(x.value) is str
        and x.lineno == x.end_lineno
        for x in nodes
    ):
        return None
    if any(kw.arg != "advance" for kw in call.keywords):
        return None
    return method, nodes


@functools.cache
def _load():
    file = Path(__file__).with_name("match_tokens.json.gz")
    if not file.exists():
        return {}, [], []
    with gzip.open(file, "rt", encoding="utf8") as handle:
        data = json.load(handle)
    lookup = {}
    for site in data["sites"]:
        path, method, words, positions, observations, uncertain = site
        for index, (line, start, end) in enumerate(positions):
            lookup.setdefault((path, line), []).append((site, index, start, end))
    return lookup, [int(v, 16) for v in data["pools"]], data["ids"]


def candidates(change):
    if len(change.edits) != 1:
        return None
    edit = change.edits[0]
    if len(edit.removed) != 1 or len(edit.added) != 1:
        return None
    lookup, pools, ids = _load()
    old_line = edit.removed[0].encode()
    new_line = edit.added[0].encode()
    for site, changed, start, end in lookup.get((change.path, edit.start + 1), []):
        prefix, suffix = old_line[:start], old_line[end:]
        if not new_line.startswith(prefix) or not new_line.endswith(suffix):
            continue
        middle = new_line[start : len(new_line) - len(suffix)].strip()
        try:
            tokens = tokenize.generate_tokens(io.StringIO(middle.decode()).readline)
            first = next(tokens)
            if first.type != tokenize.STRING or first.string.encode() != middle:
                continue
            expr = ast.parse(middle, mode="eval").body
            previous = ast.literal_eval(old_line[start:end].decode())
        except (
            SyntaxError,
            ValueError,
            UnicodeError,
            tokenize.TokenError,
            StopIteration,
        ):
            continue
        _, method, words, _, observations, uncertain = site
        if (
            previous != words[changed]
            or not isinstance(expr, ast.Constant)
            or type(expr.value) is not str
        ):
            continue
        updated = expr.value
        if updated == previous:
            return set()
        candidates_mask = pools[uncertain]
        if method == "_match_texts":
            new_words = words.copy()
            new_words[changed] = updated
            changed_membership = set(words) ^ set(new_words)
            for _, token, group in observations:
                if token in changed_membership:
                    candidates_mask |= pools[group]
        else:
            # A later mismatch implies the original word at this offset matched.
            # Keep partial-prefix differences because _retreat can restore comments.
            for matched, token, group in observations:
                if matched > changed or (matched == changed and token == updated):
                    candidates_mask |= pools[group]
        result = set()
        while candidates_mask:
            bit = candidates_mask & -candidates_mask
            result.add(ids[bit.bit_length() - 1])
            candidates_mask ^= bit
        return result
    return None
