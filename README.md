# Blast Radius

This entry chooses SQLGlot regression tests for the RWX Buildathon. Its inputs are a directory of unified diffs and the organizer's numbered test inventory. It prints one JSON object per patch.

## Build and run

Build on Linux amd64 with Docker or Podman. Set DATA to a directory containing the organizer's commits folder and tests.json.

```sh
docker build --platform linux/amd64 -t blast-radius .
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  -v "$DATA/commits:/data/commits:ro" -v "$DATA/tests.json:/data/tests.json:ro" \
  blast-radius > predictions.jsonl
```

For the organizer's sample, score the output outside the container with the score.py and answers.json supplied in the starter kit. The test answers are only used for evaluation.

## How it selects tests

The entry uses execution profiles from the unmodified SQLGlot commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa. Runtime test numbers are resolved by pytest node ID.

1. Parse the patch in the coordinates of the original file. Use the clean suite's statement coverage, import dependencies and fixture reads to find affected tests. A short-circuit edit can change whether a later operand executes, so the enclosing predicate also matters.
2. For supported edits, narrow the selection using observations from clean tests. These include the ordering and equality of comparison operands, reads of class configuration, invocation of callable entries in dispatch tables, parser token lookahead, consumption of generated closures, and individual queries in the large executor tests. The profiler accounts for serialization into and out of process-pool workers.
3. For changes to declarations, check whether the patched module initializes. Where a short replay can reduce the simulated cost, run candidate tests on a separate patched source tree. Unfinished and failing probes remain selected.
4. Sort the selected tests by recorded duration, try up to eight worker counts and keep the least expensive partition under the organizer's cost formula. Spare capacity on shorter workers can cover additional tests at the same simulated cost.

The profiles are empirical. Dynamic loading, cached state, external code and interactions between tests can hide a dependency. A timeout or unknown diff format expands the selection. Small changes can still require a long-running test.

## Rebuild the profiles

The Docker build fetches the pinned upstream archive and installs the supplied locked Python dependencies. It loads the precomputed maps from this repository. To regenerate them, use a separate pinned Git checkout, Python 3.13 and the locked packages. Set SRC to the absolute checkout path and TESTS to the organizer's tests.json. Run from this repository. The Python executable must be available as python in worker subprocesses.

```sh
export SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
python build_map.py "$SRC" "$TESTS" impact.json.gz
python build_bindings.py "$SRC" "$TESTS" bindings.json.gz
python build_predicates.py "$SRC" "$TESTS" predicates.json.gz
python build_dispatch.py "$SRC" "$TESTS" dispatch.json.gz
python build_guards.py "$SRC" "$TESTS" guards.json.gz
python build_match_tokens.py "$SRC" "$TESTS" match_tokens.json.gz
python build_factory.py "$SRC" "$TESTS" factories.json.gz
python build_fixture_cases.py "$SRC" fixture_cases.json.gz
python index_source.py "$SRC" refs.json.gz
```

For the fixture-rename map, trace the baseline suite under Linux strace. Set TRACE_DIR to an empty temporary directory and TIMELINE to a path outside that directory.

```sh
mkdir -p "$TRACE_DIR"
strace -ff -qq -yy -ttt -e trace=%file,getdents,getdents64,io_uring_enter -o "$TRACE_DIR/process" python fixture_moves.py collect "$SRC" "$TESTS" "$TIMELINE"
python fixture_moves.py summarize "$TIMELINE" "$TRACE_DIR" fixture_moves.json.gz
```

## Research

Gligoric, Eloussi and Marinov's Ekstazi work measures test dependencies on files, including non-code inputs. This motivated recording which tests read each SQL fixture. [Practical Regression Test Selection with Dynamic File Dependencies](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf)

Liu and colleagues use semantic differences between code versions to improve regression test selection. Just, Ernst and Fraser show how observations on an unchanged program can identify when a change can alter execution state. We apply that distinction to supported Python comparison edits. [FineRTS](https://doi.org/10.1145/3597926.3598086) · [Efficient mutation analysis](https://homes.cs.washington.edu/~mernst/pubs/state-infection-issta2014-abstract.html)

Wang, Pradel and Liu examine Python name dependencies and the large sets that can result from eager imports. This motivated tracking the class that defines an attribute and the function actually invoked after a dispatch dictionary is inherited. [Names Are All You Need](https://arxiv.org/abs/2605.25356)
