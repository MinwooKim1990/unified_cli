"""unified_cli — one Python + CLI API for Claude Code / Codex / Gemini.

Quick start (Python):

    from unified_cli import create, UnifiedConversation

    # Single call
    resp = create("claude").chat("안녕")
    print(resp.text, resp.session_id)

    # Manual history (external code manages session_id)
    cli = create("codex")
    r1 = cli.chat("내 이름은 민우")
    r2 = cli.chat("내 이름?", session_id=r1.session_id)

    # Wrapper-managed history (+ cross-provider context injection)
    conv = UnifiedConversation()
    conv.send("내 이름 민우", provider="claude")
    conv.send("내 이름?", provider="gemini")   # auto-preserves context

Quick start (CLI):

    unified-cli setup              # first-time onboarding
    unified-cli chat "..."          # single call
    unified-cli chat "..." -c       # continue last saved session
    unified-cli repl                # interactive REPL

Error handling:

    from unified_cli import UnifiedError
    try:
        create("claude").chat("")
    except UnifiedError as e:
        e.kind  # auth_expired | rate_limit | model_not_allowed | not_found
                # | network | config | internal
        e.hint  # 사용자용 복구 힌트
"""

from .base import BaseProvider
from .conversation import UnifiedConversation
from .core import Message, ModelInfo, ProviderName, Response, Usage
from .errors import ErrorKind, UnifiedError, classify
from .factory import PROVIDERS, create, route
from .models import DEFAULT_MODELS, list_models
from .providers import ClaudeProvider, CodexProvider, GeminiProvider
from .state import SessionState, load_last_session, save_last_session
from .usage import UsageTracker, tracker

__all__ = [
    "BaseProvider",
    "ClaudeProvider",
    "CodexProvider",
    "GeminiProvider",
    "UnifiedConversation",
    "UnifiedError",
    "UsageTracker",
    "SessionState",
    "ErrorKind",
    "Message",
    "ModelInfo",
    "ProviderName",
    "Response",
    "Usage",
    "DEFAULT_MODELS",
    "PROVIDERS",
    "classify",
    "create",
    "list_models",
    "load_last_session",
    "route",
    "save_last_session",
    "tracker",
]
