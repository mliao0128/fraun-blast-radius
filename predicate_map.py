"""Consult baseline comparison operand observations for a single operator substitution."""

import functools
import gzip
import json
from pathlib import Path


@functools.cache
def _profile():
    path = Path(__file__).with_name("predicates.json.gz")
    if not path.is_file():
        return {}, [], []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        profile = json.load(handle)
    return profile["sites"], profile["ids"], profile["incomplete"]


def _value(state, op):
    if state == 0:
        return None
    # Each raw observation records identity, equality, less-than, greater-than,
    # and whether the order relation was defined for both operand types.
    if op in ("is", "is not"):
        return bool(state & 1) == (op == "is")
    if op in ("==", "!="):
        return bool(state & 2) == (op == "==")
    if state & 16:
        return bool(state & {"<": 4, "<=": 6, ">": 8, ">=": 10}[op])
    return None


def _can_change(mask, old, new):
    while mask:
        bit = mask & -mask
        mask ^= bit
        state = bit.bit_length() - 1
        a, b = _value(state, old), _value(state, new)
        if a is None or b is None or a != b:
            return True
    return False


def candidates(path, lineno, before, after):
    """Return potentially affected and observed test IDs for a pure substitution."""
    sites, ids, incomplete = _profile()
    for row in sites.get(path, {}).get(str(lineno), []):
        prefix, old, suffix, observations, *shared = row
        if prefix + old + suffix != before:
            continue
        for new in ("is", "is not", "==", "!=", "<", "<=", ">", ">="):
            if new == old or prefix + new + suffix != after:
                continue
            # An invocation during collection or outside a known test can
            # affect later tests. Narrow only if its outcome is unchanged.
            if shared and _can_change(shared[0], old, new):
                return None
            result = {ids[i] for i in incomplete}
            observed = set()
            for states, indices in observations.items():
                names = {ids[i] for i in indices}
                observed.update(names)
                if _can_change(int(states), old, new):
                    result.update(names)
            return result, observed
    return None
