"""Regression checks for comparison evidence shared across numbered tests."""

import unittest
from unittest.mock import patch

from changes import Change, Edit
import predicate_map
from select_tests import HERE, Impact, unpack


class ComparisonSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = unpack(HERE / "impact.json.gz")["nodeids"][:3]
        numbers = (11, 24, 37, 99)
        cls.impact = Impact(
            [
                {"number": number, "id": name, "file": name.split("::")[0]}
                for number, name in zip(numbers, [*cls.ids, "tests/new.py::test_new"])
            ]
        )

    def select(self, groups, shared):
        before, after = "    return left <= right", "    return left < right"
        header = "def check(left, right):\n"
        path = "sqlglot/example.py"
        row = ["    return left ", "<=", " right", groups, shared]
        profile = ({path: {"2": [row]}}, self.ids, [])
        change = Change(
            path, header + before, header + after, [Edit(1, [before], [after])]
        )
        with (
            patch.object(predicate_map, "_profile", return_value=profile),
            patch.object(self.impact, "lookup_lines", return_value={11, 24}),
        ):
            return self.impact.select([change])

    def test_shared_only_change_selects_every_test_and_disables_replay(self):
        for shared in (1 << 50, 1):  # equal integer operands, or unknown operands
            with self.subTest(shared=shared):
                self.assertEqual(self.select({}, shared), ({11, 24, 37, 99}, False))

    def test_unchanged_shared_only_keeps_line_hits_and_unmapped_tests(self):
        self.assertEqual(self.select({}, 1 << 52), ({11, 24, 99}, True))

    def test_independent_operand_and_line_evidence(self):
        # Item 11 cannot change, 24 has line evidence only, and 37's
        # comparison can flip despite its absence from the line profile.
        groups = {str(1 << 52): [0], str(1 << 50): [2]}
        self.assertEqual(self.select(groups, 1 << 52), ({24, 37, 99}, True))
