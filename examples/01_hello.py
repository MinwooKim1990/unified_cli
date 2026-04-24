"""Example 1 — 가장 단순한 한 줄 호출.

3가지 provider 모두 동일한 API.  기본 모델 사용.
"""
from unified_cli import create

for provider in ("claude", "codex", "gemini"):
    cli = create(provider, web_search=False)     # 토큰 아끼려고 web_search off
    resp = cli.chat("say just: hi")
    print(
        f"[{provider}] model={resp.model}  "
        f"text={resp.text!r}  "
        f"tokens={resp.usage.input_tokens}/{resp.usage.output_tokens}"
    )
