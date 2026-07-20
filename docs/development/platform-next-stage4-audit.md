# Platform-next Stage 4 audit

Recorded on 2026-07-20 from the `codex/platform-browser-manage` feature
branch, building on the audited Stage 3 settings and REPL boundary.

## Scope

Stage 4 adds a packaged browser dashboard and an explicitly enabled local
management mode. Ordinary `unified-cli serve` behavior remains compatible and
continues to expose the existing read-only dashboard and OpenAI-compatible API.
Mutation-capable browser control is available only through
`unified-cli serve --manage`; repeatable `--workspace PATH` arguments are
accepted only with that opt-in.

The dashboard is shipped as three package resources (`dashboard.html`,
`app.css`, and `app.js`) with no Node runtime or browser-build dependency in
Core. The compatibility export `DASHBOARD_HTML` now decodes the packaged HTML,
while a fixed allowlist exposes only those three assets with fixed MIME types.
Traversal, normalized aliases, and arbitrary package paths are rejected.

Provider metadata is safe to enumerate without importing an extension plugin.
Provider verification and model discovery remain explicit actions. In
particular, a browser model probe is a user-triggered POST and is never part of
dashboard bootstrap or passive refresh.

## Security and compatibility boundary

- Manage mode binds to loopback and remains unavailable through an external
  bind, even when the ordinary server's separate external-bind override is
  enabled. Host and peer checks retain the DNS-rebinding boundary.
- Startup issues a short-lived, one-time, 256-bit bootstrap credential. The
  CLI places its percent-encoded value in the `/dashboard#bootstrap=...`
  fragment. URL fragments are origin-local and are not sent in the HTTP
  request; the dashboard removes the fragment immediately with
  `history.replaceState()` before exchanging the credential.
- A successful bootstrap consumes the credential and returns a host-only
  `HttpOnly; SameSite=Strict` in-memory management session cookie. Reuse,
  malformed tokens, cross-site sources, expired tokens, and sessions from a
  previous runtime fail closed.
- Every management request requires both the management cookie and a CSRF proof
  kept in memory and origin/port-scoped `history.state` for secure reloads. The
  server checks the exact same Origin for mutations, and bootstrap separately
  validates Origin and Fetch Metadata without trusting forwarded headers.
- Dashboard HTML, CSS, and JavaScript are separate static resources. Browser
  responses apply a restrictive self-only CSP, deny framing and MIME sniffing,
  disable referrer leakage, and use `no-store`. There is no inline active
  content, dynamic HTML insertion, `eval`, or arbitrary asset loader.
- Provider/model fields are rendered with DOM text nodes. Model collections use
  prototype-safe maps, and bounded normalization prevents an extension or
  provider value from becoming active markup.
- Browser chat is read-only and permits only Claude and Codex. Requests carry
  an opaque allowlisted workspace ID and a fixed `read_only` permission.
  Gemini and extension-provider browser chat are blocked rather than mapped to
  an unverified permission model.
- Provider verification uses fixed metadata-owned argument vectors, a minimal
  environment, a synthetic workspace, bounded output and time, and process
  group cleanup. Install and login commands are copy-only in the dashboard.
- Browser-visible session handles are keyed opaque identifiers. Native session
  IDs are not exposed. Session metadata remains bounded and contains no stored
  transcript by default.
- Usage responses omit prompt bodies and raw provider events. Counts, token
  aggregates, errors, and latency are bounded and redacted before entering the
  browser surface.
- Chat streams use a bounded normalized NDJSON vocabulary. Text, tool rows,
  images, body size, active conversations, provider concurrency, and output
  queues have explicit limits. Disconnect and cancel paths signal the active
  turn and clean up its process group; backpressure cannot grow an unbounded
  browser or server buffer.
- The UI has matching English and Korean catalogs, semantic landmarks, labeled
  controls, captions, polite live regions, keyboard focus handling, visible
  focus, reduced-motion behavior, light/dark themes, and responsive coverage
  down to 360 pixels.

## Independent audit status

An implementation-independent Sol review first identified subprocess lifecycle
issues involving high-numbered file descriptors, descendants that retained
stdio after their leader exited, asynchronous final-event races, Gemini's
separate streaming path, and post-exit resource-limit state. The implementation
was redesigned around selector-backed POSIX readers and the public asyncio
subprocess protocol API, then re-reviewed against dedicated regression fixtures.

The final independent review inspected the resulting source and ran 98 strict
provider, management, and lifecycle fixtures with warnings treated as errors.
Its final severity counts and decision were:

- P0: **0**
- P1: **0**
- P2: **0**
- Gate: **PASS**
- Stage 5 may advance: **YES**

## Verification

The final post-repair evidence is:

- Core raw suite: **592 passed**, with one pre-existing Starlette/httpx
  deprecation warning
- Strict subprocess lifecycle suite with `PYTHONWARNINGS=error`: **48 passed**
- Independent Sol audit fixture selection with warnings as errors: **98 passed**
- Ext raw suite: **88 passed**
- Browser Playwright/axe suite: **8 passed**
- Python 3.9 grammar parse: **49 Stage 4 Python files passed**
- Wheel and sdist metadata: **twine check passed**
- Distribution contents: packaged HTML/CSS/JS present with Core/Ext path
  separation preserved
- Clean-install recovery: Core-only wheel install passed; Core plus Ext install
  passed; uninstalling Ext left Core import, version, and dependency checks
  healthy
- `git diff --check`: passed for the recorded candidate

## Performance

Measurements use isolated process state and the Stage 4 local management
fixture. Times are medians unless a percentile is named.

| Operation | Stage 4 result | Gate |
| --- | ---: | --- |
| `import unified_cli` | 43.085 ms median / 44.223 ms p95 | within startup budget |
| `unified-cli --version` | 79.987 ms median / 86.092 ms p95 | within startup budget |
| REPL first prompt | 141.530 ms median / 164.491 ms max | below 300 ms gate |
| manage bootstrap | 0.493 ms median / 0.764 ms p95 | local-only fast path |
| bounded relay overhead | 0.0777 ms p95 | within relay budget |

Bootstrap performs no provider subprocess, network request, model probe, or
extension module import. Model discovery remains an explicit POST action.

## CI and release boundary

The browser harness pins Playwright and axe and runs with a synthetic temporary
HOME and workspace. It covers CSP-enforced smoke behavior separately from the
CSP-bypass context used only to inject axe, plus desktop, tablet, 360-pixel,
keyboard, English/Korean, reduced-motion, CSRF, bootstrap, permission, and
cleanup cases.

Browser CI remains opt-in because making recurring Chromium downloads part of
every hosted push/PR job requires explicit approval. This does not weaken the
release gate: the browser suite is mandatory in the local/release validation
run, with its pinned lockfile and installed Chromium, before Stage 4 may be
declared complete.

## Retained limitations

- Management sessions are process-local by design and are invalidated on
  restart. They are not a remote multi-user authentication mechanism.
- Provider verification can establish only the bounded command result it
  observes. It does not turn provider credentials into browser-visible data or
  authorize browser login/install flows.
- Browser chat remains read-only and restricted to verified Core mappings.
  Gemini and extension chat stay blocked until a separate capability and
  permission audit proves a safe mapping.
- Hosted browser CI is not an automatic recurring job until Chromium-download
  approval is granted; local/release browser verification remains mandatory.

This audit records feature-branch development evidence only. No merge to
`main`, tag, PyPI upload, GitHub Release, or other release action is authorized
by this document.
