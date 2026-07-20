"use strict";

const API_ROOT = "/api/ui/v1";
const EVENT_RETRY_MS = 15_000;
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024;
const MAX_CHAT_CHARS = 200_000;
const MAX_TOOL_ROWS = 100;
const MAX_USAGE_ROWS = 250;
const MAX_SESSION_ROWS = 250;
const MAX_MODEL_ROWS = 500;
const MANAGE_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const PROVIDER_ID_PATTERN = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/;
const WORKSPACE_ID_PATTERN = /^ws_[A-Za-z0-9_-]{32}$/;

const messages = {
  en: {
    actions: "Actions", activeSessions: "Active sessions", addImages: "Add images",
    aggregateOnly: "Aggregate only", allProviders: "All providers", allResults: "All results",
    allTime: "All available", appearance: "Appearance", atGlance: "At a glance",
    awaitingBootstrap: "Waiting for secure bootstrap…", awaitingData: "Awaiting data",
    boundaries: "Boundaries", boundedLog: "Bounded log", boundedWorkspace: "Bounded workspace",
    browserPermission: "Browser permission", cachedTokens: "Cached tokens", cancel: "Cancel",
    capabilities: "Capabilities", chatHeading: "Read-only chat", checkingMode: "Checking mode",
    configuration: "Configuration", confirm: "Confirm", confirmAction: "Confirm action",
    connecting: "Connecting…", context: "Context", coreAndExt: "Core and Ext",
    credentials: "Credentials", defaultModel: "Provider default", defaults: "Defaults",
    disabled: "Disabled", errorRate: "Error rate", errorsOnly: "Errors only",
    exportJson: "Export JSON", guardrails: "Active guardrails", imageLimit: "PNG, JPEG, or WebP · up to 4 files · 12 MB total",
    inputTokens: "Input tokens", kind: "Kind", language: "Language", lastDay: "Last 24 hours",
    lastHour: "Last hour", latency: "Latency", lazyDiscovery: "On-demand discovery",
    leastPrivilege: "Least privilege", manager: "Manager", memoryOnly: "Memory only",
    model: "Model", modelsCaption: "Models returned by the selected provider",
    modelsHeading: "Provider models", modelsLazyEmpty: "Select a provider to load models. Nothing is probed automatically.",
    name: "Name", navChat: "Chat", navModels: "Models", navOverview: "Overview",
    navProviders: "Providers", navSessions: "Sessions", navSettings: "Settings & Security",
    navUsage: "Usage", noPromptPreview: "Prompt previews hidden", outputTokens: "Output tokens",
    overviewHeading: "Local management status", permission: "Permission",
    permissionReadOnly: "Permission: read_only", permissions: "Permissions", privacyAware: "Privacy-aware telemetry",
    prompt: "Prompt", promptHidden: "Prompt content hidden", promptStorage: "Prompt storage",
    provider: "Provider", providerHealth: "Provider health", providerIntro: "Commands are copy-only. Verification runs only when you request it.",
    providerModel: "Provider / model", providerSetup: "Provider setup", providerSummary: "Provider summary",
    providersHeading: "Capabilities and compatibility", providersReady: "Providers ready",
    readOnlyBody: "Management API was not found. No provider checks or model probes will run automatically.",
    readOnlyTitle: "Safe read-only mode", recentTokens: "Recent tokens", refresh: "Refresh",
    refreshModels: "Load models", refreshSessions: "Refresh sessions", refreshUsage: "Refresh usage",
    response: "Response", responseEmpty: "Streaming text will appear here.", result: "Result",
    sameOriginOnly: "Same origin only", security: "Security", selectProvider: "Select provider",
    selectWorkspace: "Select workspace", send: "Send", serverAllowlist: "Server allowlist",
    serverMode: "Server mode", sessionIndex: "Session index",
    sessionsCaption: "Indexed sessions. Core is managed locally; Ext belongs to a provider.",
    sessionsEmpty: "Refresh to load sessions.", sessionsHeading: "Core and Ext sessions",
    settingsHeading: "Settings & Security", skip: "Skip to main content", source: "Source",
    successOnly: "Success only", theme: "Theme", themeAuto: "Auto", themeDark: "Dark",
    themeLight: "Light", time: "Time", tokensCached: "Tokens / cached",
    toolTimeline: "Tool timeline", toolsEmpty: "Correlated tool events will appear here.",
    updated: "Updated", usageCaption: "Recent calls without prompt previews", usageEmpty: "Refresh to load usage.",
    usageHeading: "Usage and performance", viewAll: "View all", webAccess: "Web access",
    window: "Window", workspace: "Workspace", workspaces: "Workspaces",
    promptPlaceholder: "Ask about the selected workspace…", online: "Connected", offline: "Disconnected",
    manageMode: "Manage mode", readOnlyMode: "Read-only mode", plainMode: "Plain serve",
    bootstrapFailed: "Secure bootstrap failed", bootstrapReady: "Secure bootstrap complete",
    notAvailable: "Not available", notChecked: "Not verified", ready: "Ready", unavailable: "Unavailable", unknown: "Unknown",
    compatible: "Compatible", incompatible: "Incompatible", status: "Status",
    compatibility: "Compatibility", version: "Version", installCommand: "Install command",
    loginCommand: "Login command", copy: "Copy", copied: "Copied", copyFailed: "Copy failed",
    verify: "Verify", verifying: "Verifying…", verifyComplete: "Verification complete",
    verifyFailed: "Verification failed", noProviders: "No provider metadata is available.",
    noWorkspaces: "No approved workspaces are available.", loading: "Loading…", loadFailed: "Could not load data",
    modelsLoaded: "Models loaded", noModels: "No models returned for this provider.",
    contextUnknown: "Unknown", resume: "Resume", rename: "Rename", archive: "Archive",
    delete: "Delete", save: "Save", core: "Core", ext: "Ext", sessionActionFailed: "Session action failed",
    sessionActionComplete: "Session updated", renameSession: "Rename session", deleteSessionQuestion: "Delete this session from the index?",
    archiveSessionQuestion: "Archive this session?", noSessions: "No sessions returned.",
    noUsage: "No usage rows match these filters.", calls: "calls", errors: "errors", success: "Success",
    error: "Error", chatStarting: "Starting secure stream…", chatStreaming: "Streaming…",
    chatDone: "Response complete", chatCancelled: "Chat cancelled", chatFailed: "Chat failed",
    promptRequired: "Enter a prompt.", selectionRequired: "Select a provider and workspace.",
    imageRejected: "Some images were rejected by the file limits.", removeImage: "Remove image",
    session: "Session", reasoningSummary: "Reasoning summary", toolStarted: "Started",
    toolFinished: "Finished", toolFailed: "Failed", cancelRequested: "Cancel requested",
    exportReady: "Usage JSON exported", refreshComplete: "Refresh complete", explicitOnly: "Available on explicit refresh only",
    enabled: "Enabled", allowlisted: "Allowlisted", none: "None", countItems: "items",
    editableDefaults: "Editable defaults", savePreferences: "Save browser management preferences",
    noDefaultWorkspace: "No default workspace", allowWeb: "Allow read-only web tools",
    allowPromptPreview: "Allow short prompt previews in usage",
    languageLocalNote: "Language changes apply immediately and are stored when you choose Save settings.",
    saveSettings: "Save settings", settingsSaved: "Settings saved", settingsFailed: "Settings could not be saved",
    primaryLandmark: "Primary", managementViews: "Management views", summary: "Summary"
  },
  ko: {
    actions: "작업", activeSessions: "활성 세션", addImages: "이미지 추가",
    aggregateOnly: "집계만 표시", allProviders: "모든 제공자", allResults: "모든 결과",
    allTime: "전체 기간", appearance: "화면", atGlance: "한눈에 보기",
    awaitingBootstrap: "보안 부트스트랩을 기다리는 중…", awaitingData: "데이터 대기 중",
    boundaries: "경계", boundedLog: "제한된 로그", boundedWorkspace: "제한된 작업공간",
    browserPermission: "브라우저 권한", cachedTokens: "캐시 토큰", cancel: "취소",
    capabilities: "기능", chatHeading: "읽기 전용 채팅", checkingMode: "모드 확인 중",
    configuration: "구성", confirm: "확인", confirmAction: "작업 확인",
    connecting: "연결 중…", context: "컨텍스트", coreAndExt: "Core 및 Ext",
    credentials: "자격 증명", defaultModel: "제공자 기본값", defaults: "기본값",
    disabled: "비활성화", errorRate: "오류율", errorsOnly: "오류만",
    exportJson: "JSON 내보내기", guardrails: "활성 보호 장치", imageLimit: "PNG, JPEG 또는 WebP · 최대 4개 · 총 12MB",
    inputTokens: "입력 토큰", kind: "종류", language: "언어", lastDay: "최근 24시간",
    lastHour: "최근 1시간", latency: "지연 시간", lazyDiscovery: "요청 시 검색",
    leastPrivilege: "최소 권한", manager: "관리자", memoryOnly: "메모리에만 보관",
    model: "모델", modelsCaption: "선택한 제공자가 반환한 모델",
    modelsHeading: "제공자 모델", modelsLazyEmpty: "제공자를 선택해 모델을 불러오세요. 자동 탐색은 하지 않습니다.",
    name: "이름", navChat: "채팅", navModels: "모델", navOverview: "개요",
    navProviders: "제공자", navSessions: "세션", navSettings: "설정 및 보안",
    navUsage: "사용량", noPromptPreview: "프롬프트 미리보기 숨김", outputTokens: "출력 토큰",
    overviewHeading: "로컬 관리 상태", permission: "권한",
    permissionReadOnly: "권한: read_only", permissions: "권한", privacyAware: "개인정보 보호 텔레메트리",
    prompt: "프롬프트", promptHidden: "프롬프트 내용 숨김", promptStorage: "프롬프트 저장",
    provider: "제공자", providerHealth: "제공자 상태", providerIntro: "명령은 복사만 됩니다. 확인은 직접 요청할 때만 실행됩니다.",
    providerModel: "제공자 / 모델", providerSetup: "제공자 설정", providerSummary: "제공자 요약",
    providersHeading: "기능 및 호환성", providersReady: "준비된 제공자",
    readOnlyBody: "관리 API를 찾지 못했습니다. 제공자 확인이나 모델 탐색은 자동으로 실행되지 않습니다.",
    readOnlyTitle: "안전한 읽기 전용 모드", recentTokens: "최근 토큰", refresh: "새로고침",
    refreshModels: "모델 불러오기", refreshSessions: "세션 새로고침", refreshUsage: "사용량 새로고침",
    response: "응답", responseEmpty: "스트리밍 텍스트가 여기에 표시됩니다.", result: "결과",
    sameOriginOnly: "동일 출처만", security: "보안", selectProvider: "제공자 선택",
    selectWorkspace: "작업공간 선택", send: "보내기", serverAllowlist: "서버 허용 목록",
    serverMode: "서버 모드", sessionIndex: "세션 색인",
    sessionsCaption: "색인된 세션입니다. Core는 로컬에서, Ext는 제공자가 관리합니다.",
    sessionsEmpty: "새로고침하여 세션을 불러오세요.", sessionsHeading: "Core 및 Ext 세션",
    settingsHeading: "설정 및 보안", skip: "본문으로 건너뛰기", source: "출처",
    successOnly: "성공만", theme: "테마", themeAuto: "자동", themeDark: "어둡게",
    themeLight: "밝게", time: "시간", tokensCached: "토큰 / 캐시",
    toolTimeline: "도구 타임라인", toolsEmpty: "연결된 도구 이벤트가 여기에 표시됩니다.",
    updated: "업데이트", usageCaption: "프롬프트 미리보기가 없는 최근 호출", usageEmpty: "새로고침하여 사용량을 불러오세요.",
    usageHeading: "사용량 및 성능", viewAll: "모두 보기", webAccess: "웹 접근",
    window: "기간", workspace: "작업공간", workspaces: "작업공간",
    promptPlaceholder: "선택한 작업공간에 관해 질문하세요…", online: "연결됨", offline: "연결 끊김",
    manageMode: "관리 모드", readOnlyMode: "읽기 전용 모드", plainMode: "일반 제공 모드",
    bootstrapFailed: "보안 부트스트랩 실패", bootstrapReady: "보안 부트스트랩 완료",
    notAvailable: "사용할 수 없음", notChecked: "확인 전", ready: "준비됨", unavailable: "사용 불가", unknown: "알 수 없음",
    compatible: "호환됨", incompatible: "호환되지 않음", status: "상태",
    compatibility: "호환성", version: "버전", installCommand: "설치 명령",
    loginCommand: "로그인 명령", copy: "복사", copied: "복사됨", copyFailed: "복사 실패",
    verify: "확인", verifying: "확인 중…", verifyComplete: "확인 완료",
    verifyFailed: "확인 실패", noProviders: "제공자 메타데이터가 없습니다.",
    noWorkspaces: "승인된 작업공간이 없습니다.", loading: "불러오는 중…", loadFailed: "데이터를 불러오지 못했습니다",
    modelsLoaded: "모델을 불러왔습니다", noModels: "이 제공자가 반환한 모델이 없습니다.",
    contextUnknown: "알 수 없음", resume: "재개", rename: "이름 변경", archive: "보관",
    delete: "삭제", save: "저장", core: "Core", ext: "Ext", sessionActionFailed: "세션 작업 실패",
    sessionActionComplete: "세션 업데이트됨", renameSession: "세션 이름 변경", deleteSessionQuestion: "색인에서 이 세션을 삭제할까요?",
    archiveSessionQuestion: "이 세션을 보관할까요?", noSessions: "반환된 세션이 없습니다.",
    noUsage: "필터에 맞는 사용량 행이 없습니다.", calls: "호출", errors: "오류", success: "성공",
    error: "오류", chatStarting: "보안 스트림 시작 중…", chatStreaming: "스트리밍 중…",
    chatDone: "응답 완료", chatCancelled: "채팅 취소됨", chatFailed: "채팅 실패",
    promptRequired: "프롬프트를 입력하세요.", selectionRequired: "제공자와 작업공간을 선택하세요.",
    imageRejected: "파일 제한으로 일부 이미지가 거부되었습니다.", removeImage: "이미지 제거",
    session: "세션", reasoningSummary: "추론 요약", toolStarted: "시작됨",
    toolFinished: "완료됨", toolFailed: "실패", cancelRequested: "취소 요청됨",
    exportReady: "사용량 JSON을 내보냈습니다", refreshComplete: "새로고침 완료", explicitOnly: "명시적으로 새로고칠 때만 사용 가능",
    enabled: "활성화", allowlisted: "허용됨", none: "없음", countItems: "개",
    editableDefaults: "편집 가능한 기본값", savePreferences: "브라우저 관리 환경설정 저장",
    noDefaultWorkspace: "기본 작업공간 없음", allowWeb: "읽기 전용 웹 도구 허용",
    allowPromptPreview: "사용량에 짧은 프롬프트 미리보기 허용",
    languageLocalNote: "언어 변경은 즉시 적용되며 설정 저장을 선택하면 서버에 저장됩니다.",
    saveSettings: "설정 저장", settingsSaved: "설정 저장됨", settingsFailed: "설정을 저장하지 못했습니다",
    primaryLandmark: "주요 영역", managementViews: "관리 화면", summary: "요약"
  }
};

const viewTitles = {
  overview: "navOverview", providers: "navProviders", chat: "navChat", models: "navModels",
  sessions: "navSessions", usage: "navUsage", settings: "navSettings"
};

const state = {
  language: "en",
  theme: "auto",
  mode: "pending",
  authenticated: false,
  csrfToken: "",
  bootstrap: {},
  providers: [],
  workspaces: [],
  currentView: "overview",
  eventController: null,
  eventRetryTimer: null,
  connectionConnected: false,
  connectionLabelKey: "connecting",
  images: [],
  chatController: null,
  chatId: "",
  chatText: "",
  chatTextNode: null,
  tools: new Map(),
  usage: null,
  modelsByProvider: new Map(),
  modelsLoading: false,
  resumeSessionId: "",
  confirmResolve: null
};

const fragment = new URLSearchParams(window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash);
let bootstrapToken = fragment.get("bootstrap") || "";
let bootstrapCsrf = isRecord(window.history.state)
  && typeof window.history.state.unified_cli_manage_csrf === "string"
  ? window.history.state.unified_cli_manage_csrf : "";
if (window.location.hash) {
  bootstrapCsrf = "";
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

function byId(id) {
  return document.getElementById(id);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function textValue(value, fallback = "—", limit = 500) {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed.slice(0, limit) : fallback;
  }
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return fallback;
}

function boolValue(value, fallback = false) {
  return typeof value === "boolean" ? value : fallback;
}

function numberValue(value, fallback = 0) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function listValue(value) {
  return Array.isArray(value) ? value : [];
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function makeElement(tag, className = "", content = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== "") node.textContent = content;
  return node;
}

function t(key) {
  return messages[state.language][key] || messages.en[key] || key;
}

function announce(message) {
  const target = byId("global-status");
  if (target) target.textContent = message;
}

function applyTranslations() {
  document.documentElement.lang = state.language;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.dataset.i18n;
    if (key && t(key)) node.textContent = t(key);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    const key = node.dataset.i18nPlaceholder;
    if (key) node.setAttribute("placeholder", t(key));
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
    const key = node.dataset.i18nAriaLabel;
    if (key) node.setAttribute("aria-label", t(key));
  });
  const titleKey = viewTitles[state.currentView];
  if (titleKey) byId("view-title").textContent = t(titleKey);
  byId("settings-language").textContent = state.language === "ko" ? "한국어" : "English";
  byId("settings-theme").textContent = t(`theme${state.theme.charAt(0).toUpperCase()}${state.theme.slice(1)}`);
  renderMode();
  updateConnection(state.connectionConnected, state.connectionLabelKey);
  if (state.providers.length) renderProviders();
  if (state.workspaces.length) populateWorkspaceOptions();
  if (state.usage) renderUsage();
}

function setTheme(theme) {
  const allowed = new Set(["auto", "light", "dark"]);
  state.theme = allowed.has(theme) ? theme : "auto";
  document.documentElement.dataset.theme = state.theme;
  byId("theme-select").value = state.theme;
  byId("setting-theme").value = state.theme;
  byId("settings-theme").textContent = t(`theme${state.theme.charAt(0).toUpperCase()}${state.theme.slice(1)}`);
}

function updateConnection(connected, labelKey) {
  const mark = byId("connection-indicator");
  const key = labelKey || (connected ? "online" : "offline");
  state.connectionConnected = connected;
  state.connectionLabelKey = key;
  mark.classList.toggle("is-online", connected);
  mark.classList.toggle("is-offline", !connected);
  const label = byId("connection-label");
  label.dataset.i18n = key;
  label.textContent = t(key);
}

function renderMode() {
  const badge = byId("mode-badge");
  const labels = { manage: "manageMode", read_only: "readOnlyMode", plain: "plainMode", pending: "checkingMode" };
  badge.textContent = t(labels[state.mode] || "readOnlyMode");
  badge.className = state.mode === "manage" ? "badge badge-success" : state.mode === "pending" ? "badge badge-neutral" : "badge badge-warning";
  byId("overview-mode").textContent = badge.textContent;
  const canManage = state.mode === "manage";
  const chatActive = state.chatController !== null;
  byId("chat-form").querySelectorAll("select, textarea, input, button").forEach((control) => {
    if (control.id === "chat-permission") return;
    if (control.id === "cancel-chat") control.disabled = !chatActive;
    else control.disabled = !canManage || chatActive;
  });
  byId("settings-form").querySelectorAll("select, input, button").forEach((control) => {
    if (control.id === "setting-permission") return;
    control.disabled = !canManage;
  });
}

function showView(name, focusMain = true) {
  if (!Object.hasOwn(viewTitles, name)) return;
  state.currentView = name;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== name;
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  byId("view-title").textContent = t(viewTitles[name]);
  if (focusMain) byId("main-content").focus({ preventScroll: true });
}

function normalizeProviders(value) {
  if (Array.isArray(value)) return value.filter(isRecord).slice(0, 50);
  if (!isRecord(value)) return [];
  return Object.entries(value).slice(0, 50).map(([name, metadata]) => {
    const data = isRecord(metadata) ? metadata : {};
    return { ...data, name: textValue(data.name || data.provider || name, name, 80) };
  });
}

function normalizeWorkspaces(value) {
  return listValue(value).slice(0, 100).map((item) => {
    if (typeof item === "string") return { id: item, name: item, path: item };
    if (!isRecord(item)) return null;
    const path = textValue(item.path || item.root, "", 1_000);
    const id = textValue(item.id || item.name || path, "", 300);
    if (!id) return null;
    return { id, name: textValue(item.name || path || id, id, 300), path };
  }).filter(Boolean);
}

function providerName(provider) {
  return textValue(provider.display_name || provider.name || provider.provider || provider.id, t("unknown"), 120);
}

function providerId(provider) {
  return textValue(provider.id || provider.provider || provider.name, "", 80);
}

function providerAvailability(provider) {
  const status = textValue(provider.status || provider.health || provider.state, "").toLowerCase();
  if (provider.ready === true || provider.installed === true || ["ok", "ready", "healthy", "available", "installed"].includes(status)) {
    return "ready";
  }
  if (provider.ready === false || provider.installed === false || ["unavailable", "missing", "failed", "error", "not_authenticated"].includes(status)) {
    return "unavailable";
  }
  return "unchecked";
}

function providerBadge(provider) {
  const availability = providerAvailability(provider);
  if (availability === "ready") return statusBadge(t("ready"), "success");
  if (availability === "unavailable") return statusBadge(t("unavailable"), "warning");
  return statusBadge(t("notChecked"), "neutral");
}

function statusBadge(text, kind) {
  return makeElement("span", `badge badge-${kind}`, text);
}

function appendSummaryRow(list, term, description) {
  const row = makeElement("div");
  row.append(makeElement("dt", "", term), makeElement("dd", "", description));
  list.append(row);
}

function capabilityText(value) {
  if (Array.isArray(value)) return value.slice(0, 12).map((item) => textValue(item, "", 60)).filter(Boolean).join(", ") || t("none");
  if (isRecord(value)) return Object.entries(value).filter(([, enabled]) => enabled === true).slice(0, 12).map(([name]) => name).join(", ") || t("none");
  return textValue(value, t("none"), 300);
}

function compatibilityText(provider) {
  const value = provider.compatibility;
  if (typeof value === "boolean") return value ? t("compatible") : t("incompatible");
  if (isRecord(value)) return textValue(value.status || value.message, t("unknown"), 300);
  return textValue(value, t("unknown"), 300);
}

function renderOverviewProviders() {
  const target = byId("overview-provider-list");
  clearNode(target);
  if (!state.providers.length) {
    target.append(makeElement("p", "empty-state", t("noProviders")));
    byId("overview-providers").textContent = "0";
    return;
  }
  let readyCount = 0;
  state.providers.slice(0, 6).forEach((provider) => {
    const availability = providerAvailability(provider);
    if (availability === "ready") readyCount += 1;
    const item = makeElement("div", "stack-item");
    const copy = makeElement("div");
    copy.append(makeElement("strong", "", providerName(provider)), makeElement("small", "", textValue(provider.version || provider.cli_version, t("notAvailable"), 100)));
    item.append(copy, providerBadge(provider));
    target.append(item);
  });
  byId("overview-providers").textContent = `${readyCount}/${state.providers.length}`;
  byId("overview-providers-note").textContent = t("providerSummary");
}

function createCopyCommand(label, command) {
  const wrapper = makeElement("div");
  wrapper.append(makeElement("small", "muted", label));
  const box = makeElement("div", "command-box");
  box.append(makeElement("code", "", command));
  const copy = makeElement("button", "icon-button", t("copy"));
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(command);
      announce(t("copied"));
      copy.textContent = t("copied");
      window.setTimeout(() => { copy.textContent = t("copy"); }, 1_500);
    } catch (_error) {
      announce(t("copyFailed"));
    }
  });
  box.append(copy);
  wrapper.append(box);
  return wrapper;
}

function renderProviders() {
  renderOverviewProviders();
  const target = byId("provider-grid");
  clearNode(target);
  if (!state.providers.length) {
    target.append(makeElement("p", "empty-state", t("noProviders")));
    populateProviderOptions();
    return;
  }
  state.providers.forEach((provider) => {
    const id = providerId(provider);
    const name = providerName(provider);
    const availability = providerAvailability(provider);
    const card = makeElement("article", "provider-card");
    const header = makeElement("header");
    const title = makeElement("div");
    title.append(makeElement("h3", "", name), makeElement("p", "muted", textValue(provider.description, textValue(provider.kind, "CLI provider", 100), 300)));
    header.append(title, providerBadge(provider));
    card.append(header);
    const details = makeElement("dl", "summary-list");
    appendSummaryRow(details, t("version"), textValue(provider.version || provider.cli_version, t("notAvailable"), 100));
    appendSummaryRow(details, t("status"), textValue(provider.status || provider.health || provider.state, availability === "ready" ? t("ready") : t("unknown"), 120));
    appendSummaryRow(details, t("capabilities"), capabilityText(provider.capabilities));
    appendSummaryRow(details, t("compatibility"), compatibilityText(provider));
    card.append(details);
    const commands = isRecord(provider.commands) ? provider.commands : {};
    const install = textValue(provider.install_command || provider.install || commands.install, "", 500);
    const login = textValue(provider.login_command || provider.login || commands.login, "", 500);
    if (install) card.append(createCopyCommand(t("installCommand"), install));
    if (login) card.append(createCopyCommand(t("loginCommand"), login));
    const verify = makeElement("button", "button secondary", t("verify"));
    verify.type = "button";
    verify.disabled = state.mode !== "manage" || provider.verify_supported === false || !id;
    verify.addEventListener("click", () => verifyProvider(id, verify, card));
    card.append(verify);
    target.append(card);
  });
  populateProviderOptions();
}

async function verifyProvider(id, button, card) {
  button.disabled = true;
  button.textContent = t("verifying");
  try {
    const data = await mutationFetch(`${API_ROOT}/providers/${encodeURIComponent(id)}/verify`, { method: "POST", body: "{}" });
    const current = state.providers.find((provider) => providerId(provider) === id);
    const verified = isRecord(data) && isRecord(data.provider) ? data.provider : data;
    if (current && isRecord(verified)) {
      ["installed", "auth", "status", "health", "version", "cli_version", "checks", "commands"].forEach((key) => {
        if (Object.hasOwn(verified, key)) current[key] = verified[key];
      });
    }
    renderProviders();
    announce(t("verifyComplete"));
  } catch (error) {
    button.disabled = false;
    button.textContent = t("verify");
    card.append(makeElement("p", "muted", errorMessage(error, t("verifyFailed"))));
    announce(t("verifyFailed"));
  }
}

function resetOptions(select, placeholderKey) {
  const current = select.value;
  clearNode(select);
  const first = makeElement("option", "", t(placeholderKey));
  first.value = "";
  select.append(first);
  return current;
}

function populateProviderOptions() {
  [byId("chat-provider"), byId("models-provider"), byId("usage-provider")].forEach((select) => {
    const placeholder = select.id === "usage-provider" ? "allProviders" : "selectProvider";
    const current = resetOptions(select, placeholder);
    state.providers.forEach((provider) => {
      const id = providerId(provider);
      if (!id) return;
      if (select.id === "chat-provider" && provider.chat_supported === false) return;
      const name = providerName(provider);
      const option = makeElement("option", "", name);
      option.value = id;
      select.append(option);
    });
    if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
  });
  populateSettingsOptions();
}

function populateWorkspaceOptions() {
  const select = byId("chat-workspace");
  const current = resetOptions(select, "selectWorkspace");
  state.workspaces.forEach((workspace) => {
    const option = makeElement("option", "", workspace.name);
    option.value = workspace.id;
    select.append(option);
  });
  if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
  populateSettingsOptions();
}

function populateSettingsOptions() {
  const settings = isRecord(state.bootstrap.settings) ? state.bootstrap.settings : {};
  const providerSelect = byId("setting-default-provider");
  const providerCurrent = providerSelect.value || textValue(settings.default_provider, "");
  resetOptions(providerSelect, "selectProvider");
  state.providers.forEach((provider) => {
    const id = providerId(provider);
    const coreDefault = ["claude", "codex", "gemini"].includes(id);
    if (!id || (!coreDefault && provider.default_supported !== true)) return;
    const option = makeElement("option", "", providerName(provider));
    option.value = id;
    providerSelect.append(option);
  });
  if (Array.from(providerSelect.options).some((option) => option.value === providerCurrent)) providerSelect.value = providerCurrent;

  const workspaceSelect = byId("setting-workspace");
  const workspaceCurrent = workspaceSelect.value || textValue(settings.workspace_id, "");
  resetOptions(workspaceSelect, "noDefaultWorkspace");
  state.workspaces.forEach((workspace) => {
    const option = makeElement("option", "", workspace.name);
    option.value = workspace.id;
    workspaceSelect.append(option);
  });
  if (Array.from(workspaceSelect.options).some((option) => option.value === workspaceCurrent)) workspaceSelect.value = workspaceCurrent;
}

function validBootstrap(data) {
  if (!isRecord(data) || data.version !== 1) return false;
  if (data.mode !== "manage" || data.manage !== true || data.authenticated !== true) return false;
  if (typeof data.csrf_token !== "string" || !MANAGE_TOKEN_PATTERN.test(data.csrf_token)) return false;
  if (!Array.isArray(data.providers) || !Array.isArray(data.workspaces)) return false;
  if (!isRecord(data.settings) || !isRecord(data.security) || !isRecord(data.limits) || !isRecord(data.versions) || !isRecord(data.defaults)) return false;
  if (typeof data.versions.unified_cli !== "string" || !data.versions.unified_cli || data.versions.unified_cli.length > 100) return false;
  const providerIds = new Set();
  for (const provider of data.providers) {
    if (!isRecord(provider) || typeof provider.id !== "string" || provider.id.length > 64 || !PROVIDER_ID_PATTERN.test(provider.id) || providerIds.has(provider.id)) return false;
    providerIds.add(provider.id);
  }
  const workspaceIds = new Set();
  for (const workspace of data.workspaces) {
    if (!isRecord(workspace) || typeof workspace.id !== "string" || !WORKSPACE_ID_PATTERN.test(workspace.id) || workspaceIds.has(workspace.id)) return false;
    if (typeof workspace.name !== "string" || !workspace.name || workspace.name.length > 200) return false;
    workspaceIds.add(workspace.id);
  }
  const settings = data.settings;
  if (!["en", "ko"].includes(settings.lang) || !["auto", "light", "dark"].includes(settings.theme)) return false;
  if (settings.browser_permission !== "read_only" || typeof settings.browser_prompt_preview !== "boolean" || typeof settings.web !== "boolean") return false;
  if (!["claude", "codex", "gemini"].includes(settings.default_provider)) return false;
  if (settings.workspace_id !== null && (typeof settings.workspace_id !== "string" || !workspaceIds.has(settings.workspace_id))) return false;
  return Number.isSafeInteger(data.limits.prompt_chars)
    && Number.isSafeInteger(data.limits.images)
    && Number.isSafeInteger(data.limits.image_bytes)
    && Number.isSafeInteger(data.limits.image_total_bytes);
}

function applyBootstrap(data) {
  if (!validBootstrap(data)) throw new Error(t("bootstrapFailed"));
  state.bootstrap = data;
  const mode = textValue(state.bootstrap.mode, "").toLowerCase();
  const csrfToken = textValue(state.bootstrap.csrf_token, "", 2_000);
  const managed = (
    mode === "manage"
    && state.bootstrap.manage === true
    && state.bootstrap.authenticated === true
    && csrfToken.length > 0
  );
  state.mode = managed ? "manage" : "read_only";
  state.authenticated = managed;
  state.csrfToken = managed ? csrfToken : "";
  window.history.replaceState(
    managed ? { unified_cli_manage_csrf: state.csrfToken } : null,
    "", `${window.location.pathname}${window.location.search}`
  );
  state.providers = normalizeProviders(state.bootstrap.providers);
  state.workspaces = normalizeWorkspaces(state.bootstrap.workspaces);
  const language = textValue(state.bootstrap.lang || state.bootstrap.language, "").toLowerCase();
  if (language === "en" || language === "ko") state.language = language;
  byId("language-select").value = state.language;
  const preferredTheme = isRecord(state.bootstrap.settings) ? textValue(state.bootstrap.settings.theme, "auto").toLowerCase() : "auto";
  setTheme(preferredTheme);
  applyTranslations();
  renderMode();
  renderProviders();
  populateWorkspaceOptions();
  renderBootstrapSummaries();
  updateConnection(true, "online");
  announce(t("bootstrapReady"));
}

function summaryCount(value) {
  if (Array.isArray(value)) return String(value.length);
  if (isRecord(value)) return String(Object.keys(value).length);
  return textValue(value, "0", 40);
}

function summaryFlag(value) {
  if (typeof value === "boolean") return value ? t("enabled") : t("disabled");
  if (Array.isArray(value)) return `${value.length} ${t("countItems")}`;
  return textValue(value, t("unknown"), 300);
}

function renderBootstrapSummaries() {
  const data = state.bootstrap;
  const versions = isRecord(data.versions) ? data.versions : {};
  const appVersion = textValue(versions.unified_cli || versions.app, "");
  byId("version-label").textContent = appVersion ? `Unified CLI ${appVersion}` : "Unified CLI";
  const settings = isRecord(data.settings) ? data.settings : {};
  const defaults = isRecord(settings.defaults) ? settings.defaults : isRecord(data.defaults) ? data.defaults : settings;
  byId("default-provider").textContent = textValue(defaults.provider || defaults.default_provider, t("notAvailable"), 100);
  byId("default-model").textContent = textValue(defaults.model || defaults.default_model, t("notAvailable"), 200);
  const workspaceId = textValue(defaults.workspace_id || defaults.workspace, "");
  const workspace = state.workspaces.find((item) => item.id === workspaceId);
  byId("default-workspace").textContent = workspace ? workspace.name : textValue(workspaceId, t("notAvailable"), 500);
  const security = isRecord(data.security) ? data.security : {};
  byId("security-web").textContent = summaryFlag(Object.hasOwn(security, "web") ? security.web : Object.hasOwn(settings, "web") ? settings.web : security.web_access);
  byId("security-mcp").textContent = summaryFlag(security.mcp || security.mcp_servers);
  byId("security-workspaces").textContent = summaryCount(security.workspaces || data.workspaces);
  byId("security-allowlist").textContent = summaryFlag(security.server_allowlist || security.allowlist);
  const overviewSecurity = byId("overview-security");
  byId("overview-sessions").textContent = summaryCount(data.sessions && (data.sessions.items || data.sessions));
  const usage = isRecord(data.usage) ? data.usage : {};
  byId("overview-tokens").textContent = formatNumber(numberValue(usage.total_tokens || usage.tokens, 0));
  applySettings(settings);
}

function applySettings(settings) {
  if (!isRecord(settings)) return;
  state.bootstrap.settings = settings;
  const theme = textValue(settings.theme, state.theme).toLowerCase();
  setTheme(theme);
  byId("setting-web").checked = settings.web === true;
  byId("setting-prompt-preview").checked = settings.browser_prompt_preview === true;
  byId("setting-permission").value = "read_only";
  populateSettingsOptions();
}

async function loadProviderMetadata() {
  try {
    const data = await readJson(`${API_ROOT}/providers`);
    state.providers = normalizeProviders(isRecord(data) ? data.providers : data);
    renderProviders();
  } catch (error) {
    announce(errorMessage(error, t("loadFailed")));
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = byId("save-settings");
  const status = byId("settings-status");
  const defaultProvider = byId("setting-default-provider").value;
  button.disabled = true;
  status.textContent = t("loading");
  try {
    const payload = {
      lang: state.language,
      theme: byId("setting-theme").value,
      browser_permission: "read_only",
      browser_prompt_preview: byId("setting-prompt-preview").checked,
      default_provider: defaultProvider,
      workspace_id: byId("setting-workspace").value || null,
      web: byId("setting-web").checked
    };
    const data = await mutationFetch(`${API_ROOT}/settings`, { method: "PATCH", body: JSON.stringify(payload) });
    applySettings(data);
    renderBootstrapSummaries();
    status.textContent = t("settingsSaved");
    announce(t("settingsSaved"));
  } catch (error) {
    status.textContent = errorMessage(error, t("settingsFailed"));
    announce(t("settingsFailed"));
  } finally {
    button.disabled = state.mode !== "manage";
  }
}

function enterPlainMode() {
  state.mode = "plain";
  state.authenticated = false;
  state.csrfToken = "";
  bootstrapCsrf = "";
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  byId("read-only-banner").hidden = false;
  renderMode();
  updateConnection(false, "plainMode");
  byId("overview-mode-note").textContent = t("explicitOnly");
  announce(t("readOnlyTitle"));
}

async function bootstrap() {
  const headers = new Headers({ Accept: "application/json" });
  if (bootstrapToken) headers.set("X-Unified-Bootstrap", bootstrapToken);
  else if (bootstrapCsrf && MANAGE_TOKEN_PATTERN.test(bootstrapCsrf)) headers.set("X-CSRF-Token", bootstrapCsrf);
  bootstrapToken = "";
  bootstrapCsrf = "";
  try {
    const response = await fetch(`${API_ROOT}/bootstrap`, {
      method: "GET", headers, credentials: "same-origin", cache: "no-store"
    });
    if (response.status === 404) {
      enterPlainMode();
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyBootstrap(data);
    if (state.authenticated) {
      if (!state.providers.length) await loadProviderMetadata();
      connectEvents();
    }
  } catch (error) {
    state.mode = "read_only";
    state.authenticated = false;
    state.csrfToken = "";
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    renderMode();
    updateConnection(false, "bootstrapFailed");
    byId("read-only-banner").hidden = false;
    byId("read-only-banner").querySelector("span").textContent = errorMessage(error, t("bootstrapFailed"));
    announce(t("bootstrapFailed"));
  }
}

function eventPayload(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const parsed = JSON.parse(raw);
    return isRecord(parsed) ? parsed : null;
  } catch (_error) {
    return null;
  }
}

function applyStateEvent(data) {
  if (!isRecord(data)) return;
  const payload = isRecord(data.data) ? data.data : data;
  if (payload.providers) state.providers = normalizeProviders(payload.providers);
  if (payload.workspaces) state.workspaces = normalizeWorkspaces(payload.workspaces);
  if (payload.providers) renderProviders();
  if (payload.workspaces) populateWorkspaceOptions();
  if (payload.sessions) byId("overview-sessions").textContent = summaryCount(payload.sessions.items || payload.sessions);
  if (payload.usage) {
    const usage = isRecord(payload.usage) ? payload.usage : {};
    byId("overview-tokens").textContent = formatNumber(numberValue(usage.total_tokens || usage.tokens, 0));
  }
  updateConnection(true, "online");
}

function clearEventTimers() {
  if (state.eventRetryTimer !== null) window.clearTimeout(state.eventRetryTimer);
  state.eventRetryTimer = null;
}

function closeEvents() {
  if (state.eventController) state.eventController.abort();
  state.eventController = null;
  clearEventTimers();
}

function scheduleEventRetry() {
  if (document.hidden || !state.authenticated || state.eventRetryTimer !== null) return;
  state.eventRetryTimer = window.setTimeout(() => {
    state.eventRetryTimer = null;
    connectEvents();
  }, EVENT_RETRY_MS);
}

function applyEventFrame(frame) {
  let eventName = "message";
  const dataLines = [];
  frame.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  if (!dataLines.length || eventName === "heartbeat") return;
  const data = eventPayload(dataLines.join("\n"));
  if (data && ["message", "state", "provider", "session", "usage"].includes(eventName)) applyStateEvent(data);
}

async function connectEvents() {
  if (document.hidden || !state.authenticated || state.eventController) return;
  clearEventTimers();
  const controller = new AbortController();
  state.eventController = controller;
  try {
    const response = await fetch(`${API_ROOT}/events`, {
      method: "GET",
      headers: { Accept: "text/event-stream", "X-CSRF-Token": state.csrfToken },
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal
    });
    if (!response.ok || !response.body || !(response.headers.get("content-type") || "").includes("text/event-stream")) {
      throw new Error("event stream unavailable");
    }
    updateConnection(true, "online");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!controller.signal.aborted) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      if (buffer.length > 65_536) throw new Error("event stream exceeded its limit");
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        applyEventFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (_error) {
    if (controller.signal.aborted) return;
    updateConnection(false, "offline");
  } finally {
    if (state.eventController === controller) state.eventController = null;
    if (!controller.signal.aborted) scheduleEventRetry();
  }
}

function handleVisibilityChange() {
  if (document.hidden) {
    closeEvents();
    updateConnection(false, "offline");
  } else if (state.authenticated) {
    connectEvents();
  }
}

async function readJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (state.authenticated && state.csrfToken) headers.set("X-CSRF-Token", state.csrfToken);
  const response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const type = response.headers.get("content-type") || "";
  if (!type.includes("json")) throw new Error("Unexpected response type");
  return response.json();
}

async function mutationFetch(url, options = {}) {
  if (state.mode !== "manage") throw new Error(t("readOnlyMode"));
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  headers.set("Content-Type", "application/json");
  headers.set("X-CSRF-Token", state.csrfToken);
  const response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  if (response.status === 204) return {};
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : {};
}

function errorMessage(error, fallback) {
  if (error instanceof Error) return textValue(error.message, fallback, 300);
  return fallback;
}

function formatNumber(value) {
  return new Intl.NumberFormat(state.language === "ko" ? "ko-KR" : "en-US", { maximumFractionDigits: 1 }).format(numberValue(value, 0));
}

function timestampMilliseconds(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.abs(value) < 1_000_000_000_000 ? value * 1_000 : value;
  }
  if (typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value.trim())) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric < 1_000_000_000_000 ? numeric * 1_000 : numeric;
  }
  return new Date(value).getTime();
}

function formatDate(value) {
  const date = new Date(timestampMilliseconds(value));
  if (Number.isNaN(date.getTime())) return t("unknown");
  return new Intl.DateTimeFormat(state.language === "ko" ? "ko-KR" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

async function refreshOverview() {
  const button = byId("refresh-overview");
  button.disabled = true;
  try {
    if (state.mode === "plain") {
      const results = await Promise.allSettled([readJson("/v1/usage"), readJson("/v1/conversations")]);
      if (results[0].status === "fulfilled") {
        state.usage = results[0].value;
        updateOverviewFromUsage(state.usage);
      }
      if (results[1].status === "fulfilled") {
        const rows = sessionItems(results[1].value);
        byId("overview-sessions").textContent = formatNumber(rows.length);
      }
    } else if (state.authenticated) {
      const results = await Promise.allSettled([
        readJson(`${API_ROOT}/providers`),
        readJson(`${API_ROOT}/sessions`),
        readJson(`${API_ROOT}/usage`)
      ]);
      if (results[0].status === "fulfilled") {
        const providerData = results[0].value;
        state.providers = normalizeProviders(isRecord(providerData) ? providerData.providers : providerData);
        renderProviders();
      }
      if (results[1].status === "fulfilled") byId("overview-sessions").textContent = formatNumber(sessionItems(results[1].value).length);
      if (results[2].status === "fulfilled") updateOverviewFromUsage(results[2].value);
    }
    announce(t("refreshComplete"));
  } catch (error) {
    announce(errorMessage(error, t("loadFailed")));
  } finally {
    button.disabled = false;
  }
}

function updateOverviewFromUsage(value) {
  if (!isRecord(value)) return;
  const totals = isRecord(value.totals) ? value.totals : value;
  const rows = usageItems(value);
  const calculated = usageTotals(value, rows);
  byId("overview-tokens").textContent = formatNumber(numberValue(totals.total_tokens || totals.tokens, calculated.input + calculated.output));
}

async function loadModels(provider, force = false) {
  if (!provider || state.modelsLoading) return;
  if (!force && state.modelsByProvider.has(provider)) {
    renderModels(state.modelsByProvider.get(provider));
    return;
  }
  state.modelsLoading = true;
  byId("refresh-models").disabled = true;
  byId("models-status").textContent = t("loading");
  try {
    const data = await mutationFetch(
      `${API_ROOT}/providers/${encodeURIComponent(provider)}/models`,
      { method: "POST", body: "{}" }
    );
    const rows = Array.isArray(data) ? data : listValue(data.models || data.data);
    state.modelsByProvider.set(provider, rows.filter(isRecord).slice(0, MAX_MODEL_ROWS));
    renderModels(state.modelsByProvider.get(provider));
    byId("models-status").textContent = t("modelsLoaded");
    populateChatModels(provider);
  } catch (error) {
    byId("models-status").textContent = errorMessage(error, t("loadFailed"));
  } finally {
    state.modelsLoading = false;
    byId("refresh-models").disabled = !byId("models-provider").value;
  }
}

function renderModels(rows) {
  const body = byId("models-table-body");
  clearNode(body);
  if (!rows.length) {
    const row = makeElement("tr");
    const cell = makeElement("td", "empty-state", t("noModels"));
    cell.colSpan = 4;
    row.append(cell);
    body.append(row);
    return;
  }
  rows.slice(0, MAX_MODEL_ROWS).forEach((model) => {
    const row = makeElement("tr");
    row.append(
      makeElement("td", "", textValue(model.id || model.name || model.model, t("unknown"), 300)),
      makeElement("td", "", textValue(model.source || model.owned_by, t("unknown"), 120)),
      makeElement("td", "", textValue(model.context || model.context_window || model.max_tokens, t("contextUnknown"), 80)),
      makeElement("td", "", capabilityText(model.capabilities || model.features))
    );
    body.append(row);
  });
}

function populateChatModels(provider) {
  const select = byId("chat-model");
  const current = resetOptions(select, "defaultModel");
  const rows = state.modelsByProvider.get(provider) || [];
  rows.forEach((model) => {
    const id = textValue(model.id || model.name || model.model, "", 300);
    if (!id) return;
    const option = makeElement("option", "", id);
    option.value = id;
    select.append(option);
  });
  if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
}

function sessionItems(value) {
  if (Array.isArray(value)) return value.filter(isRecord).slice(0, MAX_SESSION_ROWS);
  if (!isRecord(value)) return [];
  const rows = value.sessions || value.items || value.data || value.conversations;
  return listValue(rows).filter(isRecord).slice(0, MAX_SESSION_ROWS);
}

async function loadSessions() {
  const button = byId("refresh-sessions");
  button.disabled = true;
  try {
    const url = state.mode === "plain" ? "/v1/conversations" : `${API_ROOT}/sessions`;
    const data = await readJson(url);
    renderSessions(sessionItems(data));
    announce(t("refreshComplete"));
  } catch (error) {
    renderTableError(byId("sessions-table-body"), 5, errorMessage(error, t("loadFailed")));
  } finally {
    button.disabled = false;
  }
}

function sessionKind(item) {
  const raw = textValue(item.kind || item.type || item.scope, "core").toLowerCase();
  if (raw === "ext" || raw === "external" || item.external === true) return "ext";
  const descriptor = state.providers.find((provider) => providerId(provider) === textValue(item.provider, ""));
  return descriptor && textValue(descriptor.source, "").toLowerCase() === "ext" ? "ext" : "core";
}

function sessionId(item) {
  return textValue(item.id || item.session_id || item.conversation_id, "", 500);
}

function renderSessions(rows) {
  const body = byId("sessions-table-body");
  clearNode(body);
  if (!rows.length) {
    const row = makeElement("tr");
    const cell = makeElement("td", "empty-state", t("noSessions"));
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  rows.forEach((item) => {
    const id = sessionId(item);
    const kind = sessionKind(item);
    const row = makeElement("tr");
    row.append(
      makeElement("td", "", kind === "ext" ? t("ext") : t("core")),
      makeElement("td", "", textValue(item.name || item.title || id, t("unknown"), 300)),
      makeElement("td", "", textValue(item.provider || item.last_provider, t("unknown"), 80)),
      makeElement("td", "", formatDate(item.updated_at || item.updated || item.created_at))
    );
    const actions = makeElement("td");
    if (state.mode === "manage" && id) {
      const descriptor = state.providers.find((provider) => providerId(provider) === textValue(item.provider, ""));
      const canResume = Boolean(item.workspace_id) && (!descriptor || descriptor.chat_supported !== false);
      const resume = sessionButton(t("resume"), () => resumeSession(item));
      resume.disabled = !canResume;
      actions.append(
        resume,
        sessionButton(t("rename"), () => beginRename(item, actions)),
        sessionButton(t("archive"), () => confirmSessionAction(id, "archive", t("archiveSessionQuestion"))),
        sessionButton(t("delete"), () => confirmSessionAction(id, "delete", t("deleteSessionQuestion")), "text-button danger-text")
      );
    } else {
      actions.textContent = t("readOnlyMode");
    }
    row.append(actions);
    body.append(row);
  });
}

function sessionButton(label, callback, className = "text-button") {
  const button = makeElement("button", className, label);
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}

function beginRename(item, cell) {
  clearNode(cell);
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 160;
  input.value = textValue(item.name || item.title, "", 160);
  input.setAttribute("aria-label", t("renameSession"));
  const save = sessionButton(t("save"), async () => {
    const name = input.value.trim();
    if (!name) return;
    await mutateSession(sessionId(item), "rename", { name });
  }, "button primary");
  const cancel = sessionButton(t("cancel"), loadSessions, "button secondary");
  cell.append(input, save, cancel);
  input.focus();
}

async function askConfirmation(message) {
  const dialog = byId("confirm-dialog");
  byId("confirm-message").textContent = message;
  if (typeof dialog.showModal !== "function") return window.confirm(message);
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
  });
}

async function confirmSessionAction(id, action, question) {
  const confirmed = await askConfirmation(question);
  if (confirmed) await mutateSession(id, action);
}

function resumeSession(item) {
  const provider = textValue(item.provider, "");
  const workspace = textValue(item.workspace_id, "");
  const id = sessionId(item);
  if (!provider || !workspace || !id) return;
  byId("chat-provider").value = provider;
  byId("chat-workspace").value = workspace;
  state.resumeSessionId = id;
  if (state.modelsByProvider.has(provider)) populateChatModels(provider);
  showView("chat");
  byId("chat-session-label").textContent = `${t("session")}: ${textValue(item.name || id, id, 200)}`;
  byId("chat-prompt").focus();
}

async function mutateSession(id, action, payload = {}) {
  try {
    const base = `${API_ROOT}/sessions/${encodeURIComponent(id)}`;
    let method = "PATCH";
    let url = base;
    let body = payload;
    if (action === "delete") {
      method = "DELETE";
      url = base;
    } else if (action === "rename") {
      method = "PATCH";
    } else if (action === "archive") {
      body = { archived: true };
    }
    await mutationFetch(url, { method, body: JSON.stringify(body) });
    announce(t("sessionActionComplete"));
    await loadSessions();
  } catch (error) {
    announce(errorMessage(error, t("sessionActionFailed")));
  }
}

function usageItems(value) {
  if (Array.isArray(value)) return value.filter(isRecord).slice(0, MAX_USAGE_ROWS);
  if (!isRecord(value)) return [];
  return listValue(value.calls || value.recent || value.items || value.data).filter(isRecord).slice(0, MAX_USAGE_ROWS);
}

function usageTotals(value, rows) {
  const totals = isRecord(value) && isRecord(value.totals) ? value.totals : isRecord(value) ? value : {};
  const sum = (keys) => rows.reduce((total, row) => total + numberValue(keys.map((key) => row[key]).find((item) => item !== undefined), 0), 0);
  return {
    input: numberValue(totals.input_tokens || totals.prompt_tokens, sum(["input_tokens", "prompt_tokens"])),
    output: numberValue(totals.output_tokens || totals.completion_tokens, sum(["output_tokens", "completion_tokens"])),
    cached: numberValue(totals.cached_tokens || totals.cache_read_tokens, sum(["cached_tokens", "cache_read_tokens"]))
  };
}

async function loadUsage() {
  const button = byId("refresh-usage");
  button.disabled = true;
  try {
    const url = state.mode === "plain" ? "/v1/usage" : `${API_ROOT}/usage`;
    state.usage = await readJson(url);
    renderUsage();
    updateOverviewFromUsage(state.usage);
    byId("export-usage").disabled = false;
    announce(t("refreshComplete"));
  } catch (error) {
    renderTableError(byId("usage-table-body"), 6, errorMessage(error, t("loadFailed")));
  } finally {
    button.disabled = false;
  }
}

function filteredUsageRows() {
  let rows = usageItems(state.usage);
  const provider = byId("usage-provider").value;
  const result = byId("usage-errors").value;
  const windowValue = byId("usage-window").value;
  const now = Date.now();
  rows = rows.filter((row) => {
    const matchesProvider = !provider || textValue(row.provider, "", 80) === provider;
    const failed = row.error === true || Boolean(textValue(row.error || row.error_code || row.error_kind, "")) || textValue(row.status, "").toLowerCase() === "error";
    const matchesResult = result === "all" || (result === "errors" && failed) || (result === "success" && !failed);
    const timestamp = timestampMilliseconds(row.timestamp || row.time || row.ts || row.created_at);
    const cutoff = windowValue === "hour" ? 3_600_000 : windowValue === "day" ? 86_400_000 : Infinity;
    const matchesWindow = cutoff === Infinity || (Number.isFinite(timestamp) && now - timestamp <= cutoff);
    return matchesProvider && matchesResult && matchesWindow;
  });
  return rows.slice(0, MAX_USAGE_ROWS);
}

function renderUsage() {
  if (!state.usage) return;
  const allRows = usageItems(state.usage);
  const rows = filteredUsageRows();
  const totals = usageTotals(state.usage, rows);
  byId("usage-input").textContent = formatNumber(totals.input);
  byId("usage-output").textContent = formatNumber(totals.output);
  byId("usage-cached").textContent = formatNumber(totals.cached);
  const errorCount = rows.filter((row) => row.error === true || textValue(row.status, "").toLowerCase() === "error" || Boolean(textValue(row.error_code || row.error_kind, ""))).length;
  byId("usage-errors-rate").textContent = rows.length ? `${((errorCount / rows.length) * 100).toFixed(1)}%` : "0%";
  byId("usage-error-note").textContent = `${errorCount} ${t("errors")} / ${rows.length} ${t("calls")}`;
  const body = byId("usage-table-body");
  clearNode(body);
  if (!rows.length) {
    const row = makeElement("tr");
    const cell = makeElement("td", "empty-state", t("noUsage"));
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  rows.forEach((item) => {
    const failed = item.error === true || textValue(item.status, "").toLowerCase() === "error" || Boolean(textValue(item.error_code || item.error_kind, ""));
    const tokens = numberValue(item.total_tokens, numberValue(item.input_tokens || item.prompt_tokens, 0) + numberValue(item.output_tokens || item.completion_tokens, 0));
    const cached = numberValue(item.cached_tokens || item.cache_read_tokens, 0);
    const row = makeElement("tr");
    row.append(
      makeElement("td", "", formatDate(item.timestamp || item.time || item.ts || item.created_at)),
      makeElement("td", "", `${textValue(item.provider, t("unknown"), 80)} / ${textValue(item.model, t("unknown"), 200)}`),
      makeElement("td", "", `${formatNumber(tokens)} / ${formatNumber(cached)}`),
      makeElement("td", "", formatDuration(item.ttft_ms || item.time_to_first_token_ms)),
      makeElement("td", "", formatDuration(item.latency_ms || item.duration_ms)),
      makeElement("td", "", failed ? t("error") : t("success"))
    );
    body.append(row);
  });
  if (allRows.length > MAX_USAGE_ROWS) announce(`${MAX_USAGE_ROWS} ${t("countItems")}`);
}

function formatDuration(value) {
  const amount = numberValue(value, -1);
  return amount >= 0 ? `${formatNumber(amount)} ms` : "—";
}

function renderTableError(body, columns, message) {
  clearNode(body);
  const row = makeElement("tr");
  const cell = makeElement("td", "empty-state", message);
  cell.colSpan = columns;
  row.append(cell);
  body.append(row);
}

function exportUsage() {
  if (!state.usage) return;
  const safeExport = {
    exported_at: new Date().toISOString(),
    filters: { provider: byId("usage-provider").value, result: byId("usage-errors").value, window: byId("usage-window").value },
    rows: filteredUsageRows().map((row) => ({
      timestamp: formatDate(row.timestamp || row.time || row.ts || row.created_at),
      provider: textValue(row.provider, t("unknown"), 80),
      model: textValue(row.model, t("unknown"), 200),
      input_tokens: numberValue(row.input_tokens || row.prompt_tokens, 0),
      output_tokens: numberValue(row.output_tokens || row.completion_tokens, 0),
      cached_tokens: numberValue(row.cached_tokens || row.cache_read_tokens, 0),
      total_tokens: numberValue(row.total_tokens, numberValue(row.input_tokens || row.prompt_tokens, 0) + numberValue(row.output_tokens || row.completion_tokens, 0)),
      ttft_ms: numberValue(row.ttft_ms || row.time_to_first_token_ms, 0),
      latency_ms: numberValue(row.latency_ms || row.duration_ms, 0),
      status: row.error === true ? "error" : textValue(row.status, "success", 40),
      error_code: textValue(row.error_code || row.error_kind, "", 80)
    }))
  };
  const blob = new Blob([JSON.stringify(safeExport, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `unified-cli-usage-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  URL.revokeObjectURL(url);
  announce(t("exportReady"));
}

function releaseImages() {
  state.images = [];
  clearNode(byId("image-preview"));
  byId("image-picker").value = "";
}

function handleImages(files) {
  releaseImages();
  let total = 0;
  let rejected = false;
  Array.from(files).slice(0, MAX_IMAGES).forEach((file) => {
    const allowed = ["image/png", "image/jpeg", "image/webp"].includes(file.type);
    if (!allowed || file.size > MAX_IMAGE_BYTES || total + file.size > MAX_IMAGE_TOTAL_BYTES) {
      rejected = true;
      return;
    }
    total += file.size;
    const item = { file, previewUrl: "" };
    state.images.push(item);
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      item.previewUrl = typeof reader.result === "string" ? reader.result : "";
      renderImages();
    });
    reader.readAsDataURL(file);
  });
  if (files.length > MAX_IMAGES) rejected = true;
  renderImages();
  if (rejected) announce(t("imageRejected"));
}

function renderImages() {
  const target = byId("image-preview");
  clearNode(target);
  state.images.forEach((item, index) => {
    const wrapper = makeElement("div", "image-item");
    const image = document.createElement("img");
    if (item.previewUrl) image.src = item.previewUrl;
    image.alt = textValue(item.file.name, t("addImages"), 180);
    const label = makeElement("span", "", textValue(item.file.name, t("addImages"), 180));
    const remove = makeElement("button", "icon-button", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("removeImage")}: ${label.textContent}`);
    remove.addEventListener("click", () => {
      state.images.splice(index, 1);
      renderImages();
    });
    wrapper.append(image, label, remove);
    target.append(wrapper);
  });
}

function fileAsPayload(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      resolve(result);
    });
    reader.addEventListener("error", () => reject(new Error(t("imageRejected"))));
    reader.readAsDataURL(file);
  });
}

function prepareChatOutput() {
  state.chatText = "";
  state.tools.clear();
  clearNode(byId("chat-output"));
  clearNode(byId("tool-timeline"));
  const paragraph = makeElement("p");
  state.chatTextNode = document.createTextNode("");
  paragraph.append(state.chatTextNode);
  byId("chat-output").append(paragraph);
  byId("chat-session-label").textContent = "";
}

function appendChatText(value) {
  if (typeof value !== "string" || !value) return;
  state.chatText = `${state.chatText}${value}`;
  if (state.chatText.length > MAX_CHAT_CHARS) state.chatText = state.chatText.slice(-MAX_CHAT_CHARS);
  if (state.chatTextNode) state.chatTextNode.nodeValue = state.chatText;
  const output = byId("chat-output");
  output.scrollTop = output.scrollHeight;
}

function appendReasoningSummary(value) {
  const summary = textValue(value, "", 4_000);
  if (!summary) return;
  const box = makeElement("aside", "reasoning-summary");
  box.append(makeElement("strong", "", t("reasoningSummary")), makeElement("p", "", summary));
  byId("chat-output").append(box);
}

function toolKey(event) {
  return textValue(event.tool_call_id || event.call_id || event.id, `${textValue(event.name || event.tool, "tool", 100)}-${state.tools.size}`, 300);
}

function renderToolEvent(event, finished) {
  const key = toolKey(event);
  let entry = state.tools.get(key);
  if (!entry) {
    const row = makeElement("li", "timeline-item");
    const mark = makeElement("span", "", "◌");
    mark.setAttribute("aria-hidden", "true");
    const copy = makeElement("div");
    copy.append(makeElement("strong", "", textValue(event.name || event.tool, t("unknown"), 120)), makeElement("small", "", textValue(event.summary || event.input_summary, key, 300)));
    const status = makeElement("span", "badge badge-neutral", t("toolStarted"));
    row.append(mark, copy, status);
    byId("tool-timeline").append(row);
    entry = { row, status, mark };
    state.tools.set(key, entry);
    while (byId("tool-timeline").children.length > MAX_TOOL_ROWS) {
      const first = byId("tool-timeline").firstElementChild;
      if (first) first.remove();
      const oldest = state.tools.keys().next().value;
      if (oldest !== undefined) state.tools.delete(oldest);
    }
  }
  if (finished) {
    const failed = event.error === true || textValue(event.status, "").toLowerCase() === "error";
    entry.mark.textContent = failed ? "!" : "✓";
    entry.status.textContent = failed ? t("toolFailed") : t("toolFinished");
    entry.status.className = failed ? "badge badge-danger" : "badge badge-success";
  }
}

function handleChatEvent(event) {
  if (!isRecord(event)) return;
  const type = textValue(event.type || event.event, "", 80);
  if (type === "session") {
    state.chatId = textValue(event.chat_id || event.session_id || event.id, state.chatId, 500);
    const resumable = textValue(event.session_id, "", 500);
    if (resumable) state.resumeSessionId = resumable;
    byId("chat-session-label").textContent = state.chatId ? `${t("session")}: ${state.chatId}` : t("session");
  } else if (type === "text_delta") {
    appendChatText(typeof event.delta === "string" ? event.delta : typeof event.text === "string" ? event.text : "");
  } else if (type === "reasoning_summary") {
    appendReasoningSummary(event.summary || event.text);
  } else if (type === "tool_started") {
    renderToolEvent(event, false);
  } else if (type === "tool_finished") {
    renderToolEvent(event, true);
  } else if (type === "usage") {
    const usage = isRecord(event.usage) ? event.usage : event;
    const amount = numberValue(usage.total_tokens, numberValue(usage.input_tokens, 0) + numberValue(usage.output_tokens, 0));
    byId("chat-status").textContent = `${t("chatStreaming")} · ${formatNumber(amount)} tokens`;
  } else if (type === "error") {
    throw new Error(textValue(event.message || event.error, t("chatFailed"), 500));
  } else if (type === "done") {
    const status = textValue(event.status, "completed").toLowerCase();
    byId("chat-status").textContent = status === "cancelled" ? t("chatCancelled") : status === "error" ? t("chatFailed") : t("chatDone");
  }
}

async function readChatStream(response) {
  if (!response.body) throw new Error(t("chatFailed"));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const result = await reader.read();
    if (result.done) break;
    buffer += decoder.decode(result.value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    lines.forEach((line) => {
      const clean = line.trim();
      if (!clean) return;
      try {
        handleChatEvent(JSON.parse(clean));
      } catch (error) {
        if (error instanceof SyntaxError) return;
        throw error;
      }
    });
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    try { handleChatEvent(JSON.parse(buffer)); } catch (error) { if (!(error instanceof SyntaxError)) throw error; }
  }
}

async function submitChat(event) {
  event.preventDefault();
  if (state.mode !== "manage" || state.chatController) return;
  const provider = byId("chat-provider").value;
  const workspace = byId("chat-workspace").value;
  const prompt = byId("chat-prompt").value.trim();
  if (!prompt) {
    byId("chat-status").textContent = t("promptRequired");
    byId("chat-prompt").focus();
    return;
  }
  if (!provider || !workspace) {
    byId("chat-status").textContent = t("selectionRequired");
    return;
  }
  prepareChatOutput();
  state.chatController = new AbortController();
  state.chatId = "";
  renderMode();
  byId("chat-status").textContent = t("chatStarting");
  try {
    const images = await Promise.all(state.images.map((item) => fileAsPayload(item.file)));
    const headers = new Headers({ Accept: "application/x-ndjson", "Content-Type": "application/json" });
    headers.set("X-CSRF-Token", state.csrfToken);
    const response = await fetch(`${API_ROOT}/chat`, {
      method: "POST", credentials: "same-origin", cache: "no-store", headers,
      signal: state.chatController.signal,
      body: JSON.stringify({
        provider,
        model: byId("chat-model").value || null,
        workspace_id: workspace,
        permission: "read_only",
        session_id: state.resumeSessionId || null,
        prompt,
        images
      })
    });
    state.chatId = textValue(response.headers.get("X-Unified-Chat-Id"), state.chatId, 500);
    if (state.chatId) byId("chat-session-label").textContent = `${t("session")}: ${state.chatId}`;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    byId("chat-status").textContent = t("chatStreaming");
    await readChatStream(response);
    if (byId("chat-status").textContent === t("chatStreaming")) byId("chat-status").textContent = t("chatDone");
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") byId("chat-status").textContent = t("chatCancelled");
    else byId("chat-status").textContent = errorMessage(error, t("chatFailed"));
  } finally {
    state.chatController = null;
    renderMode();
  }
}

async function cancelChat() {
  if (!state.chatController) return;
  const controller = state.chatController;
  const id = state.chatId;
  byId("chat-status").textContent = t("cancelRequested");
  if (!id) {
    controller.abort();
    return;
  }
  try {
    const path = `${API_ROOT}/chat/${encodeURIComponent(id)}/cancel`;
    await mutationFetch(path, { method: "POST", body: JSON.stringify({ chat_id: id }) });
  } catch (_error) {
    announce(t("chatCancelled"));
  } finally {
    controller.abort();
    byId("chat-status").textContent = t("chatCancelled");
  }
}

function wireEvents() {
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  document.querySelectorAll("[data-open-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.openView)));
  byId("language-select").addEventListener("change", (event) => {
    state.language = event.target.value === "ko" ? "ko" : "en";
    applyTranslations();
  });
  byId("theme-select").addEventListener("change", (event) => setTheme(event.target.value));
  byId("setting-theme").addEventListener("change", (event) => setTheme(event.target.value));
  byId("refresh-overview").addEventListener("click", refreshOverview);
  byId("models-provider").addEventListener("change", (event) => {
    const provider = event.target.value;
    byId("refresh-models").disabled = !provider;
    if (provider) loadModels(provider);
  });
  byId("refresh-models").addEventListener("click", () => loadModels(byId("models-provider").value, true));
  byId("chat-provider").addEventListener("change", (event) => {
    const provider = event.target.value;
    state.resumeSessionId = "";
    byId("chat-session-label").textContent = "";
    if (state.modelsByProvider.has(provider)) populateChatModels(provider);
    else resetOptions(byId("chat-model"), "defaultModel");
  });
  byId("chat-workspace").addEventListener("change", () => {
    state.resumeSessionId = "";
    byId("chat-session-label").textContent = "";
  });
  byId("refresh-sessions").addEventListener("click", loadSessions);
  byId("refresh-usage").addEventListener("click", loadUsage);
  byId("export-usage").addEventListener("click", exportUsage);
  byId("usage-filters").addEventListener("change", renderUsage);
  byId("image-picker").addEventListener("change", (event) => handleImages(event.target.files || []));
  byId("chat-form").addEventListener("submit", submitChat);
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("cancel-chat").addEventListener("click", cancelChat);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  const dialog = byId("confirm-dialog");
  dialog.addEventListener("close", () => {
    if (state.confirmResolve) state.confirmResolve(dialog.returnValue === "confirm");
    state.confirmResolve = null;
  });
  window.addEventListener("pagehide", () => {
    closeEvents();
    releaseImages();
    if (state.chatController) state.chatController.abort();
  });
}

wireEvents();
setTheme("auto");
applyTranslations();
renderMode();
bootstrap();
