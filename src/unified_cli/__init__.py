"""unified_cli — one Python API for supported coding-agent CLIs.

Quick start (Python):

    from unified_cli import create, UnifiedConversation

    # Single call
    resp = create("claude").chat("안녕")
    print(resp.text, resp.session_id)

    # Bundled Preview providers use the same public API (example: Grok).
    # Configure/login once with the provider's official CLI before calling it.
    grok = create("grok", cwd="/absolute/project/path")
    for message in grok.stream("이 프로젝트를 요약해줘"):
        if message.kind == "text":
            print(message.text, end="", flush=True)

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
                # | network | resource_limit | config | internal
        e.hint  # 사용자용 복구 힌트
"""

__version__ = "0.5.3"

from .base import BaseProvider
from .conversation import UnifiedConversation
from .core import Message, ModelInfo, ProviderId, ProviderName, Response, Usage
from .errors import ErrorKind, UnifiedError, classify
from .extension_config import ExtensionLaunchOverridesV1, StoredExtensionLaunchV1
from .factory import PROVIDERS, create, route
from .models import DEFAULT_MODELS, invalidate_model_cache, list_models
from .plugin import (
    PROVIDER_CONFIGURATION_ABI_V1,
    PROVIDER_PLUGIN_ABI_V1,
    BoundProviderOperationsV1,
    ProviderBoundFactoryV1,
    ProviderCreateRequestV1,
    ProviderDoctorV1,
    ProviderFactoryV1,
    ProviderLaunchBinderV1,
    ProviderLaunchContextV1,
    ProviderModelListerV1,
    ProviderPluginV1,
    ProviderReceiptEnvelopeV1,
    ProviderServerPolicyV1,
    ProviderSupportStatusV1,
)
from .providers import ClaudeProvider, CodexProvider, GeminiProvider
from .registry import (
    ENTRY_POINT_GROUP,
    ProviderDescriptor,
    ProviderDescriptorV1,
    bind_extension_provider,
    clear_extension_provider_configuration,
    configure_extension_provider,
    doctor_provider,
    list_providers,
    load_provider_plugin,
    snapshot_provider_descriptor,
)
from .state import SessionState, load_last_session, save_last_session
from .usage import UsageTracker, tracker

__all__ = [
    "__version__",
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
    "PROVIDER_CONFIGURATION_ABI_V1",
    "PROVIDER_PLUGIN_ABI_V1",
    "BoundProviderOperationsV1",
    "ENTRY_POINT_GROUP",
    "ExtensionLaunchOverridesV1",
    "ProviderBoundFactoryV1",
    "ProviderDescriptor",
    "ProviderDescriptorV1",
    "ProviderCreateRequestV1",
    "ProviderDoctorV1",
    "ProviderFactoryV1",
    "ProviderId",
    "ProviderLaunchBinderV1",
    "ProviderLaunchContextV1",
    "ProviderName",
    "ProviderModelListerV1",
    "ProviderPluginV1",
    "ProviderReceiptEnvelopeV1",
    "ProviderServerPolicyV1",
    "ProviderSupportStatusV1",
    "Response",
    "StoredExtensionLaunchV1",
    "Usage",
    "DEFAULT_MODELS",
    "PROVIDERS",
    "bind_extension_provider",
    "classify",
    "clear_extension_provider_configuration",
    "configure_extension_provider",
    "create",
    "doctor_provider",
    "invalidate_model_cache",
    "list_models",
    "list_providers",
    "load_provider_plugin",
    "load_last_session",
    "route",
    "save_last_session",
    "snapshot_provider_descriptor",
    "tracker",
]
