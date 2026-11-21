"""Verify initialization when a patch changes Python declarations."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from changes import shadow_tree


def imports_cleanly(source, changes, paths, budget):
    if budget < 0.1:
        return False
    with tempfile.TemporaryDirectory(prefix="br-import-") as dst:
        root = Path(dst)
        shadow_tree(source, root, changes)
        modules = [
            p[:-3].replace("/", ".").removesuffix(".__init__") for p in sorted(paths)
        ]
        try:
            check = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import importlib,sys; [importlib.import_module(m) for m in sys.argv[1:]]",
                    *modules,
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(root)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(2.0, budget),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return check.returncode == 0
