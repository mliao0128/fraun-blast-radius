"""Observe value reads from immutable-literal class dictionaries on the clean suite."""
import ast
import functools
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.reduction import ForkingPickler


def serializing_pool(cls, *args, initializer=None, initargs=(), **kwargs):
    def through_pipe(value):
        return ForkingPickler.loads(ForkingPickler.dumps(value))

    class LocalPool(ThreadPoolExecutor):
        def submit(self, function, /, *args, **kwargs):
            def dispatch():
                fn, args2, kw = through_pipe((function, args, kwargs))
                return through_pipe(fn(*args2, **kw))
            return super().submit(dispatch)
    if initializer:
        initializer, initargs = through_pipe((initializer, initargs))
    return LocalPool(max_workers=1, initializer=initializer, initargs=initargs)


class Values:
    def __init__(self, active):
        self.active = active
        self.seen = defaultdict(lambda: defaultdict(set))
        self.unknown = set()

    def observe(self, mark, key):
        if not self.active:
            self.unknown.add(mark)
        else:
            self.seen[mark][key].update(self.active)

    def dictionary(self, mark, mapping):
        records = self
        class Monitored(dict):
            def __getitem__(self, key):
                records.observe(mark, str(key))
                return super().__getitem__(key)

            def get(self, key, *args):
                records.observe(mark, str(key))
                return super().get(key, *args)

            def copy(self):
                records.observe(mark, "*")
                return super().copy()

            def items(self):
                records.observe(mark, "*")
                return super().items()

            def values(self):
                records.observe(mark, "*")
                return super().values()

            def __iter__(self):
                return super().__iter__()

            def __eq__(self, other):
                records.observe(mark, "*")
                return super().__eq__(other)

            def __ne__(self, other):
                records.observe(mark, "*")
                return super().__ne__(other)

            def __repr__(self):
                records.observe(mark, "*")
                return super().__repr__()
        def instrument_bulk(method_name):
            original = getattr(dict, method_name)
            @functools.wraps(original)
            def observed_call(obj, *args, **kwargs):
                records.observe(mark, "*")
                return original(obj, *args, **kwargs)
            return observed_call

        for operation in ("__or__", "__ror__", "__ior__", "__reduce_ex__", "pop", "popitem", "setdefault", "update"):
            setattr(Monitored, operation, instrument_bulk(operation))
        self.seen[mark]
        return Monitored(mapping)


def instrument_config(cls, node, relative):
    for assignment in node.body:
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
            continue
        target = assignment.targets[0] if isinstance(assignment, ast.Assign) else assignment.target
        if not isinstance(target, ast.Name) or not target.id.isupper() or not isinstance(assignment.value, ast.Dict):
            continue
        try:
            literal = ast.literal_eval(assignment.value)
        except (ValueError, TypeError):
            continue
        if not literal or not all(type(k) is str and k != "*" for k in literal):
            continue
        mark = f"{relative}:{assignment.lineno}"
        assignment.value = ast.copy_location(ast.Call(ast.Name("_br_config", ast.Load()),
            [ast.Constant(mark), assignment.value], []), assignment.value)
    return cls.generic_visit(node)

