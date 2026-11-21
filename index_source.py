"""Index reads of class attributes and names in the pristine SQLGlot sources."""

import ast
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def build(root):
    refs = defaultdict(lambda: defaultdict(set))
    for folder in ["sqlglot", "tests"]:
        for path in (root / folder).rglob("*.py"):
            file = path.relative_to(root).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
                    node.ctx, ast.Load
                ):
                    key = node.id if isinstance(node, ast.Name) else node.attr
                    refs[key][file].add(node.lineno)
    return {
        name: {path: sorted(lines) for path, lines in paths.items()}
        for name, paths in refs.items()
    }


if __name__ == "__main__":
    with gzip.open(sys.argv[2], "wt", encoding="utf-8", compresslevel=5) as out:
        json.dump(build(Path(sys.argv[1])), out, separators=(",", ":"))
