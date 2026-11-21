"""Build the clean pinned SQLGlot per-query statement coverage used by fixture_cases."""

import argparse
from collections import defaultdict
import functools
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys

from fixture_cases import replay, TESTS


class Trace:
    def __init__(self, root):
        self.root = str(root) + os.sep
        self.mon = sys.monitoring
        self.tool = 3
        self.active = 1
        self.rows = defaultdict(dict)
        self.mon.use_tool_id(self.tool, "SQL fixture query")
        self.mon.register_callback(self.tool, self.mon.events.LINE, self.line)
        self.mon.set_events(self.tool, self.mon.events.LINE)

    @functools.cache
    def relative(self, name):
        if name.startswith(self.root + "sqlglot" + os.sep):
            return name[len(self.root):].replace(os.sep, "/")
        return None

    def line(self, code, line):
        path = self.relative(code.co_filename)
        if path:
            row = self.rows[path]
            row[line] = row.get(line, 0) | self.active
        return self.mon.DISABLE

    def switch(self, tag):
        self.active = 1 << tag
        self.mon.restart_events()

    def finish(self, count):
        self.mon.set_events(self.tool, 0)
        masks = list(sorted({v for row in self.rows.values() for v in row.values()}))
        indices = {v: i for i, v in enumerate(masks)}
        return {"count": count, "masks": masks, "paths": {
            path: {line: indices[mask] for line, mask in sorted(values.items())}
            for path, values in sorted(self.rows.items())
        }}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("source", type=Path)
    args.add_argument("output", type=Path)
    args.add_argument("--one", choices=TESTS)
    options = args.parse_args()
    root = options.source.resolve()
    output = options.output.resolve()
    if options.one:
        sys.path.insert(0, str(root))
        os.chdir(root)
        recorder = Trace(root)
        count = replay(options.one, recorder=recorder)
        output.write_text(json.dumps(recorder.finish(count), separators=(",", ":")))
        return
    import tempfile
    result = {}
    with tempfile.TemporaryDirectory() as temp:
        for i, nodeid in enumerate(TESTS):
            part = Path(temp) / f"{i}.json"
            subprocess.run([sys.executable, __file__, str(root), str(part), "--one", nodeid],
                           check=True)
            result[nodeid] = json.loads(part.read_text())
    with gzip.GzipFile(output, "wb", compresslevel=6, mtime=0) as stream:
        stream.write(json.dumps(result, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
