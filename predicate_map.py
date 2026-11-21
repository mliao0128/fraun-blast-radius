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


def candidates(path, lineno, before, after):
    """Return potentially affected baseline test node IDs, or None to use coverage."""
    sites, ids, incomplete = _profile()
    for prefix, old, suffix, observations in sites.get(path, {}).get(str(lineno), []):
        if (prefix + old + suffix).strip() != before.strip():
            continue
        for new in ("is", "is not", "==", "!=", "<", "<=", ">", ">="):
            if new == old or (prefix + new + suffix).strip() != after.strip():
                continue
            result = {ids[i] for i in incomplete}
            for states, indices in observations.items():
                mask = int(states)
                while mask:
                    bit = mask & -mask
                    mask ^= bit
                    state = bit.bit_length() - 1
                    a, b = _value(state, old), _value(state, new)
                    if a is None or b is None or a != b:
                        result.update(ids[i] for i in indices)
                        break
            return result
    return None
