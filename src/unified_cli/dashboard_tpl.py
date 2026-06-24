"""HTML template for the `/dashboard` page (inline CSS + fetch polling)."""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>unified-cli dashboard</title>
  <style>
    :root {
      --bg:#0f1115; --panel:#1a1d25; --border:#2b2f38;
      --fg:#e6e6e6; --dim:#8a8f98; --accent:#7dd3fc;
      --ok:#4ade80; --warn:#facc15; --err:#f87171;
      --mono: ui-monospace,"SFMono-Regular",Menlo,Monaco,Consolas,monospace;
      --panel-2:#20242e; --accent-soft:rgba(125,211,252,0.14);
      --accent-line:rgba(125,211,252,0.30);
      --shadow:0 1px 2px rgba(0,0,0,0.3);
      --shadow-hover:0 8px 24px rgba(0,0,0,0.45);
      --radius:12px;
    }
    * { box-sizing: border-box; }
    body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
           font: 14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
           -webkit-font-smoothing:antialiased;
           background-image:
             radial-gradient(900px 500px at 100% -10%, rgba(125,211,252,0.06), transparent 60%),
             radial-gradient(700px 400px at -10% 0%, rgba(125,211,252,0.04), transparent 55%); }
    a { color:var(--accent); text-decoration:none; }
    a:hover { text-decoration:underline; }

    /* ---- header ---- */
    header { display:flex; align-items:center; justify-content:space-between;
             flex-wrap:wrap; gap:12px; margin-bottom:22px; }
    .brand { display:flex; align-items:center; gap:12px; }
    .logo { width:34px; height:34px; border-radius:9px; flex:none;
            background:linear-gradient(145deg, var(--accent), #4aa8d8);
            display:grid; place-items:center; color:#06222e; font-weight:800;
            font-size:16px; box-shadow:0 0 0 1px var(--accent-line), 0 4px 14px rgba(125,211,252,0.25); }
    .brand h1 { margin:0; font-weight:700; font-size:19px; letter-spacing:-0.01em; }
    .brand .tag { color:var(--dim); font-size:11px; letter-spacing:0.04em;
                  text-transform:uppercase; }
    .status { display:flex; align-items:center; gap:16px; font-size:12px;
              color:var(--dim); }
    .status .item { display:flex; align-items:center; gap:7px; }
    .dot { width:8px; height:8px; border-radius:50%; flex:none; }
    .dot-live { background:var(--accent);
                box-shadow:0 0 0 0 var(--accent-line);
                animation:pulse 2s infinite; }
    .dot-ok { background:var(--ok); box-shadow:0 0 8px rgba(74,222,128,0.6); }
    .dot-err { background:var(--err); box-shadow:0 0 8px rgba(248,113,113,0.6);
               animation:none; }
    @keyframes pulse {
      0%   { box-shadow:0 0 0 0 var(--accent-line); }
      70%  { box-shadow:0 0 0 7px rgba(125,211,252,0); }
      100% { box-shadow:0 0 0 0 rgba(125,211,252,0); }
    }

    /* ---- stat cards ---- */
    .stats { display:grid; grid-template-columns:repeat(5,1fr); gap:14px;
             margin-bottom:18px; }
    .stat { background:linear-gradient(180deg, var(--panel-2), var(--panel));
            border:1px solid var(--border); border-radius:var(--radius);
            padding:14px 16px; box-shadow:var(--shadow); position:relative;
            overflow:hidden; }
    .stat::before { content:""; position:absolute; left:0; top:0; bottom:0;
                    width:3px; background:var(--accent); opacity:0.0; }
    .stat:hover::before { opacity:0.9; }
    .stat .label { font-size:10.5px; text-transform:uppercase;
                   letter-spacing:0.08em; color:var(--dim); font-weight:600; }
    .stat .value { font-family:var(--mono); font-size:25px; font-weight:600;
                   margin-top:5px; letter-spacing:-0.02em; line-height:1.1; }
    .stat .value .unit { font-size:13px; color:var(--dim); margin-left:3px;
                         font-weight:500; }
    .stat .value.accent { color:var(--accent); }

    /* ---- grid + cards ---- */
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    .card { background:var(--panel); border:1px solid var(--border);
            border-radius:var(--radius); padding:18px; box-shadow:var(--shadow);
            transition:box-shadow .18s ease, transform .18s ease,
                       border-color .18s ease; }
    .card:hover { box-shadow:var(--shadow-hover); transform:translateY(-1px);
                  border-color:#3a3f4b; }
    .card h2 { margin:0 0 14px; font-size:12px; text-transform:uppercase;
               letter-spacing:0.1em; color:var(--dim); font-weight:700;
               display:flex; align-items:center; gap:8px; }
    .card h2 .count { color:var(--accent); font-family:var(--mono);
                      font-size:11px; background:var(--accent-soft);
                      padding:1px 7px; border-radius:20px; font-weight:600; }
    .full { grid-column:1 / -1; }

    /* ---- provider health cards ---- */
    .phealth { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
               gap:12px; }
    .pcard { background:var(--panel-2); border:1px solid var(--border);
             border-radius:10px; padding:13px 14px; position:relative;
             transition:border-color .16s ease, transform .16s ease; }
    .pcard:hover { transform:translateY(-1px); }
    .pcard.h-ok { border-left:3px solid var(--ok); }
    .pcard.h-warn { border-left:3px solid var(--warn); }
    .pcard.h-err { border-left:3px solid var(--err); }
    .pcard.locked { opacity:0.72; }
    .pcard .ptop { display:flex; align-items:center; justify-content:space-between;
                   gap:8px; margin-bottom:10px; }
    .pcard .pname { font-weight:700; font-size:15px; letter-spacing:-0.01em; }
    .pcard .prow { display:flex; justify-content:space-between; gap:10px;
                   font-size:12px; padding:3px 0; }
    .pcard .prow .k { color:var(--dim); flex:none; }
    .pcard .prow .v { font-family:var(--mono); text-align:right; min-width:0;
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .lock { font-size:10px; color:var(--warn); border:1px solid rgba(250,204,21,0.35);
            background:rgba(250,204,21,0.10); border-radius:6px; padding:1px 6px;
            margin-left:6px; vertical-align:middle; }

    /* ---- spark ---- */
    .sparks { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    .spark { background:var(--panel-2); border:1px solid var(--border);
             border-radius:10px; padding:12px 14px; }
    .spark .stop { display:flex; justify-content:space-between; align-items:baseline;
                   margin-bottom:6px; }
    .spark .stop .t { font-size:11px; color:var(--dim); text-transform:uppercase;
                      letter-spacing:0.06em; font-weight:600; }
    .spark .stop .n { font-family:var(--mono); font-size:13px; color:var(--accent);
                      font-weight:600; }
    .spark svg { display:block; width:100%; height:46px; }

    /* ---- model bars ---- */
    .mbars { display:flex; flex-direction:column; gap:7px; }
    .mbar { display:grid; grid-template-columns:120px 1fr 34px; align-items:center;
            gap:10px; font-size:12px; }
    .mbar .mname { font-family:var(--mono); color:var(--fg); overflow:hidden;
                   text-overflow:ellipsis; white-space:nowrap; }
    .mbar .track { height:7px; background:rgba(255,255,255,0.05); border-radius:6px;
                   overflow:hidden; }
    .mbar .fill { height:100%; border-radius:6px;
                  background:linear-gradient(90deg, #4aa8d8, var(--accent)); }
    .mbar .mcount { font-family:var(--mono); text-align:right; color:var(--dim); }
    .mblock + .mblock { margin-top:12px; padding-top:12px;
                        border-top:1px solid var(--border); }
    .mblock .mhdr { font-size:11px; color:var(--dim); margin-bottom:8px;
                    font-weight:600; }
    .mblock .mhdr b { color:var(--fg); }

    /* ---- tables ---- */
    table { width:100%; border-collapse:collapse; font-family:var(--mono);
            font-size:12px; }
    thead th { position:sticky; top:0; background:var(--panel); }
    th, td { padding:7px 9px; text-align:left;
             border-bottom:1px solid var(--border); }
    th { color:var(--dim); font-weight:700; font-size:10.5px;
         text-transform:uppercase; letter-spacing:0.05em; }
    tbody tr { transition:background .12s ease; }
    tbody tr:hover { background:rgba(255,255,255,0.025); }
    tr:last-child td { border-bottom:none; }
    tr.err-row td { background:rgba(248,113,113,0.07); }
    tr.err-row td:first-child { box-shadow:inset 2px 0 0 var(--err); }
    .scroll { max-height:380px; overflow:auto; border-radius:8px; }
    .ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
    .dim { color:var(--dim); }
    .mono { font-family:var(--mono); }
    .right { text-align:right; }
    .num { font-variant-numeric:tabular-nums; }
    .empty { color:var(--dim); text-align:center; padding:18px; font-size:12px; }
    .badge { display:inline-block; padding:2px 9px; border-radius:20px;
             font-size:10.5px; font-weight:700; letter-spacing:0.02em; }
    .badge-ok { background:rgba(74,222,128,0.15); color:var(--ok); }
    .badge-warn { background:rgba(250,204,21,0.15); color:var(--warn); }
    .badge-err { background:rgba(248,113,113,0.15); color:var(--err); }
    .pill { display:inline-block; padding:1px 7px; border-radius:6px; font-size:11px;
            border:1px solid var(--border); }
    .pill-oauth { color:var(--ok); border-color:rgba(74,222,128,0.3);
                  background:rgba(74,222,128,0.08); }
    .pill-key { color:var(--accent); border-color:var(--accent-line);
                background:var(--accent-soft); }
    .pill-none { color:var(--err); border-color:rgba(248,113,113,0.3);
                 background:rgba(248,113,113,0.08); }

    footer { margin-top:24px; color:var(--dim); font-size:11px; text-align:center; }
    footer a { color:var(--dim); }
    footer a:hover { color:var(--accent); }

    /* ---- responsive ---- */
    @media (max-width:980px) { .stats { grid-template-columns:repeat(3,1fr); } }
    @media (max-width:700px) {
      body { padding:16px; }
      .grid { grid-template-columns:1fr; }
      .stats { grid-template-columns:repeat(2,1fr); }
      .sparks { grid-template-columns:1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="logo">u</div>
      <div>
        <h1>unified-cli</h1>
        <div class="tag">provider dashboard</div>
      </div>
    </div>
    <div class="status">
      <div class="item">
        <span id="conn-dot" class="dot dot-ok"></span>
        <span id="conn-txt">connecting…</span>
      </div>
      <div class="item">
        <span class="dot dot-live"></span>
        <span>updated <span id="ts" class="mono">—</span></span>
      </div>
    </div>
  </header>

  <div class="stats" id="stats"></div>

  <div class="grid">
    <div class="card full">
      <h2>Provider health <span class="count" id="prov-count">0</span></h2>
      <div class="phealth" id="phealth"></div>
    </div>

    <div class="card">
      <h2>Activity</h2>
      <div class="sparks">
        <div class="spark">
          <div class="stop"><span class="t">Latency</span><span class="n" id="spk-lat-n">—</span></div>
          <div id="spk-lat"></div>
        </div>
        <div class="spark">
          <div class="stop"><span class="t">Tokens</span><span class="n" id="spk-tok-n">—</span></div>
          <div id="spk-tok"></div>
        </div>
      </div>
      <div style="margin-top:14px" class="mbars-wrap">
        <div class="card-sub" style="font-size:11px;color:var(--dim);font-weight:600;margin-bottom:9px;text-transform:uppercase;letter-spacing:0.06em">Models by calls</div>
        <div class="mbars" id="mbars"></div>
      </div>
    </div>

    <div class="card">
      <h2>Usage by provider</h2>
      <table id="tbl-usage">
        <thead>
          <tr>
            <th>Provider</th><th class="right">Calls</th>
            <th class="right">Errors</th><th class="right">In / Out</th>
            <th class="right">Avg</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Active conversations <span class="count" id="conv-count">0</span></h2>
      <table id="tbl-conv">
        <thead>
          <tr><th>Conversation</th><th>Provider</th><th class="right">Turns</th><th>Session</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card full">
      <h2>Recent calls <span class="count" id="recent-count">0</span></h2>
      <div class="scroll">
        <table id="tbl-recent">
          <thead>
            <tr>
              <th>Time</th><th>Provider</th><th>Model</th>
              <th class="right">In</th><th class="right">Out</th>
              <th class="right">Cached</th><th class="right">Latency</th>
              <th>Prompt</th><th>Error</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <footer>unified-cli · localhost only ·
    <a href="/v1/usage">/v1/usage</a> ·
    <a href="/v1/doctor">/v1/doctor</a> ·
    <a href="/v1/models">/v1/models</a>
  </footer>

  <script>
    // ---- helpers ----
    function esc(s){ return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
    function fmtTs(t){ return new Date(t*1000).toLocaleTimeString(); }
    function trunc(s,n){ s=String(s||''); return s.length>n ? s.slice(0,n)+'…' : s; }
    function fmtNum(n){
      n = Number(n) || 0;
      if (n >= 1e6) return (n/1e6).toFixed(n>=1e7?0:1)+'M';
      if (n >= 1e3) return (n/1e3).toFixed(n>=1e4?0:1)+'k';
      return String(n);
    }

    function healthBadge(h){
      if (h === "ok") return '<span class="badge badge-ok">OK</span>';
      if (h === "setup_needed") return '<span class="badge badge-warn">SETUP</span>';
      return '<span class="badge badge-err">MISSING</span>';
    }
    function healthClass(h){
      if (h === "ok") return "h-ok";
      if (h === "setup_needed") return "h-warn";
      return "h-err";
    }

    // inline SVG sparkline; newest on the right
    function sparkline(values, stroke){
      var W = 240, H = 46, pad = 3;
      if (!values || !values.length) {
        return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"></svg>';
      }
      var max = Math.max.apply(null, values);
      var min = Math.min.apply(null, values);
      var span = (max - min) || 1;
      var n = values.length;
      var step = n > 1 ? (W - pad*2) / (n - 1) : 0;
      var pts = values.map(function(v, i){
        var x = pad + i*step;
        var y = H - pad - ((v - min) / span) * (H - pad*2);
        return [x, y];
      });
      var d = pts.map(function(p, i){ return (i?'L':'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
      var area = d + ' L' + pts[n-1][0].toFixed(1) + ' ' + H + ' L' + pts[0][0].toFixed(1) + ' ' + H + ' Z';
      var last = pts[n-1];
      var gid = 'g' + Math.random().toString(36).slice(2,7);
      return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
        + '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'
        + '<stop offset="0%" stop-color="'+stroke+'" stop-opacity="0.28"/>'
        + '<stop offset="100%" stop-color="'+stroke+'" stop-opacity="0"/>'
        + '</linearGradient></defs>'
        + '<path d="'+area+'" fill="url(#'+gid+')"/>'
        + '<path d="'+d+'" fill="none" stroke="'+stroke+'" stroke-width="1.6" '
        + 'stroke-linejoin="round" stroke-linecap="round"/>'
        + '<circle cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)+'" r="2.4" fill="'+stroke+'"/>'
        + '</svg>';
    }

    function setConn(ok){
      var d = document.getElementById('conn-dot');
      var t = document.getElementById('conn-txt');
      d.className = 'dot ' + (ok ? 'dot-ok' : 'dot-err');
      t.textContent = ok ? 'connected' : 'connection error';
      t.className = ok ? '' : 'err';
    }

    function statCard(label, value, unit, accent){
      return '<div class="stat"><div class="label">'+label+'</div>'
        + '<div class="value'+(accent?' accent':'')+'">'+value
        + (unit ? '<span class="unit">'+unit+'</span>' : '') + '</div></div>';
    }

    function renderStats(agg){
      var calls=0, errors=0, tin=0, tout=0, lat=0;
      agg.forEach(function(a){
        calls += a.calls||0; errors += a.errors||0;
        tin += a.input_tokens||0; tout += a.output_tokens||0;
        lat += a.total_latency_ms||0;
      });
      var errRate = calls ? (errors/calls*100) : 0;
      var avg = calls ? (lat/calls) : 0;
      document.getElementById('stats').innerHTML =
          statCard('Total calls', fmtNum(calls), '', true)
        + statCard('Error rate', errRate.toFixed(1), '%', false)
        + statCard('Tokens in', fmtNum(tin), '', false)
        + statCard('Tokens out', fmtNum(tout), '', false)
        + statCard('Avg latency', avg.toFixed(0), 'ms', true);
      var ev = document.querySelectorAll('#stats .stat .value')[1];
      if (ev && errRate > 0) ev.style.color = 'var(--err)';
    }

    function renderHealth(providers){
      document.getElementById('prov-count').textContent = providers.length;
      document.getElementById('phealth').innerHTML = providers.map(function(p){
        var locked = (p.health !== 'ok' && (p.model_source === 'locked' || p.model_source === 'disabled'))
                   || /lock|disabl/i.test(String(p.model_source||''));
        var auth = p.has_oauth ? '<span class="pill pill-oauth">OAuth</span>' : '';
        if (p.has_api_key) auth += (auth?' ':'') + '<span class="pill pill-key">$'+esc(p.api_key_env||'KEY')+'</span>';
        if (!p.has_oauth && !p.has_api_key) auth = '<span class="pill pill-none">none</span>';
        var src = p.model_source ? '<span class="dim"> · '+esc(p.model_source)+'</span>' : '';
        var lockTag = locked ? '<span class="lock">locked</span>' : '';
        return ''
          + '<div class="pcard '+healthClass(p.health)+(locked?' locked':'')+'">'
          +   '<div class="ptop"><span class="pname">'+esc(p.provider)+lockTag+'</span>'+healthBadge(p.health)+'</div>'
          +   '<div class="prow"><span class="k">Binary</span><span class="v" title="'+esc(p.bin_path||'')+'">'+esc(trunc(p.bin_path||'(not found)',34))+'</span></div>'
          +   '<div class="prow"><span class="k">Auth</span><span class="v">'+auth+'</span></div>'
          +   '<div class="prow"><span class="k">Models</span><span class="v">'+(p.model_count||0)+src+'</span></div>'
          +   '<div class="prow"><span class="k">Default</span><span class="v">'+esc(p.default_model||'—')+'</span></div>'
          + '</div>';
      }).join('');
    }

    function renderUsage(agg){
      document.querySelector('#tbl-usage tbody').innerHTML = agg.length ? agg.map(function(a){
        return '<tr>'
          + '<td><b>'+esc(a.provider)+'</b></td>'
          + '<td class="right num">'+(a.calls||0)+'</td>'
          + '<td class="right num '+(a.errors?'err':'dim')+'">'+(a.errors||0)+'</td>'
          + '<td class="right num">'+fmtNum(a.input_tokens)+' / '+fmtNum(a.output_tokens)+'</td>'
          + '<td class="right num">'+(a.avg_latency_ms||0).toFixed(0)+' <span class="dim">ms</span></td>'
          + '</tr>';
      }).join('') : '<tr><td colspan="5" class="empty">No calls yet</td></tr>';
    }

    function renderModelBars(agg){
      // merge model_calls across providers. Null-prototype map: model ids are
      // user-controllable, so a model named "constructor"/"__proto__"/"valueOf"
      // would otherwise hit Object.prototype and corrupt the whole chart.
      var merged = Object.create(null);
      agg.forEach(function(a){
        var mc = a.model_calls || {};
        Object.keys(mc).forEach(function(m){ merged[m] = (merged[m]||0) + mc[m]; });
      });
      var rows = Object.keys(merged).map(function(m){ return [m, merged[m]]; })
                       .sort(function(x,y){ return y[1]-x[1]; }).slice(0,8);
      if (!rows.length) {
        document.getElementById('mbars').innerHTML = '<div class="empty">No model calls yet</div>';
        return;
      }
      var max = rows[0][1] || 1;
      document.getElementById('mbars').innerHTML = rows.map(function(r){
        var pct = Math.max(4, (r[1]/max*100)).toFixed(1);
        return '<div class="mbar">'
          + '<span class="mname" title="'+esc(r[0])+'">'+esc(trunc(r[0],18))+'</span>'
          + '<span class="track"><span class="fill" style="width:'+pct+'%"></span></span>'
          + '<span class="mcount">'+r[1]+'</span></div>';
      }).join('');
    }

    function renderSparks(recent){
      // recent is newest-first; reverse so newest is on the right
      var ordered = recent.slice().reverse();
      var lat = ordered.map(function(r){ return Number(r.latency_ms)||0; });
      var tok = ordered.map(function(r){ return (Number(r.input_tokens)||0) + (Number(r.output_tokens)||0); });
      document.getElementById('spk-lat').innerHTML = sparkline(lat, getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#7dd3fc');
      document.getElementById('spk-tok').innerHTML = sparkline(tok, getComputedStyle(document.documentElement).getPropertyValue('--ok').trim() || '#4ade80');
      document.getElementById('spk-lat-n').textContent = lat.length ? (lat[lat.length-1]+' ms') : '—';
      document.getElementById('spk-tok-n').textContent = tok.length ? fmtNum(tok.reduce(function(a,b){return a+b;},0)) : '—';
    }

    function renderConv(cs){
      document.getElementById('conv-count').textContent = cs.length;
      document.querySelector('#tbl-conv tbody').innerHTML = cs.length ? cs.map(function(c){
        var sess = (c.sessions && c.sessions[c.last_provider]) || '';
        return '<tr>'
          + '<td>'+esc(trunc(c.conversation_id,14))+'</td>'
          + '<td>'+esc(c.last_provider || '—')+'</td>'
          + '<td class="right num">'+(c.turn_count||0)+'</td>'
          + '<td class="dim">'+esc(trunc(sess,16))+'</td>'
          + '</tr>';
      }).join('') : '<tr><td colspan="4" class="empty">No active conversations</td></tr>';
    }

    function renderRecent(rs){
      document.getElementById('recent-count').textContent = rs.length;
      document.querySelector('#tbl-recent tbody').innerHTML = rs.length ? rs.map(function(r){
        var isErr = !!r.error_kind;
        return '<tr'+(isErr?' class="err-row"':'')+'>'
          + '<td class="dim">'+fmtTs(r.ts)+'</td>'
          + '<td>'+esc(r.provider)+'</td>'
          + '<td>'+esc(trunc(r.model,22))+'</td>'
          + '<td class="right num">'+(r.input_tokens||0)+'</td>'
          + '<td class="right num">'+(r.output_tokens||0)+'</td>'
          + '<td class="right num dim">'+(r.cached_tokens||0)+'</td>'
          + '<td class="right num">'+(r.latency_ms||0)+'</td>'
          + '<td class="dim" title="'+esc(r.prompt_preview||'')+'">'+esc(trunc(r.prompt_preview,40))+'</td>'
          + '<td class="err">'+esc(r.error_kind || '')+'</td>'
          + '</tr>';
      }).join('') : '<tr><td colspan="9" class="empty">No calls yet</td></tr>';
    }

    async function refresh(){
      try {
        var results = await Promise.all([
          fetch('/v1/doctor').then(function(r){ return r.json(); }),
          fetch('/v1/usage').then(function(r){ return r.json(); }),
          fetch('/v1/conversations').then(function(r){ return r.json(); }),
        ]);
        var providers = results[0] || [];
        var usage = results[1] || {};
        var convs = results[2] || {};
        var agg = usage.aggregates || [];
        var recent = usage.recent || [];
        var cs = convs.conversations || [];

        renderStats(agg);
        renderHealth(providers);
        renderUsage(agg);
        renderModelBars(agg);
        renderSparks(recent);
        renderConv(cs);
        renderRecent(recent);

        setConn(true);
        document.getElementById('ts').textContent = new Date().toLocaleTimeString();
      } catch (e) {
        setConn(false);
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
