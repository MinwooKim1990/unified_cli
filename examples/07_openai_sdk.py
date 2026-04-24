"""Example 7 — OpenAI Python SDK 로 통합 서버 호출하기.

미리 서버 띄워야 함:
    source .venv/bin/activate
    uvicorn unified_cli.server:app --port 8000

그 다음 이 스크립트 실행 (`pip install openai` 별도 필요).
"""
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")

# 1) 자동 라우팅: "haiku" → claude
r = client.chat.completions.create(
    model="haiku",
    messages=[{"role": "user", "content": "say: hello from claude"}],
    user="demo-conv",
)
print("[1]", r.choices[0].message.content)

# 2) 같은 user 로 다른 provider 호출 → 이전 컨텍스트 자동 주입
r = client.chat.completions.create(
    model="gpt-5.4-mini",
    messages=[{"role": "user", "content": "지난 메시지에서 뭐라고 시켰지? 한 문장."}],
    user="demo-conv",
)
print("[2]", r.choices[0].message.content)

# 3) 스트리밍
print("[3] ", end="", flush=True)
for chunk in client.chat.completions.create(
    model="gemini/gemini-3.1-flash-lite-preview",  # 명시 prefix
    messages=[{"role": "user", "content": "1부터 5까지 세."}],
    user="demo-conv-2",
    stream=True,
):
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
print()
