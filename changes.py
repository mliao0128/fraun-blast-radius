"""Apply checked unified hunks to the pinned tree, retaining old-side edit coordinates."""
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


@dataclass
class Edit:
    start: int                      # zero-based gap in the old file
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


@dataclass
class Change:
    path: str
    old: str
    new: str | None
    edits: list[Edit]


def file_path(label):
    label = label.split("\t", 1)[0]
    if label == "/dev/null":
        return None
    if not label.startswith(("a/", "b/")):
        raise ValueError("unexpected patch filename")
    name = label[2:]
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name:
        raise ValueError("unexpected patch path")
    return name


def parse_patch(patch, source):
    stream = patch.splitlines()
    if any(s.startswith(("rename from ", "rename to ", "copy from ", "copy to ", "GIT binary patch", "Binary files ")) for s in stream):
        raise ValueError("non-text change")
    result = []
    i = 0
    while i < len(stream):
        if not stream[i].startswith("--- "):
            i += 1
            continue
        before, after = file_path(stream[i][4:]), file_path(stream[i + 1][4:])
        if before and after and before != after:
            raise ValueError("renamed file")
        path = before or after
        old = (source / path).read_text(encoding="utf-8") if before else ""
        lines = old.splitlines()
        new = []
        edits = []
        ends_with_newline = old.endswith("\n")
        new_no_eol = False
        cursor = 0
        i += 2
        while i < len(stream) and stream[i].startswith("@@ "):
            header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", stream[i])
            old_start, old_count, _, new_count = header.groups()
            old_count, new_count = int(old_count or 1), int(new_count or 1)
            start = int(old_start) - int(old_count > 0)
            if start < cursor:
                raise ValueError("overlapping hunks")
            new.extend(lines[cursor:start])
            cursor = start
            i += 1
            edit = None
            last = None
            while old_count or new_count:
                line = stream[i]
                i += 1
                if line.startswith("\\ No newline"):
                    if last in ("+", " "):
                        new_no_eol = True
                    continue
                op, value = line[0], line[1:]
                last = op
                if op in "- ":
                    if lines[cursor] != value:
                        raise ValueError("patch differs from pinned source")
                    old_count -= 1
                if op in "+ ":
                    new_count -= 1
                    new.append(value)
                if op == " ":
                    edit = None
                elif op in "+-":
                    if edit is None:
                        edit = Edit(cursor)
                        edits.append(edit)
                    (edit.removed if op == "-" else edit.added).append(value)
                else:
                    raise ValueError("unrecognized hunk")
                if op in " -":
                    cursor += 1
                if min(old_count, new_count) < 0:
                    raise ValueError("bad hunk length")
            if i < len(stream) and stream[i].startswith("\\ No newline"):
                new_no_eol = last in ("+", " ")
                i += 1
            if cursor == len(lines):
                ends_with_newline = not new_no_eol
        new.extend(lines[cursor:])
        content = "\n".join(new) + ("\n" if new and ends_with_newline else "")
        result.append(Change(path, old, content if after else None, edits))
    if not result:
        raise ValueError("no readable edits")
    return result


def shadow_tree(base, destination, changes):
    """Materialize changed files and link unchanged siblings to the read-only tree."""
    overrides = {Path(change.path): change.new for change in changes}
    dirs = {parent for name in overrides for parent in name.parents}
    for directory in sorted(dirs, key=lambda p: len(p.parts)):
        target = destination / directory
        target.mkdir(exist_ok=True)
        original = base / directory
        if original.is_dir():
            for child in original.iterdir():
                rel = directory / child.name
                # The image uses checked-hash bytecode. The interpreter validates
                # changed source even when the old and new file sizes coincide.
                if rel not in dirs and rel not in overrides:
                    (target / child.name).symlink_to(child, target_is_directory=child.is_dir())
    for path, content in overrides.items():
        if content is not None:
            (destination / path).write_text(content, encoding="utf-8")
