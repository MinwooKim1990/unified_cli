# Platform-next Stage 3 audit

Recorded on 2026-07-20 from the `codex/platform-settings-repl` feature
branch, based on Stage 2 integration commit
`c256cdce723ae9f805d7a20753302b0e74afa7da`.

## Scope

Stage 3 introduces Settings v2 and restructures the interactive client around
`CommandRegistry`, `ReplState`, `EventRenderer`, and `SessionManager`. The
historical Core providers, public imports, direct chat path, non-TTY behavior,
and all 15 original slash commands remain compatible. `/tokens` remains an
alias of `/usage`, and `/quit` remains an alias of `/exit`.

Settings v1 values are migrated in memory and written back only through the
validated v2 schema. The defaults retain the Core provider and routing
behavior, hide reasoning, use compact tool rendering, keep cross-provider
context at eight turns, retain provider-managed REPL permissions, and set the
future browser permission default to read-only. Extension settings are stored
under provider-ID namespaces, and `full` permission cannot be persisted.

The REPL now provides capability-aware provider, model, authentication,
settings, permission, tool, workspace, session, usage, theme, and multiline
commands. Unsupported capability requests fail with a reason and recovery
path. Model and extension discovery remain lazy: startup performs no provider,
model, authentication, network, or plugin probe.

## Security and compatibility boundary

- Settings, state, history, and session-index directories are owner-only
  (`0700`), and files are owner-only (`0600`) from their first write.
- State writers use bounded schemas, private lock files, no-follow checks,
  atomic replacement, and provider-namespaced opaque session IDs.
- Session metadata rejects credential-shaped keys and stores no transcript by
  default.
- Authentication helpers use provider-owned, fixed argument arrays with
  `shell=False`, a minimal non-secret environment, an interactive confirmation,
  bounded output, timeout, and process-group cleanup. Browser authentication is
  not implemented in this stage.
- `/diff` uses fixed Git arguments with hooks, fsmonitor, external diff drivers,
  text conversion, pagers, and global/system configuration disabled. Output,
  runtime, and descendant processes are bounded.
- `/export` creates a new `0600` regular file without following links or
  overwriting an existing path.
- Terminal-controlled provider, model, path, event, tool, and error text is
  sanitized before rendering. Korean wide characters and combining marks are
  counted by terminal cells, including terminals narrower than 20 columns.
- Tool start and result events are correlated by ID. Raw chain-of-thought is
  neither displayed nor persisted; only explicitly public reasoning summaries
  may be rendered.
- Explicit Codex read-only and workspace-write choices are translated to both
  fresh-process `-s` arguments and TOML-safe `sandbox_mode` overrides shared by
  fresh and resumed sessions. Additional writable roots are likewise carried
  through `sandbox_workspace_write.writable_roots` on resume.
- Permission transitions into workspace-write, and transitions back to a
  provider-managed default from an explicit mode, require interactive
  confirmation. Non-TTY use fails closed.
- Claude plan mode and effort, and Codex sandbox, effort, style, system prompt,
  web setting, and writable roots, are mapped only where the installed official
  CLI has a verified representation. Gemini and extension capabilities without
  a safe mapping fail closed.
- Cumulative partial/final text is reconciled for persisted turns in both sync
  and async conversations without changing yielded `Message` objects or order.

## Independent Sol audit

The first independent review reported two P1 and two P2 findings:

1. explicit Codex sandbox selection was not retained by resumed sessions;
2. `provider_default` to `workspace_write` did not require confirmation;
3. Codex additional directories were omitted from resumed sessions; and
4. cumulative partial plus final text could be stored twice in history,
   exports, and cross-provider context.

All four were repaired and converted into fresh/resume argument, permission
denial, sync/async stream, and early-close regression tests. During re-audit,
the reviewer found one additional P2: the toolbar imposed a 20-cell floor even
when the terminal was narrower. The floor was removed, 10-column Korean and
control-character coverage was added, and the reviewer also reproduced correct
behavior at 1, 2, 10, 20, and 21 columns.

The same independent reviewer then re-read and exercised the final snapshot:

- P0: **0**
- P1: **0**
- P2: **0**
- Gate: **PASS**
- Stage 4 may advance: **yes**

## Verification

- Core raw suite: **507 passed, 1 existing Starlette warning**
- Ext raw suite: **88 passed**
- Stage 3, PTY, ABI, and security focus: **246 passed**
- Final REPL and PTY focus: **64 passed**
- Python 3.9 grammar parse: **85 repository Python files passed**
- EN/KO catalogs: **484/484 keys**, with matching placeholders
- `git diff --check`: passed
- CodeGraph: synchronized and up to date
- Main worktree: clean at
  `ffc8a2194738db4ef78e88abbddfe64ae714f145`

## Startup performance

Using an isolated HOME and 30 fresh processes for import and version timing,
plus ten real disposable pseudo-terminal sessions for the first prompt:

| Operation | Stage 2 median | Stage 3 median | Gate |
| --- | ---: | ---: | --- |
| `import unified_cli` | 42.508 ms | 42.867 ms | pass |
| `unified-cli --version` | 83.487 ms | 79.395 ms | pass |
| REPL first prompt | not recorded | 138.835 ms | pass (`<=300 ms`) |

The first prompt did not submit a provider request and performed no provider or
extension discovery. Core startup remains within the larger-of-50-ms-or-10-
percent regression budget.

## Retained limitations

- The history libraries reopen their path by name. The wrapper verifies the
  owner-only directory and regular file before use and falls back to in-memory
  history on an unsafe path, but it cannot eliminate every same-UID pathname
  race outside its process.
- A process kill that cannot run Python cleanup, such as `SIGKILL` or host
  power loss, can leave a partially written brand-new export. It cannot expose
  the file to other users or overwrite an existing file.
- Descendant process-group cleanup is a POSIX contract. The package's declared
  platforms and current Core CLI targets are POSIX; a future Windows port
  requires a separate job-object implementation and audit.
- Provider capabilities without a verified, fail-closed official CLI mapping
  remain unavailable rather than being approximated. Extension adapter support
  belongs to Stage 5.

This audit records local development evidence only. No tag, PyPI upload,
GitHub Release, or merge to `main` is authorized at this stage.
