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

## Portable gate host references

The offline performance gate keeps the Stage 0 Core import and version values
above as its code baselines. On 2026-07-22, three sequential runs of the gate at
integration commit `bd616c2` recorded process-startup p95 values of 30.605,
29.130, and 30.355 ms and Ext import p95 values of 53.486, 50.740, and 52.066
ms. The medians of those run-level statistics, 30.355 and 52.066 ms, are the
versioned host references in `scripts/performance-baseline-v1.json`.

For a reference metric `r`, the gate computes only its positive host delta,
`d(r) = max(0, observed(r) - baseline(r))`. Core version uses the sum of the
process-startup and Core-import deltas. The passive Ext registry uses the
minimum of the Core- and Ext-import deltas, so a regression in only one import
path cannot enlarge its allowance. A target passes when
`observed(target) - host_adjustment <= policy_threshold`; the JSON result keeps
the effective raw threshold and reports the adjustment, normalized observation,
policy threshold, and per-reference deltas under `details.host_normalization`.

Every reference remains an independent hard gate. Host normalization therefore
removes shared platform cost without turning a slow reference or target into an
automatic pass.

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
