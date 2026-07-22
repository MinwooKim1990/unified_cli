# Platform-next Stage 0 baseline

Recorded from commit `ffc8a2194738db4ef78e88abbddfe64ae714f145` on 2026-07-20.

## Environment

- macOS 26.5.2 (build 25F84), Darwin 25.5.0, arm64
- Python 3.14.3, project virtual environment: `.venv`
- `unified-cli` 0.4.0; installed provider CLIs: Claude Code 2.1.185, Codex CLI
  0.144.1, agy 1.1.2

The checkout was clean before this document was added. The baseline test command
completed with **309 passed, 1 warning in 22.28s**:

```sh
./.venv/bin/python -m pytest -q
```

The warning is Starlette's deprecation warning for `httpx` in FastAPI's
`TestClient`; it does not fail the suite.

## No-prompt startup latency baseline

All measurements use 30 sequential fresh-process samples, `time.perf_counter_ns`
from the Python standard library, `stdin=DEVNULL`, and captured stdout/stderr.
Each subprocess receives an allowlisted environment with a disposable `HOME`,
XDG directories, and `TMPDIR`; provider credentials and user configuration are
not inherited. No provider prompt or authentication operation was invoked.
Results are milliseconds; p95 is linearly interpolated from the sorted 30
samples.

| Operation | min | median | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `import unified_cli` (in-process import body only) | 45.006 | 48.606 | 52.039 | 65.452 | 86.241 |
| `unified-cli --version` (end-to-end process) | 88.138 | 94.635 | 98.837 | 114.328 | 119.203 |
| `claude --version` (end-to-end process) | 38.105 | 39.183 | 54.545 | 41.558 | 493.986 |
| `codex --version` (end-to-end process) | 27.494 | 30.016 | 32.466 | 39.117 | 85.557 |
| `agy --version` (end-to-end process) | 38.448 | 41.853 | 51.576 | 54.006 | 307.669 |

The provider rows and wrapper row are startup/version probes, **not** equivalent
chat latency comparisons. They provide a safe raw-CLI-versus-wrapper reference
without prompting an authenticated provider. The run resolved Claude and agy
under `$HOME/.local/bin` and Codex under `/opt/homebrew/bin`; these paths are
recorded observations, not reproduction inputs. Large isolated maxima occurred
for all three provider CLIs, so use the median and p95 rather than the mean or
maximum for change detection.

### Reproduce import and version timing

Run from the repository root. This is the exact methodology used above. It does
not submit a prompt or intentionally issue an authenticated provider request.
The harness does not block network access, so undocumented vendor telemetry or
update checks performed by a `--version` command remain outside this guarantee.

```sh
./.venv/bin/python -c '
import os, shutil, statistics, subprocess, tempfile, time
from pathlib import Path
root = Path.cwd(); n = 30
commands = {
  "import": [str(root / ".venv/bin/python"), "-c", "import time; t=time.perf_counter_ns(); import unified_cli; print((time.perf_counter_ns()-t)/1e6)"],
  "wrapper": [str(root / ".venv/bin/unified-cli"), "--version"],
}
for name in ("claude", "codex", "agy"):
    if executable := shutil.which(name):
        print("resolved", name, executable)
        commands[name] = [executable, "--version"]
def percentile(values, p):
    values = sorted(values); k = (len(values)-1)*p; lo = int(k); hi = min(lo+1, len(values)-1)
    return values[lo] + (values[hi]-values[lo])*(k-lo)
with tempfile.TemporaryDirectory(prefix="unified-cli-benchmark-") as home:
    env = {
      "HOME": home, "PATH": os.environ.get("PATH", os.defpath),
      "TMPDIR": home + "/tmp", "XDG_CACHE_HOME": home + "/.cache",
      "XDG_CONFIG_HOME": home + "/.config",
      "XDG_DATA_HOME": home + "/.local/share", "LANG": "C", "LC_ALL": "C",
      "PYTHONNOUSERSITE": "1", "UNIFIED_CLI_LANG": "en",
    }
    Path(env["TMPDIR"]).mkdir(mode=0o700)
    for name, command in commands.items():
        samples = []
        for _ in range(n):
            start = time.perf_counter_ns()
            result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
            samples.append(float(result.stdout.strip()) if name == "import" else (time.perf_counter_ns()-start)/1e6)
        print(name, {"n": n, "min": min(samples), "median": statistics.median(samples), "mean": statistics.fmean(samples), "p95": percentile(samples, .95), "max": max(samples)})
'
```

The import measurement starts its clock immediately before `import unified_cli`
inside a newly launched Python process, so interpreter startup is deliberately
excluded. `unified-cli --version` is timed outside the process and therefore
includes executable and interpreter startup; its implementation has a dedicated
fast path that avoids parser construction and provider discovery.

## Immutable same-metric reference gate

The release-blocking gate keeps the Stage 0 Core anchors and policies above, but
qualifies host overhead by executing the exact same metric from an immutable
reference checkout. The reference is the full commit
`be1478884735c862e894959944ba53e149ea4210`. Later changes before this gate only
affected the old harness and its documentation; the measured Core and Ext
source trees are identical.

Every retained sample, and every warmup, runs three fresh processes in this
fixed order: `reference_before`, `candidate`, `reference_after`. For reference
values `b_i` and `a_i`, candidate value `c_i`, and the metric's versioned anchor
`A`, the calculation is:

`r_i = min(b_i, a_i)`

`h_i = max(0, r_i - A)`

`normalized_i = max(0, c_i - h_i)`

The target statistic is applied only after this per-sample normalization and is
compared unrounded. One slow reference side cannot increase credit because the
minimum is used. There is no ratio, multiplier, host cap, aggregate adjustment,
or cross-metric credit: Core import references only Core import, Core version
references only Core version, and the passive registry references only the
passive registry. Any reference failure, origin mismatch, or proof failure
fails the measurement closed.

The anchors and policies are:

| Metric | Statistic / samples | Reference anchor | Policy |
| --- | --- | ---: | ---: |
| Core import | median / 15 | 48.606 ms | 98.606 ms |
| Core version | median / 15 | 94.635 ms | 144.635 ms |
| Passive Ext registry | p95 / 61 | 195.661 ms | 250.000 ms |

The registry anchor was established on 2026-07-22 in Asia/Seoul on an Apple M4
Mac mini (Darwin 25.5.0, arm64) with CPython 3.14.3. The final sanitized harness
ran three independent runs, each with three warmups and 61 retained candidate
measurements while candidate and reference both pointed at the pinned source.
The run-level candidate p95 values were 167.266000, 195.660958, and 242.742667
ms; their median is 195.660958 ms, stored to the baseline's three-decimal
precision as 195.661 ms. For provenance, the same runs' `reference_before` p95
values were 176.680542, 208.317250, and 226.874375 ms; `reference_after` p95
values were 180.929583, 209.314458, and 232.702625 ms; and paired-minimum p95
values were 155.973250, 190.061750, and 174.057125 ms. The complete retained
arrays were captured in the implementation session output; no older ambient
272.861 ms, generated-workload 80.838 ms, or superseded 31-sample anchor was
reused.

With this repository's linear percentile definition, 61-sample p95 lands
exactly on sorted index 57 (the fourth-largest value), rather than interpolating
the 29th and 30th values of a 31-sample set. This fixed sample count makes the
five-percent exceedance policy less sensitive to two isolated scheduler spikes
while preserving the p95 statistic, 250 ms policy, and fail-closed credit
calculation. It is one 61-sample run, not an appended sample set or retry
selection.

The reference checkout is validated once, before any measured child, without a Git subprocess. Its detached
`.git/HEAD` must equal the full pinned SHA, both package versions must be exactly
0.5.0 and 0.1.0, and the two relevant source trees must match digest
`7f21edae7ab640afb342261ef4092586101edc9549661e391032ce6906fc04f4`.
The versioned `sha256-path-content-v1` digest walks
`src/unified_cli` and
`packages/unified-cli-ext/src/unified_cli_ext` in sorted POSIX-path order. It
hashes `D\\0<path>\\0` for directories and
`F\\0<path>\\0<size>\\0<bytes>` for files; bytecode/cache and egg/dist metadata
are excluded. Symlinks, special files, path escape, wrong versions, wrong SHA,
or digest drift fail closed.

The validator captures those exact regular-file bytes into a frozen parent-memory
manifest using descriptor-relative, no-follow opens and before/after `fstat`
checks. Each `reference_before` and `reference_after` invocation materializes a
new randomly named snapshot from that manifest immediately before process
launch and removes it immediately after exit. Snapshot creation is outside the
timer. The writable checkout and a reused reference sandbox are never exposed
to a reference process; the before snapshot is gone while the candidate runs,
and the after snapshot path does not yet exist.

Every Python child uses `python -I -S -B`: isolated mode and `-S` prevent cwd,
`PYTHONPATH`, user-site, and startup-hook injection, while explicit `-B` prevents
candidate/reference bytecode writes because isolated mode ignores the analogous
environment variable. The harness does not claim a fixed hash seed: `-I`
intentionally ignores `PYTHONHASHSEED`, and no policy depends on hash iteration
order. Every child cwd is a disposable empty directory, never the repository.

The registry bootstrap first retains only interpreter stdlib/lib-dynload paths,
then loads the guard by its absolute path, and only afterward adds the guard and
selected sanitized source paths. Candidate `ssl.py`, `socket.py`, and
`sitecustomize.py` therefore cannot shadow startup imports. It proves the exact
single distribution inventory and derives the exact entry-point inventory from
each already-verified `distribution.entry_points` property, avoiding the
version-sensitive global `metadata.entry_points()` shape on Python 3.9–3.14.
It discovers but does not import the canary descriptor and proves all project
module origins. Ambient site packages and ignored packaging metadata cannot
affect the measurement.

Normalized candidate children also install an audit hook before candidate paths
become importable. The hook records and rejects filesystem writes/mutations,
subprocess execution, `fork`/`forkpty`, and `ctypes`/`_ctypes` imports. Thus a
candidate cannot leave a watcher behind or alter a later reference snapshot;
even a caught attempt leaves a marker and fails the metric closed.

The REPL readiness metric retains its 300 ms p95 policy and now uses three
warmups plus 31 samples. The larger fixed sample set was adopted after a local
nine-sample run had two scheduler outliers and a 331.032 ms p95; it reduces
single-spike sensitivity without retries, best-of-N selection, or a threshold
increase.

## REPL first-prompt readiness gate

The relevant measure is wall time from invoking `unified-cli repl` in a real TTY
to the first rendered prompt in a fresh process. The harness allocates a
disposable pseudo-terminal so prompt-toolkit takes its real interactive path,
waits for the visible prompt marker, then submits only `/exit`. It uses an
isolated `HOME` and never submits a provider prompt. A pipe is not an equivalent
measurement because it selects a different non-TTY/readline path.

## Raw provider-versus-wrapper prompt latency: pending

A semantically comparable benchmark would send the same prompt to each raw CLI
and to `unified-cli chat` with the same provider/model/session settings. It is
outside this offline baseline because it can authenticate and contact a provider.
When authorized, measure at least 20 samples with a non-sensitive fixed prompt,
record provider/model/CLI versions, disable web search, and report both
time-to-first-output and completion time.
