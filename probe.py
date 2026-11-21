"""Replay short unittest batches in disposable processes, preserving uncertainty."""
import importlib
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from changes import shadow_tree


class TestReplay:
    def __init__(self, source, inventory):
        self.source = source
        self.preloaded = False
        self.ids = {t["number"]: t["id"] for t in inventory}

    def check(self, changes, numbers, max_wall, case_plan=None):
        if not numbers or max_wall < 0.25:
            return set()
        if not self.preloaded and any(self.ids[n].startswith(("tests/test_optimizer.py", "tests/test_executor.py")) for n in numbers):
            # Only third-party libraries are shared. Every child imports SQLGlot
            # and the tests from its own complete patched source tree.
            importlib.import_module("pandas.testing")
            importlib.import_module("duckdb")
            self.preloaded = True
        with tempfile.TemporaryDirectory(prefix="br-") as directory:
            temp = Path(directory)
            shadow_tree(self.source, temp, changes)
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if not pid:
                try:
                    os.setsid()
                    os.close(read_fd)
                    with open(os.devnull, "w") as sink:
                        os.dup2(sink.fileno(), 1)
                        os.dup2(sink.fileno(), 2)
                    os.chdir(temp)
                    sys.path.insert(0, str(temp))
                    os.environ["PYTHONPATH"] = str(temp)
                    if case_plan:
                        from fixture_cases import replay
                        for number in numbers:
                            try:
                                replay(self.ids[number], case_plan[number])
                            except Exception:
                                continue
                            os.write(write_fd, f"{number}\n".encode())
                    else:
                        self.run_tests(numbers, write_fd)
                finally:
                    os._exit(0)
            os.close(write_fd)
            deadline = time.monotonic() + max_wall
            try:
                while os.waitpid(pid, os.WNOHANG) == (0, 0):
                    if time.monotonic() >= deadline:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, 0)
                        break
                    time.sleep(.005)
                os.set_blocking(read_fd, False)
                try:
                    result = os.read(read_fd, 65536)
                except BlockingIOError:
                    result = b""
            finally:
                os.close(read_fd)
            return {int(i) for i in result.splitlines() if i.isdigit()}

    def run_tests(self, numbers, fd):
        by_id = {}
        suite = unittest.TestSuite()
        for n in numbers:
            parts = self.ids[n].split("::")
            if len(parts) != 3 or "[" in parts[-1]:
                continue
            try:
                module = importlib.import_module(parts[0][:-3].replace("/", "."))
                cls = getattr(module, parts[1])
                if not issubclass(cls, unittest.TestCase):
                    continue
                case = cls(parts[2])
                by_id[case.id()] = n
                suite.addTest(case)
            except Exception:
                continue

        passed = []
        class Results(unittest.TestResult):
            def addSuccess(self, test):
                super().addSuccess(test)
                passed.append(by_id[test.id()])

        result = Results()
        result.failfast = True
        suite.run(result)
        if any(test.id() not in by_id for test, error in result.errors):
            return
        for number in passed:
            os.write(fd, f"{number}\n".encode())
