"""Example 4 — 스트리밍 + 이벤트 종류별 처리.

`stream()` 이 yield 하는 Message 는 kind 로 구분됨:
  text | tool_use | tool_result | reasoning | session | usage | done | error
"""
from unified_cli import create

cli = create("claude", web_search=False)

print("Prompt → 'Python 한 줄 하이쿠 써줘'\n")
for msg in cli.stream("Python 한 줄 하이쿠 써줘."):
    if msg.kind == "text" and msg.text:
        print(msg.text, end="", flush=True)
    elif msg.kind == "tool_use":
        name = (msg.tool or {}).get("name")
        print(f"\n[tool_use: {name}]", flush=True)
    elif msg.kind == "tool_result":
        print(f"\n[tool_result id={(msg.tool or {}).get('id')}]", flush=True)
    elif msg.kind == "usage" and msg.usage:
        print(
            f"\n\n[usage] in={msg.usage.input_tokens} "
            f"out={msg.usage.output_tokens} "
            f"cached={msg.usage.cached_tokens}"
        )
    elif msg.kind == "session":
        print(f"\n[session_id={msg.session_id}]")
