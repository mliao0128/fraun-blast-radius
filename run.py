"""Score-directed regression selection for the RWX Blast Radius track."""

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

import fixture_cases
import fixture_moves
from module_check import imports_cleanly
from changes import parse_patch
from probe import TestReplay
from schedule import fill_spare, pack
from select_tests import Impact


def charged():
    return sum(
        sum(resource.getrusage(w)[:2])
        for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/upstream"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    if hasattr(os, "sched_getaffinity"):
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed[:2])
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
    deadline = start + min(145, max(20, len(patches) * 2.5))
    executor_tests = {n for n, name in nodeid.items() if name in fixture_cases.TESTS}
    for p in patches:
        t = time.monotonic()
        if args.diagnostics:
            cpu_before = charged()
        candidates = set(ms)
        changes = []
        try:
            patch_text = p.read_text(encoding="utf-8")
            move = (
                fixture_moves.candidates(patch_text, source, impact)
                if "rename from " in patch_text
                else None
            )
            if move is None:
                changes = parse_patch(patch_text, source)
                candidates, can_replay = impact.select(changes)
            else:
                changes, candidates, can_replay = [], move, False
                impact.initializations.clear()
            if impact.initializations and not imports_cleanly(
                source, changes, impact.initializations, deadline - time.monotonic()
            ):
                for path in impact.initializations:
                    candidates.update(impact.imported(path))
                can_replay = False
            before = len(candidates)
            case_plan = {}
            for n in (
                candidates.intersection(executor_tests)
                if changes and can_replay
                else ()
            ):
                cases = fixture_cases.plan(changes, nodeid[n])
                if cases == []:
                    candidates.remove(n)
                elif cases and len(cases) <= 3 and not args.no_replay:
                    case_plan[n] = cases
            if case_plan:
                candidates -= replay.check(
                    changes,
                    sorted(case_plan),
                    min(3.2, deadline - time.monotonic()),
                    case_plan,
                )
            fixture_readers = set()
            for change in changes:
                if not change.path.endswith(".py"):
                    fixture_readers.update(
                        impact.from_pool(impact.reads.get(change.path), ms)
                    )
            # Spawning pool workers usually costs more CPU than pruning these
            # short recorded tests can save. Keep them in the simulated schedule.
            eligible = candidates - fixture_readers - impact.pool_tests
            total = sum(ms[i] for i in eligible)
            possible_gain = pack(candidates, ms)[0] - pack(candidates - eligible, ms)[0]
            if (
                can_replay
                and not args.no_replay
                and eligible
                and total <= 1250
                and possible_gain > 350
            ):
                ordered = sorted(eligible, key=lambda n: nodeid[n])
                budget = min(2.2, deadline - time.monotonic())
                candidates -= replay.check(changes, ordered, budget)
        except Exception as exc:
            print(
                f"{p.stem}: conservative fallback ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            candidates = set(ms)
            before = len(ms)
        cost, workers = pack(candidates, ms)
        if len(workers) > 1 and len(candidates) < len(ms):
            optional = set()
            for change in changes:
                if change.path.endswith(".py"):
                    optional.update(impact.imported(change.path))
            fill_spare(
                workers, optional - candidates, ms, min(240, 100000 // len(patches))
            )
        if args.diagnostics:
            print(
                f"{p.stem} initial={before} selected={sum(map(len, workers))} cost={cost / 1000:.3f} cpu={charged() - cpu_before:.3f} wall={time.monotonic() - t:.3f}",
                file=sys.stderr,
            )
        print(
            json.dumps({"commit": p.stem, "workers": workers}, separators=(",", ":")),
            flush=True,
        )


if __name__ == "__main__":
    main()
