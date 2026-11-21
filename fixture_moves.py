"""Record baseline fixture-path dependencies and select tests for plain Git renames.

Build the map by running the pinned suite on Linux under strace -ff -qq -yy -ttt
-e trace=%file,getdents,getdents64,io_uring_enter. See the CLI at the end.
"""

import bisect
from collections import defaultdict
from contextlib import contextmanager
import functools
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time

FIXTURES = "tests/fixtures/"
REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
MAP = Path(__file__).with_name("fixture_moves.json.gz")


def collect(source, inventory, output):
    import pytest

    docs = json.loads(inventory.read_text())
    ids = [row["id"] for row in docs["tests"]]
    root = source.resolve()
    revision = (root / ".git/HEAD").read_text().strip()
    if revision != REVISION:
        raise ValueError("Expected the detached pinned commit")
    output = output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    clock = []
    active = ""
    outcomes = {}
    scope_members = defaultdict(list)
    for ident in ids:
        parts = ident.split("::")
        for length in range(len(parts) + 1):
            scope_members["::".join(parts[:length])].append(ident)

    @contextmanager
    def set_scope(scope):
        nonlocal active
        prior = active
        active = scope if scope in scope_members else ""
        clock.append([time.time_ns() // 1000, active])
        try:
            yield
        finally:
            active = prior
            clock.append([time.time_ns() // 1000, active])

    class Plugin:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            with set_scope(item.nodeid):
                return (yield)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_make_collect_report(self, collector):
            with set_scope(collector.nodeid):
                return (yield)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            with set_scope(request.node.nodeid):
                return (yield)

        def pytest_runtest_logreport(self, report):
            key = report.nodeid
            if report.failed or report.skipped or report.when == "call":
                outcomes[key] = (
                    report.outcome if outcomes.get(key) != "failed" else "failed"
                )

    files = sorted({row["file"] for row in docs["tests"]})
    result = pytest.main(
        ["-q", "--tb=line", "-p", "no:cacheprovider", *files], plugins=[Plugin()]
    )
    if result or any(outcomes.get(ident) != "passed" for ident in ids):
        raise RuntimeError("Baseline suite failed")
    output.write_text(
        json.dumps(
            {
                "scope_changes": clock,
                "scope_members": scope_members,
                "root": str(root),
                "revision": REVISION,
            }
        )
    )


# A header without quoting is unambiguous for this conservative path alphabet.
DATA_PATH = re.compile(
    r"tests/fixtures/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-][A-Za-z0-9._-]*"
)
HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")


def renames(text):
    lines = text.splitlines()
    if not lines or len(lines) % 4:
        return None
    pairs = []
    for index in range(0, len(lines), 4):
        header, similarity, old_line, new_line = lines[index : index + 4]
        match = HEADER.fullmatch(header)
        if match is None:
            return None
        old, new = match.groups()
        if (
            similarity != "similarity index 100%"
            or old_line != f"rename from {old}"
            or new_line != f"rename to {new}"
            or old == new
        ):
            return None
        if any(not DATA_PATH.fullmatch(path) for path in (old, new)):
            return None
        old_suffix = PurePosixPath(old).suffixes
        if old_suffix != PurePosixPath(new).suffixes or old_suffix not in (
            [".sql"],
            [".json"],
            [".csv", ".gz"],
        ):
            return None
        pairs.append((old, new))
    paths = [path for pair in pairs for path in pair]
    return pairs if len(set(paths)) == len(paths) else None


@functools.cache
def load():
    if not MAP.is_file():
        return {}
    with gzip.open(MAP, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    data["inventory"] = set(data["inventory"])
    return data


def candidates(text, source, impact):
    pairs = renames(text)
    if pairs is None:
        return None
    data = load()
    if data.get("revision") != REVISION or data.get("opaque"):
        return None
    # Git does not retain empty directories. Their disappearance would also
    # change ancestor enumeration and directory existence tests.
    old_names = {old for old, _ in pairs}
    old_dirs = {PurePosixPath(old).parent for old in old_names}
    new_dirs = {PurePosixPath(new).parent for _, new in pairs}
    for directory in old_dirs - new_dirs:
        if not (source / directory).is_dir() or all(
            str(directory / member.name) in old_names
            for member in (source / directory).iterdir()
        ):
            return None
    selected = set(impact.unmapped)
    selected.update(
        number
        for ident, number in impact.number.items()
        if ident not in data["inventory"]
    )
    for old, new in pairs:
        old_path, new_path = source / old, source / new
        if (
            not old_path.is_file()
            or old_path.resolve() != old_path
            or new_path.exists()
            or not new_path.parent.is_dir()
            or new_path.resolve() != new_path
        ):
            return None
        if old not in data["paths"] and old not in impact.reads:
            return None
        ids = set(data["paths"].get(old, ())) | set(data["paths"].get(new, ()))
        for path in (old, new):
            selected.update(impact.from_pool(impact.reads.get(path)))
            ids.update(data["lists"].get(str(PurePosixPath(path).parent), ()))
        selected.update(impact.number[ident] for ident in ids if ident in impact.number)
    return selected


def summarize(timeline_path, trace_directory, output):
    # Each time window below covers all of a test's child processes. The baseline
    # runs tests serially. A class or package setup gets that entire scope's IDs.
    timeline = json.loads(timeline_path.read_text())
    root = timeline["root"]
    fixture_root = root + "/" + FIXTURES.rstrip("/")
    times = [time_us for time_us, _ in timeline["scope_changes"]]
    scopes = [scope for _, scope in timeline["scope_changes"]]
    members = timeline["scope_members"]
    inventory = set(members[""])
    paths = defaultdict(set)
    listings = defaultdict(set)
    opaque = False
    files = sorted(trace_directory.glob("process.*"))
    if not files or not times:
        raise ValueError("Missing trace or pytest timeline")

    # Include unsuccessful opens and existence probes. A new destination can turn
    # a negative lookup into a successful one. strace's -yy also resolves native
    # directory handles and relative openat paths that have no Python audit event.
    quoted = re.compile(r'"([^"\\]*)"')
    descriptor = re.compile(r"<(/[^<>]+)>")
    timestamp = re.compile(r"^(\d+)\.(\d{6})\s+(\w+)\(")
    cwds = {root}
    for file in files:
        with file.open(errors="replace") as log:
            for line in log:
                if "chdir(" not in line or " = 0" not in line:
                    continue
                match = re.search(r'(?:chdir\("([^"]+)"|fchdir\(\d+<([^>]+)>)', line)
                if not match:
                    opaque = True
                    continue
                path = match.group(1) or match.group(2)
                if not path.startswith("/"):
                    opaque = True
                else:
                    cwds.add(path)
    path_cache = {}
    for file in files:
        with file.open(errors="replace") as log:
            for line in log:
                if "io_uring_enter(" in line and " = -1 " not in line:
                    opaque = True
                at = timestamp.match(line)
                if at is None:
                    continue
                call = at.group(3)
                if call == "getdents64" or call == "getdents":
                    names = descriptor.findall(line)
                    mapping = listings
                else:
                    # Absolute names may occur as arguments or as resolved fds.
                    # Unfinished partial lines are avoided using one logfile per PID.
                    arguments = line[at.end() :].split(" = ", 1)[0]
                    resolved = descriptor.findall(arguments)
                    names = []
                    for literal in quoted.findall(arguments):
                        if literal.startswith("/"):
                            names.append(literal)
                        elif literal:
                            bases = (
                                [resolved[0]]
                                if resolved and arguments.startswith(("AT_FDCWD<",))
                                else cwds
                            )
                            if resolved and arguments.startswith(tuple("0123456789")):
                                bases = [resolved[0]]
                            for base in bases:
                                if (
                                    base.startswith(fixture_root)
                                    or "fixture" in literal
                                    or ".." in literal
                                ):
                                    names.append(base + "/" + literal)
                    mapping = paths
                for name in names:
                    if name not in path_cache:
                        norm = os.path.normpath(name)
                        rel = (
                            norm[len(root) + 1 :]
                            if norm == fixture_root
                            or norm.startswith(fixture_root + "/")
                            else None
                        )
                        path_cache[name] = rel
                    rel = path_cache[name]
                    if rel is None:
                        continue
                    t = int(at.group(1)) * 1_000_000 + int(at.group(2))
                    index = bisect.bisect_right(times, t) - 1
                    scope = scopes[index] if index >= 0 else ""
                    mapping[rel].add(scope)

    def expand(events):
        return {
            rel: sorted(set().union(*(members[scope] for scope in group)))
            for rel, group in sorted(events.items())
        }

    packed = {
        "revision": timeline["revision"],
        "inventory": sorted(inventory),
        "paths": expand(paths),
        "lists": expand(listings),
        "opaque": opaque,
    }
    with (
        output.open("wb") as target,
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as stream,
    ):
        stream.write(json.dumps(packed, separators=(",", ":")).encode())


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] not in ("collect", "summarize"):
        raise SystemExit(
            "fixture_moves.py collect CHECKOUT INVENTORY TIMELINE | summarize TIMELINE TRACE_DIR MAP"
        )
    (collect if sys.argv[1] == "collect" else summarize)(
        *(Path(arg) for arg in sys.argv[2:])
    )
