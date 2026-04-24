"""HTML template for the `/dashboard` page (inline CSS + fetch polling)."""

DASHBOARD_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>unified-cli dashboard</title>
  <style>
    :root {
      --bg:#0f1115; --panel:#1a1d25; --border:#2b2f38;
      --fg:#e6e6e6; --dim:#8a8f98; --accent:#7dd3fc;
      --ok:#4ade80; --warn:#facc15; --err:#f87171;
      --mono: ui-monospace,"SFMono-Regular",Menlo,Monaco,Consolas,monospace;
    }
    * { box-sizing: border-box; }
    body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
           font: 14px/1.5 -apple-system,Segoe UI,sans-serif; }
    h1 { margin:0 0 8px; font-weight:600; font-size:20px; }
    .sub { color:var(--dim); margin-bottom:20px; font-size:12px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
    .card { background:var(--panel); border:1px solid var(--border);
            border-radius:8px; padding:16px; }
    .card h2 { margin:0 0 12px; font-size:13px; text-transform:uppercase;
               letter-spacing:0.1em; color:var(--dim); font-weight:600; }
    .full { grid-column: 1 / -1; }
    table { width:100%; border-collapse:collapse; font-family:var(--mono);
            font-size:12px; }
    th, td { padding:6px 8px; text-align:left; border-bottom:1px solid var(--border); }
    th { color:var(--dim); font-weight:600; font-size:11px;
         text-transform:uppercase; letter-spacing:0.05em; }
    tr:last-child td { border-bottom:none; }
    .ok { color:var(--ok); }  .warn { color:var(--warn); }  .err { color:var(--err); }
    .dim { color:var(--dim); }
    .mono { font-family:var(--mono); }
    .right { text-align:right; }
    .badge { display:inline-block; padding:2px 8px; border-radius:4px;
             font-size:11px; font-weight:600; }
    .badge-ok { background:rgba(74,222,128,0.15); color:var(--ok); }
    .badge-warn { background:rgba(250,204,21,0.15); color:var(--warn); }
    .badge-err { background:rgba(248,113,113,0.15); color:var(--err); }
    footer { margin-top:20px; color:var(--dim); font-size:11px; text-align:center; }
  </style>
</head>
<body>
  <h1>unified-cli dashboard</h1>
  <div class="sub">자동 갱신: 5초 · <span id="ts" class="mono">—</span></div>

  <div class="grid">
    <div class="card full">
      <h2>Providers</h2>
      <table id="tbl-providers">
        <thead>
          <tr>
            <th>Provider</th><th>Health</th><th>Binary</th>
            <th>Auth</th><th class="right">Models</th><th>Default</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Usage totals</h2>
      <table id="tbl-usage">
        <thead>
          <tr>
            <th>Provider</th><th class="right">Calls</th>
            <th class="right">Errors</th><th class="right">In / Out</th>
            <th class="right">Avg latency</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Active conversations</h2>
      <table id="tbl-conv">
        <thead>
          <tr><th>Conv id</th><th>Provider</th><th>Session</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card full">
      <h2>Recent calls</h2>
      <table id="tbl-recent">
        <thead>
          <tr>
            <th>Time</th><th>Provider</th><th>Model</th>
            <th class="right">In</th><th class="right">Out</th>
            <th class="right">Latency</th><th>Prompt</th><th>Error</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <footer>unified-cli · localhost only · <a style="color:var(--dim)" href="/v1/usage">/v1/usage</a> · <a style="color:var(--dim)" href="/v1/models">/v1/models</a></footer>

  <script>
    function healthBadge(h) {
      if (h === "ok") return '<span class="badge badge-ok">OK</span>';
      if (h === "setup_needed") return '<span class="badge badge-warn">setup needed</span>';
      return '<span class="badge badge-err">missing binary</span>';
    }
    function esc(s){ return String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
    function fmtTs(t){ return new Date(t*1000).toLocaleTimeString(); }
    function trunc(s,n){ s=String(s||''); return s.length>n? s.slice(0,n)+'…' : s; }

    async function refresh() {
      const [providers, usage, convs] = await Promise.all([
        fetch('/v1/doctor').then(r => r.json()),
        fetch('/v1/usage').then(r => r.json()),
        fetch('/v1/conversations').then(r => r.json()),
      ]);

      // Providers
      document.querySelector('#tbl-providers tbody').innerHTML = providers.map(p => `
        <tr>
          <td><b>${esc(p.provider)}</b></td>
          <td>${healthBadge(p.health)}</td>
          <td class="dim">${esc(trunc(p.bin_path || '(not found)', 40))}</td>
          <td>${p.has_oauth ? '<span class="ok">OAuth</span>' : ''}${p.has_oauth && p.has_api_key ? ' + ' : ''}${p.has_api_key ? '<span class="warn">$'+esc(p.api_key_env)+'</span>' : ''}${!p.has_oauth && !p.has_api_key ? '<span class="err">(none)</span>' : ''}</td>
          <td class="right mono">${p.model_count}</td>
          <td class="mono">${esc(p.default_model)}</td>
        </tr>
      `).join('');

      // Usage aggregates
      const agg = usage.aggregates || [];
      document.querySelector('#tbl-usage tbody').innerHTML = agg.length ? agg.map(a => `
        <tr>
          <td><b>${esc(a.provider)}</b></td>
          <td class="right mono">${a.calls}</td>
          <td class="right mono ${a.errors?'err':'dim'}">${a.errors}</td>
          <td class="right mono">${a.input_tokens}/${a.output_tokens}</td>
          <td class="right mono">${a.avg_latency_ms.toFixed(0)} ms</td>
        </tr>
      `).join('') : '<tr><td colspan=5 class="dim">아직 호출 없음</td></tr>';

      // Conversations
      const cs = convs.conversations || [];
      document.querySelector('#tbl-conv tbody').innerHTML = cs.length ? cs.map(c => `
        <tr>
          <td class="mono">${esc(c.conversation_id)}</td>
          <td>${esc(c.last_provider || '-')}</td>
          <td class="mono dim">${esc(trunc(c.sessions[c.last_provider] || '', 20))}</td>
        </tr>
      `).join('') : '<tr><td colspan=3 class="dim">활성 대화 없음</td></tr>';

      // Recent
      const rs = usage.recent || [];
      document.querySelector('#tbl-recent tbody').innerHTML = rs.length ? rs.map(r => `
        <tr>
          <td class="dim">${fmtTs(r.ts)}</td>
          <td>${esc(r.provider)}</td>
          <td class="mono">${esc(r.model)}</td>
          <td class="right mono">${r.input_tokens}</td>
          <td class="right mono">${r.output_tokens}</td>
          <td class="right mono">${r.latency_ms} ms</td>
          <td class="dim">${esc(trunc(r.prompt_preview, 40))}</td>
          <td class="err">${esc(r.error_kind || '')}</td>
        </tr>
      `).join('') : '<tr><td colspan=8 class="dim">아직 호출 없음</td></tr>';

      document.getElementById('ts').textContent = new Date().toLocaleTimeString();
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
