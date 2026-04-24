"""Example 6 — 에러 분류 + fallback.

모든 실패는 UnifiedError(kind=..., provider=..., message=..., hint=...) 로 올라옴.
kind 값:
  auth_expired / rate_limit / model_not_allowed /
  not_found / network / config / internal
"""
from unified_cli import UnifiedError, create

# 1) 잘못된 모델 — model_not_allowed / not_found
print("--- Case A: unknown model ---")
try:
    cli = create("gemini", model="gemini-nonexistent-xyz", web_search=False)
    cli.chat("hi")
except UnifiedError as e:
    print(f"  kind={e.kind}  provider={e.provider}")
    print(f"  msg={e.message}")
    print(f"  hint={e.hint}")

# 2) 존재하지 않는 session_id — not_found
print("\n--- Case B: invalid session_id ---")
try:
    cli = create("gemini", web_search=False)
    cli.chat("hi", session_id="00000000-0000-0000-0000-000000000000")
except UnifiedError as e:
    print(f"  kind={e.kind}  msg={e.message[:80]}")

# 3) 잘못된 provider 이름 — config
print("\n--- Case C: bad provider name ---")
try:
    create("gpt4", model="hi")  # type: ignore[arg-type]
except UnifiedError as e:
    print(f"  kind={e.kind}  msg={e.message}")

# 4) str(err) 는 사람이 읽기 좋은 포맷
print("\n--- Case D: pretty-print ---")
try:
    create("gemini", model="gemini-nope", web_search=False).chat("hi")
except UnifiedError as e:
    print(str(e))
