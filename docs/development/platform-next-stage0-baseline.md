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

## Portable gate paired import calibration

The offline performance gate keeps the Stage 0 Core import and version values
above as code baselines. A later Ubuntu Python 3.14 run at integration commit
`be147888` showed why process startup is not a sufficient host reference:
startup p95 was 33.687 ms while the unchanged Core import median was 108.546 ms,
Core version median was 202.939 ms, Ext import p95 was 153.386 ms, and the
passive Ext registry p95 was 272.861 ms. Import-heavy work had a shared host
penalty that an interpreter-launch probe did not represent.

The gate now brackets every normalized target sample with fresh-process runs of
an independent, generated pure-Python import DAG. The disposable package is not
part of Core, Ext, their dependencies, or the repository source tree. It has
three fixed profiles so the calibration unit resembles each target:

| Target | DAG modules | Calibration value | Versioned calibration baseline |
| --- | ---: | --- | ---: |
| Core import | 420 leaves | import-body time | 49.712 ms |
| Core version | 500 leaves | end-to-end process time | 97.284 ms |
| Passive Ext registry | 720 leaves | import-body time | 80.838 ms |

The baselines were recorded in the same fresh-process bracket shape used by the
gate: three warmup pairs followed by three retained before/after pairs. The six
retained observations were 62.034500, 50.217458, 47.549583, 50.063250,
48.242250, and 49.361459 ms for Core import; 98.078250, 96.994500, 96.614459,
97.094333, 97.474500, and 98.390333 ms for Core version; and 80.935417,
80.101333, 79.955083, 81.404458, 80.791958, and 80.883583 ms for the passive
registry. Their medians, rounded to three decimals, are the values above. The
profiles deliberately use a factor of one: their local costs match the target
units closely enough that no multiplier can amplify a host allowance.

Each calibration process proves the exact module origin, module count, and
deterministic sentinel. Import canaries reject Core, Ext, or entry-point imports;
the subprocess guard rejects provider execution; and bytecode generation is
disabled. A failed proof or an invalid duration fails that target measurement
and grants no credit.

For target sample `i`, let `b_i` and `a_i` be the before and after calibration
durations, and let `c` be the profile baseline. When both durations are at most
`c + 50 ms`, the paired adjustment is:

`h_i = min(max(0, b_i - c), max(0, a_i - c))`

`h_i` is also capped at 50 ms. If exactly one duration exceeds the envelope,
that pair receives zero adjustment; if both exceed it, the target measurement
fails as an unqualified host. The gate computes
`normalized_i = target_i - h_i` for every sample and only then applies the
target's median or p95 statistic. It does not subtract an aggregate calibration
median. The Stage 0 Core policies remain
`48.606 + max(50, 10%)` and `94.635 + max(50, 10%)`; the passive registry keeps
its fixed 250 ms policy. Reported raw thresholds are raised only by the exact
difference between the raw and paired-normalized target statistics.

No project metric supplies an allowance to another metric. A Core import that
is slow but still within its own limit cannot make Core version or the passive
registry pass. The standalone Core-sized DAG metric is also a hard readiness
gate at 99.712 ms. The loader accepts only the explicit original
pre-normalization v1 shape as legacy; half-migrated or unbounded normalization
configuration fails closed.

## REPL first-prompt baseline: pending

The relevant measure is wall time from invoking `unified-cli repl` in a real TTY
to the first rendered `you>` prompt, separately for a cold and warm process.
It is intentionally not automated in this baseline: a pipe makes the REPL choose
its non-TTY/readline path, while the real path depends on prompt-toolkit terminal
capabilities, user history files, and terminal rendering. A clean future harness
should allocate a disposable pseudo-terminal, wait for the prompt marker, use an
isolated `HOME`, and never submit a provider prompt. Until then, do not compare
the piped startup time with interactive REPL performance.

## Raw provider-versus-wrapper prompt latency: pending

A semantically comparable benchmark would send the same prompt to each raw CLI
and to `unified-cli chat` with the same provider/model/session settings. It is
outside this offline baseline because it can authenticate and contact a provider.
When authorized, measure at least 20 samples with a non-sensitive fixed prompt,
record provider/model/CLI versions, disable web search, and report both
time-to-first-output and completion time.
