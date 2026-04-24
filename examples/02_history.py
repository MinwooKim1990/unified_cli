"""Example 2 — 한 provider 안에서 히스토리 이어쓰기.

두 가지 방법:
  A) session_id 를 수동으로 넘긴다
  B) UnifiedConversation(sticky=True) 으로 관리를 맡긴다
"""
from unified_cli import UnifiedConversation, create

print("--- 방법 A: session_id 수동 전달 ---")
cli = create("claude", web_search=False)
r1 = cli.chat("내 이름은 민우야.")
r2 = cli.chat("내 이름 뭐였지? 한 단어로.", session_id=r1.session_id)
print("→", r2.text.strip()[:60])

print("\n--- 방법 B: Conversation(sticky=True) ---")
conv = UnifiedConversation(
    default_provider="codex",
    sticky=True,
    provider_opts={"web_search": False},
)
conv.send("내가 좋아하는 색은 파란색이야.")
r = conv.send("내가 좋아하는 색 뭐였지? 한 단어로.")
print("→", r.text.strip()[:60])
