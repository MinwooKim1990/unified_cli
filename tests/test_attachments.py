"""Unit tests for image / attachment handling (Phase 2).

Covers normalization (path/bytes/url/Attachment), per-provider _build_args
output, Claude's headless rejection, and the OpenAI multi-content parser.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unified_cli import UnifiedError
from unified_cli.core import (
    Attachment, attachment_b64, attachment_bytes,
    normalize_image, normalize_images,
)


def _make_dummy_png(payload: bytes = b"fake-png-data"):
    fd, p = tempfile.mkstemp(suffix=".png")
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    return p


# ---- normalization ----

def test_normalize_path_string_detects_media_type():
    p = _make_dummy_png()
    try:
        a = normalize_image(p)
        assert a.path == p
        assert a.media_type == "image/png"
        assert a.is_path
    finally:
        os.unlink(p)


def test_normalize_path_object():
    p = _make_dummy_png()
    try:
        a = normalize_image(Path(p))
        assert a.is_path and a.path == p
    finally:
        os.unlink(p)


def test_normalize_bytes():
    a = normalize_image(b"\x89PNG payload")
    assert a.is_bytes
    assert a.bytes_ == b"\x89PNG payload"


def test_normalize_http_url():
    a = normalize_image("https://example.com/cat.jpg")
    assert a.is_url
    assert a.url == "https://example.com/cat.jpg"


def test_normalize_data_url():
    a = normalize_image("data:image/png;base64,AAAA")
    assert a.is_url


def test_normalize_attachment_passthrough_fills_media_type():
    p = _make_dummy_png()
    try:
        att = Attachment(path=p)  # media_type unset
        a = normalize_image(att)
        assert a.media_type == "image/png"
    finally:
        os.unlink(p)


def test_normalize_unsupported_type_raises():
    try:
        normalize_image({"weird": "dict"})  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        assert False, "should have raised TypeError"


def test_normalize_images_handles_none_and_empty():
    assert normalize_images(None) == []
    assert normalize_images([]) == []


def test_normalize_images_mixed_inputs():
    p = _make_dummy_png()
    try:
        result = normalize_images([p, b"bytes", "https://x/y.png"])
        assert len(result) == 3
        assert result[0].is_path
        assert result[1].is_bytes
        assert result[2].is_url
    finally:
        os.unlink(p)


# ---- attachment payload helpers ----

def test_attachment_bytes_from_path():
    p = _make_dummy_png(b"hello-bytes")
    try:
        att = Attachment(path=p, media_type="image/png")
        assert attachment_bytes(att) == b"hello-bytes"
    finally:
        os.unlink(p)


def test_attachment_bytes_from_bytes():
    att = Attachment(bytes_=b"raw-data")
    assert attachment_bytes(att) == b"raw-data"


def test_attachment_b64_path():
    p = _make_dummy_png(b"encode-me")
    try:
        att = Attachment(path=p, media_type="image/png")
        assert base64.b64decode(attachment_b64(att)) == b"encode-me"
    finally:
        os.unlink(p)


def test_attachment_url_bytes_raises():
    att = Attachment(url="https://example.com/x.png")
    try:
        attachment_bytes(att)
    except ValueError:
        pass
    else:
        assert False


# ---- provider _build_args integration (no live calls) ----
# These construct providers only to exercise _build_args. We pass an explicit
# `bin_path` stub so the constructor skips binary discovery — otherwise the
# tests would require claude/codex/agy to be installed (they fail in CI, which
# has none of the CLIs). _build_args never executes the binary; argv[0] is the
# stub and no assertion below depends on it.

def test_codex_build_args_with_image_uses_stdin():
    from unified_cli import create
    p = _make_dummy_png()
    try:
        cli = create("codex", web_search=False, bin_path="codex")
        argv, stdin = cli._build_args(
            "describe", session_id=None, resume_last=False,
            model="gpt-5.4-mini", streaming=False, images=[p],
        )
        assert "-i" in argv
        assert p in argv
        # Codex 0.129 reads prompt from stdin when -i is used:
        assert stdin == "describe"
        assert "describe" not in argv
    finally:
        os.unlink(p)


def test_codex_build_args_no_image_uses_argv():
    from unified_cli import create
    cli = create("codex", web_search=False, bin_path="codex")
    argv, stdin = cli._build_args(
        "hi", session_id=None, resume_last=False,
        model="gpt-5.4-mini", streaming=False, images=None,
    )
    assert stdin is None
    assert argv[-1] == "hi"


def test_claude_build_args_with_image_uses_read_tool():
    """Claude routes image input through the Read tool: --allowedTools Read +
    bypassPermissions + path prepended to the prompt."""
    from unified_cli import create
    p = _make_dummy_png()
    try:
        cli = create("claude", web_search=False, bin_path="claude")
        argv, stdin = cli._build_args(
            "describe", session_id=None, resume_last=False,
            model="haiku", streaming=False, images=[p],
        )
        joined = " ".join(argv)
        assert "--allowedTools" in joined
        # Read tool must appear in the allowed list
        idx = argv.index("--allowedTools")
        assert "Read" in argv[idx + 1]
        assert "--permission-mode" in joined
        assert "bypassPermissions" in joined
        # Image absolute path must be in the (last) prompt argument
        assert p in argv[-1]
        assert stdin is None
    finally:
        os.unlink(p)


def test_gemini_build_args_with_image_injects_at_path():
    from unified_cli import create
    p = _make_dummy_png()
    try:
        cli = create("gemini", web_search=False, bin_path="agy")
        argv, stdin = cli._build_args(
            "describe", session_id=None, resume_last=False,
            model="gemini-3.5-flash", streaming=False, images=[p],
        )
        # The prompt now starts with @<path>
        assert argv[-1].startswith("@")
        assert p in argv[-1]
        assert "describe" in argv[-1]
        assert stdin is None
    finally:
        os.unlink(p)


# ---- server multi-content parser ----

def test_server_multicontent_parses_text_only():
    from unified_cli.server import ChatMessage, _extract_user_message
    msgs = [ChatMessage(role="user", content="hello")]
    text, images = _extract_user_message(msgs)
    assert text == "hello"
    assert images == []


def test_server_multicontent_parses_text_blocks():
    from unified_cli.server import ChatMessage, _extract_user_message
    msgs = [ChatMessage(role="user", content=[
        {"type": "text", "text": "describe"},
        {"type": "text", "text": "this image"},
    ])]
    text, images = _extract_user_message(msgs)
    assert "describe" in text and "this image" in text
    assert images == []


def test_server_multicontent_parses_data_url_image():
    from unified_cli.server import ChatMessage, _extract_user_message
    fake_b64 = base64.b64encode(b"png-bytes").decode()
    msgs = [ChatMessage(role="user", content=[
        {"type": "text", "text": "what is this?"},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{fake_b64}"}},
    ])]
    text, images = _extract_user_message(msgs)
    assert text == "what is this?"
    assert len(images) == 1
    att = images[0]
    assert isinstance(att, Attachment)
    assert att.bytes_ == b"png-bytes"
    assert att.media_type == "image/png"


def test_server_multicontent_parses_http_url():
    from unified_cli.server import ChatMessage, _extract_user_message
    msgs = [ChatMessage(role="user", content=[
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ])]
    text, images = _extract_user_message(msgs)
    assert text == "describe"
    assert len(images) == 1
    assert images[0] == "https://example.com/x.png"


if __name__ == "__main__":
    import traceback
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                print(f"  ✗ {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
