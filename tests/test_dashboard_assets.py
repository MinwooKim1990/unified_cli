from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "unified_cli" / "web"
HTML = WEB / "dashboard.html"
CSS = WEB / "app.css"
JS = WEB / "app.js"


class DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.start_tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_script_text: list[str] = []
        self._script_src: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.start_tags.append((tag, values))
        if tag == "script":
            self._script_src = values.get("src")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script_src = None

    def handle_data(self, data: str) -> None:
        if self._script_src is None and any(tag == "script" for tag, _ in self.start_tags[-1:]):
            if data.strip():
                self.inline_script_text.append(data)


def _parser() -> DashboardParser:
    parser = DashboardParser()
    parser.feed(HTML.read_text(encoding="utf-8"))
    return parser


def test_dashboard_assets_exist_and_use_external_active_content() -> None:
    assert HTML.is_file()
    assert CSS.is_file()
    assert JS.is_file()

    parser = _parser()
    links = [attrs for tag, attrs in parser.start_tags if tag == "link"]
    scripts = [attrs for tag, attrs in parser.start_tags if tag == "script"]
    styles = [attrs for tag, attrs in parser.start_tags if tag == "style"]

    assert any(item.get("href") == "/dashboard/assets/app.css" for item in links)
    assert scripts == [{"type": "module", "src": "/dashboard/assets/app.js"}]
    assert not styles
    assert not parser.inline_script_text

    for _tag, attrs in parser.start_tags:
        assert "style" not in attrs
        assert not any(name.lower().startswith("on") for name in attrs)


def test_dashboard_has_all_seven_semantic_views_and_navigation() -> None:
    parser = _parser()
    views = {
        attrs["data-view-panel"]
        for tag, attrs in parser.start_tags
        if tag == "section" and attrs.get("data-view-panel")
    }
    nav_targets = {
        attrs["data-view"]
        for tag, attrs in parser.start_tags
        if tag == "button" and attrs.get("data-view")
    }
    expected = {"overview", "providers", "chat", "models", "sessions", "usage", "settings"}
    assert views == expected
    assert nav_targets == expected

    tags = [tag for tag, _attrs in parser.start_tags]
    assert {"aside", "nav", "header", "main", "section", "article", "form", "table", "caption", "dialog"} <= set(tags)


def test_dashboard_accessibility_structure_and_labels() -> None:
    parser = _parser()
    starts = parser.start_tags
    ids = {attrs["id"] for _tag, attrs in starts if attrs.get("id")}
    label_targets = {attrs["for"] for tag, attrs in starts if tag == "label" and attrs.get("for")}
    controls = [attrs for tag, attrs in starts if tag in {"input", "select", "textarea"}]

    assert any(tag == "a" and attrs.get("class") == "skip-link" and attrs.get("href") == "#main-content" for tag, attrs in starts)
    assert "main-content" in ids
    assert all(attrs.get("id") in label_targets or attrs.get("aria-label") for attrs in controls)
    assert any(attrs.get("aria-live") == "polite" for _tag, attrs in starts)
    assert len([attrs for _tag, attrs in starts if attrs.get("data-i18n-aria-label")]) >= 4
    assert any(tag == "div" and attrs.get("role") == "log" for tag, attrs in starts)
    assert any(tag == "dialog" and attrs.get("aria-labelledby") for tag, attrs in starts)
    assert len([tag for tag, _attrs in starts if tag == "caption"]) >= 3


def test_styles_cover_focus_mobile_zoom_themes_and_reduced_motion() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "@media (max-width: 360px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert 'html[data-theme="dark"]' in css
    assert "minmax(0, 1fr)" in css
    assert "overflow-x: auto" in css
    assert ".file-button:focus-within" in css
    assert "!important" in css  # reduced-motion and visually-hidden guarantees


def test_javascript_has_no_unsafe_dom_or_inline_style_apis() -> None:
    js = JS.read_text(encoding="utf-8")
    forbidden = (
        "inner" + "HTML",
        "outer" + "HTML",
        "insertAdjacent" + "HTML",
        "document." + "write",
        "new " + "Function",
        ".style",
        "Object." + "assign",
    )
    assert all(item not in js for item in forbidden)
    assert not re.search(r"\beval\s*\(", js)
    assert not re.search(r"setAttribute\s*\(\s*['\"]style['\"]", js)
    assert "document.createElement" in js
    assert ".textContent" in js


def test_bootstrap_secret_cleanup_cookie_credentials_and_csrf_are_explicit() -> None:
    js = JS.read_text(encoding="utf-8")
    fragment_index = js.index('fragment.get("bootstrap")')
    cleanup_index = js.index("window.history.replaceState")
    bootstrap_fetch_index = js.index("async function bootstrap()")
    assert fragment_index < cleanup_index < bootstrap_fetch_index
    assert 'headers.set("X-Unified-Bootstrap", bootstrapToken)' in js
    assert 'headers.set("X-CSRF-Token", state.csrfToken)' in js
    assert "unified_cli_manage_csrf" in js
    assert "MANAGE_TOKEN_PATTERN" in js
    assert "function validBootstrap(data)" in js
    assert 'data.version !== 1' in js
    assert 'credentials: "same-origin"' in js
    assert 'state.bootstrap.manage === true' in js
    assert 'state.bootstrap.authenticated === true' in js
    assert 'state.mode = managed ? "manage" : "read_only"' in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js


def test_plain_mode_and_initial_load_do_not_probe_providers() -> None:
    js = JS.read_text(encoding="utf-8")
    assert "/v1/doctor" not in js
    assert "response.status === 404" in js
    assert "enterPlainMode()" in js
    assert 'readJson("/v1/usage")' in js
    assert 'readJson("/v1/conversations")' in js
    startup = js[js.rfind("wireEvents();") :]
    assert "bootstrap();" in startup
    assert "loadModels(" not in startup
    assert "loadUsage(" not in startup
    assert "loadSessions(" not in startup


def test_authenticated_events_stop_when_hidden_and_retry_is_bounded() -> None:
    js = JS.read_text(encoding="utf-8")
    assert 'fetch(`${API_ROOT}/events`' in js
    assert '"X-CSRF-Token": state.csrfToken' in js
    assert "response.body.getReader()" in js
    assert "new EventSource" not in js
    assert "document.hidden" in js
    assert 'document.addEventListener("visibilitychange", handleVisibilityChange)' in js
    assert "closeEvents();" in js
    assert "EVENT_RETRY_MS = 15_000" in js
    assert f'{API_STATE_PATH}' not in js


API_STATE_PATH = "/api/ui/v1/" + "state"


def test_frontend_routes_and_payloads_match_management_contract() -> None:
    js = JS.read_text(encoding="utf-8")
    assert '`${API_ROOT}/providers/${encodeURIComponent(provider)}/models`' in js
    assert '{ method: "POST", body: "{}" }' in js
    assert 'workspace_id: workspace' in js
    assert 'session_id: state.resumeSessionId || null' in js
    assert 'const path = `${API_ROOT}/chat/${encodeURIComponent(id)}/cancel`' in js
    assert 'if (!id) return;' in js
    assert 'body = { archived: true }' in js
    assert 'mutationFetch(`${API_ROOT}/settings`, { method: "PATCH"' in js
    assert 'lang: state.language' in js
    cancel_body = js[js.index("async function cancelChat()") : js.index("function wireEvents()")]
    assert cancel_body.index("await mutationFetch") < cancel_body.rindex("controller.abort()")


def test_provider_updates_are_allowlisted_against_prototype_pollution() -> None:
    js = JS.read_text(encoding="utf-8")
    assert "Object.assign" not in js
    assert 'Object.hasOwn(verified, key)' in js
    assert 'providerId(provider)' in js
    assert 'encodeURIComponent(id)' in js
    assert "__proto__" not in js
    assert "constructor" not in js
    assert "prototype" not in js


def test_provider_actions_and_extension_metadata_are_strictly_opt_in() -> None:
    js = JS.read_text(encoding="utf-8")
    assert 'provider.verify_supported !== true' in js
    assert 'provider.chat_supported !== true' in js
    assert 'provider.models_supported !== true' in js
    assert 'provider.default_supported !== true' in js
    assert 'provider[field] === true' in js
    assert 'value.chat_supported === true' in js
    assert 'value.verify_supported === true' in js
    assert 'value.models_supported === true' in js
    assert 'value.default_supported === true' in js
    assert 'provider.source === "extension"' in js
    assert 'provider.status !== "loaded"' in js
    assert 'provider.server_policy.enabled !== false' in js
    assert 'Object.hasOwn(provider, "commands")' in js
    assert 'source === "builtin" && CORE_PROVIDER_IDS.has' in js
    assert 'UNSAFE_PROVIDER_TEXT_PATTERN' in js
    assert '!CORE_PROVIDER_IDS.has(sessionProvider)' in js


def test_provider_state_connection_and_usage_export_fail_closed() -> None:
    js = JS.read_text(encoding="utf-8")
    assert 'return "unchecked"' in js
    assert 't("notChecked")' in js
    assert "state.connectionLabelKey = key" in js
    assert "timestampMilliseconds" in js
    export = js[js.index("function exportUsage()") : js.index("function releaseImages()")]
    assert "session_id" not in export
    assert "conversation_id" not in export
    assert "const copy = { ...row }" not in export
    assert "input_tokens:" in export and "latency_ms:" in export


def test_chat_ndjson_is_bounded_cancelable_and_hides_raw_reasoning() -> None:
    js = JS.read_text(encoding="utf-8")
    for event_type in (
        "session",
        "text_delta",
        "reasoning_summary",
        "tool_started",
        "tool_finished",
        "usage",
        "error",
        "done",
    ):
        assert f'"{event_type}"' in js
    assert "reasoning_delta" not in js
    assert "AbortController" in js
    assert 'response.headers.get("X-Unified-Chat-Id")' in js
    assert "MAX_CHAT_CHARS" in js
    assert "MAX_TOOL_ROWS" in js
    assert "MAX_IMAGES = 4" in js
    assert 'permission: "read_only"' in js


def test_language_dictionaries_have_matching_complete_keys() -> None:
    js = JS.read_text(encoding="utf-8")
    en_start = js.index("  en: {")
    ko_start = js.index("  ko: {")
    end = js.index("\n  }\n};", ko_start)
    key_pattern = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9]*):", re.MULTILINE)
    en_keys = set(key_pattern.findall(js[en_start + len("  en: {"):ko_start]))
    ko_keys = set(key_pattern.findall(js[ko_start + len("  ko: {"):end]))
    assert en_keys == ko_keys

    html = HTML.read_text(encoding="utf-8")
    referenced = set(re.findall(
        r'data-i18n(?:-placeholder|-aria-label)?="([A-Za-z][A-Za-z0-9]*)"',
        html,
    ))
    assert referenced <= en_keys
    assert {"navOverview", "navProviders", "navChat", "navModels", "navSessions", "navUsage", "navSettings"} <= en_keys


def test_node_accepts_dashboard_module_syntax_when_available() -> None:
    node = shutil.which("node")
    if node is None:
        return
    result = subprocess.run(
        [node, "--check", str(JS)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
