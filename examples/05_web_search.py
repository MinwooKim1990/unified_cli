"""Example 5 — 웹서치 (3개 provider 모두 기본 ON).

웹서치 이벤트(Message{kind="tool_use"}):
  Claude:  tool.name = "WebSearch"
  Codex:   tool.name = "web_search"
  Gemini(agy): 에이전틱이라 tool_use 이벤트를 별도로 방출하지 않음 — 평문 답변만.
"""
from unified_cli import create

QUERY = "웹검색으로 '파이썬 3.14' 가 릴리스된 연도만 숫자로"

for provider in ("claude", "codex", "gemini"):
    cli = create(provider)   # web_search=True 가 기본
    tools_used: list[str] = []
    chunks: list[str] = []
    for msg in cli.stream(QUERY):
        if msg.kind == "tool_use":
            tools_used.append((msg.tool or {}).get("name", "?"))
        elif msg.kind == "text" and msg.text:
            chunks.append(msg.text)
    print(f"[{provider}] tools={tools_used}")
    print(f"          answer={''.join(chunks).strip()[:80]!r}\n")
