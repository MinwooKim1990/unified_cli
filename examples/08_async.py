"""Example 8 — async API (achat / astream).

asyncio 기반 애플리케이션에서 쓸 때.
"""
import asyncio
from unified_cli import create


async def main():
    cli = create("codex", web_search=False)

    # 1) 비동기 chat
    resp = await cli.achat("async 로 안녕")
    print("[achat]", resp.text.strip()[:60])

    # 2) 비동기 스트리밍
    print("[astream] ", end="", flush=True)
    async for msg in cli.astream(
        "이어서 한 문장만 더",
        session_id=resp.session_id,
    ):
        if msg.kind == "text" and msg.text:
            print(msg.text, end="", flush=True)
    print()

    # 3) 병렬 호출 (서로 다른 provider)
    claude_cli = create("claude", web_search=False)
    gemini_cli = create("gemini", web_search=False)
    results = await asyncio.gather(
        claude_cli.achat("reply 'A'"),
        gemini_cli.achat("reply 'B'"),
    )
    for r in results:
        print(f"[parallel] {r.provider}: {r.text.strip()[:30]}")


asyncio.run(main())
