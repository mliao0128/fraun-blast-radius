"""Narrow edits whose effect depends on a statically or dynamically checked guard."""

import ast


def runtime_form(tree):
    aliases = set()
    direct = set()
    for item in tree.body:
        if isinstance(item, ast.Import):
            aliases.update(
                alias.asname or alias.name
                for alias in item.names
                if alias.name == "typing"
            )
        elif isinstance(item, ast.ImportFrom) and item.module == "typing":
            direct.update(
                alias.asname or alias.name
                for alias in item.names
                if alias.name == "TYPE_CHECKING"
            )
    all_names = aliases | direct
    if not all_names or any(
        isinstance(n, ast.Name)
        and n.id in all_names
        and not isinstance(n.ctx, ast.Load)
        for n in ast.walk(tree)
    ):
        return None
    body = []
    touched = False
    for item in tree.body:
        if isinstance(item, ast.If) and not item.orelse:
            val = item.test
            static = (
                isinstance(val, ast.Name)
                and val.id in direct
                or isinstance(val, ast.Attribute)
                and val.attr == "TYPE_CHECKING"
                and isinstance(val.value, ast.Name)
                and val.value.id in aliases
            )
            if static and all(
                isinstance(s, (ast.Import, ast.ImportFrom, ast.Pass)) for s in item.body
            ):
                touched = True
                continue
        body.append(item)
    return (
        ast.dump(ast.Module(body=body, type_ignores=tree.type_ignores))
        if touched
        else None
    )


def type_only(old, new, source):
    if "TYPE_CHECKING" not in source:
        return False
    a = runtime_form(old)
    return a is not None and a == runtime_form(new)


def deleted_guard(tree, edit, spans):
    if edit.added or not edit.removed:
        return None
    start, stop = edit.start + 1, edit.start + len(edit.removed)
    for node in spans:
        if (
            isinstance(node, ast.If)
            and not node.orelse
            and node.lineno == start
            and node.end_lineno == stop
            and node.body[0].lineno > start
            and isinstance(node.test, ast.Attribute)
            and isinstance(node.test.value, ast.Name)
        ):
            # Removing a local declaration can change name resolution before the guard.
            if any(
                isinstance(
                    n,
                    (
                        ast.Global,
                        ast.Nonlocal,
                        ast.FunctionDef,
                        ast.ClassDef,
                        ast.Yield,
                        ast.YieldFrom,
                    ),
                )
                or isinstance(n, ast.Name)
                and isinstance(n.ctx, (ast.Store, ast.Del))
                for n in ast.walk(node)
            ):
                continue
            return node
    return None
