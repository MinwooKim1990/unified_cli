"""Example 3 — 한 Conversation 에서 provider 자유 전환.

sticky=False (기본값) 에서는 provider 가 바뀔 때 직전 8턴 컨텍스트가
자동으로 새 provider 의 프롬프트 앞에 주입됨.
"""
from unified_cli import UnifiedConversation

conv = UnifiedConversation(provider_opts={"web_search": False})

# 1) Claude 에서 정보 저장
conv.send("내 이름은 민우, 좋아하는 색은 파란색.", provider="claude")

# 2) Codex 로 전환 — 이전 Claude 대화가 자동 주입됨
r = conv.send("내 이름과 색을 한 문장으로.", provider="codex")
print(f"[codex]  {r.text.strip()[:100]}")

# 3) Gemini 로 전환 — Claude+Codex 두 턴이 모두 컨텍스트로 주입
r = conv.send("내 이름만 한 단어로.", provider="gemini")
print(f"[gemini] {r.text.strip()[:60]}")

print(f"\nsessions: {conv.sessions}")
print(f"total turns: {len(conv.turns)}")
