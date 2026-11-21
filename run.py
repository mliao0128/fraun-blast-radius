"""Score-directed regression selection for the RWX Blast Radius track."""
import argparse
import json
import fixture_cases
from module_check import imports_cleanly
import sys
import time
from pathlib import Path

from changes import parse_patch
from probe import TestReplay
from schedule import pack
from select_tests import Impact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/upstream"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    start = time.monotonic()
    source = args.source.resolve()
    inventory = json.loads((args.data / "tests.json").read_bytes())["tests"]
    ms = {test["number"]: round(1000 * test["seconds"]) for test in inventory}
    nodeid = {test["number"]: test["id"] for test in inventory}
    impact = Impact(inventory)
    replay = TestReplay(source, inventory)
    patches = sorted((args.data / "commits").glob("*.patch"))
    # Both official run limits exceed this wall-clock reserve. Keep capacity for
    # parsing and returning a valid conservative selection for all remaining edits.
    deadline = start + min(145, max(20, len(patches)*2.5))
    executor_tests = {n for n, name in nodeid.items() if name in fixture_cases.TESTS}
    for p in patches:
        t = time.monotonic()
        candidates = set(ms)
        try:
            changes = parse_patch(p.read_text(encoding="utf-8"), source)
            candidates, can_replay = impact.select(changes)
            if impact.initializations and not imports_cleanly(source, changes, impact.initializations, deadline - time.monotonic()):
                for path in impact.initializations:
                    candidates.update(impact.imported(path))
                can_replay = False
            before = len(candidates)
            case_plan = {}
            for n in candidates.intersection(executor_tests):
                cases = fixture_cases.plan(changes, nodeid[n])
                if cases == []:
                    candidates.remove(n)
                elif cases and len(cases) <= 3 and not args.no_replay:
                    case_plan[n] = cases
            if case_plan:
                candidates -= replay.check(changes, sorted(case_plan), min(3.2, deadline - time.monotonic()), case_plan)
            fixture_readers = set()
            for change in changes:
                if not change.path.endswith(".py"):
                    fixture_readers.update(impact.from_pool(impact.reads.get(change.path), ms))
            eligible = candidates - fixture_readers
            total = sum(ms[i] for i in eligible)
            possible_gain = pack(candidates, ms)[0] - pack(candidates - eligible, ms)[0]
            if can_replay and not args.no_replay and eligible and total <= 1250 and possible_gain > 350:
                ordered = sorted(eligible, key=lambda n: nodeid[n])
                budget = min(2.2, deadline - time.monotonic())
                candidates -= replay.check(changes, ordered, budget)
        except Exception as exc:
            print(f"{p.stem}: conservative fallback ({type(exc).__name__}: {exc})", file=sys.stderr)
            candidates = set(ms)
            before = len(ms)
        cost, workers = pack(candidates, ms)
        if args.diagnostics:
            print(f"{p.stem} initial={before} selected={len(candidates)} cost={cost/1000:.3f} wall={time.monotonic()-t:.3f}", file=sys.stderr)
        print(json.dumps({"commit": p.stem, "workers": workers}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
