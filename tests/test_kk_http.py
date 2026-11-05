"""Tests for the Kaperkunde HTTP entry point (pdf_mcp/kk_http.py)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pdf_mcp import kk_http
from pdf_mcp.kk_http import BearerAuth, Refused, check_arguments, confine, guarded


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return Path(os.path.realpath(data))


def test_confine_accepts_paths_under_root(root: Path) -> None:
    assert confine(str(root / "a.pdf"), root, "pdf_path") == str(root / "a.pdf")
    assert confine("sub/b.pdf", root, "pdf_path") == str(root / "sub" / "b.pdf")
    assert confine(str(root), root, "output_dir") == str(root)


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "../x.pdf", "/data/../etc/passwd", "~/x.pdf", "", "a\x00b"],
)
def test_confine_refuses_paths_outside_root(root: Path, value: str) -> None:
    with pytest.raises(Refused):
        confine(value.replace("/data", str(root)), root, "pdf_path")


def test_confine_refuses_a_sibling_with_the_same_prefix(root: Path) -> None:
    sibling = root.parent / (root.name + "-other")
    sibling.mkdir()
    with pytest.raises(Refused):
        confine(str(sibling / "x.pdf"), root, "pdf_path")


def test_confine_follows_symlinks_out(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(Refused):
        confine(str(root / "link" / "x.pdf"), root, "output_path")


def test_check_arguments_covers_every_path_shape(root: Path) -> None:
    for name in ("pdf_path", "output_dir", "pfx_password_file", "pdf_list", "chain_paths", "insert_from_path"):
        value = ["/etc/x"] if name in ("pdf_list", "chain_paths") else "/etc/x"
        with pytest.raises(Refused):
            check_arguments({name: value}, root, False)
    checked = check_arguments({"pdf_list": ["a.pdf", str(root / "b.pdf")]}, root, False)
    assert checked["pdf_list"] == [str(root / "a.pdf"), str(root / "b.pdf")]
    # merge_pdfs takes Iterable[str]: pydantic hands over a one-shot iterator.
    checked = check_arguments({"pdf_list": iter(["a.pdf"])}, root, False)
    assert checked["pdf_list"] == [str(root / "a.pdf")]
    with pytest.raises(Refused):
        check_arguments({"pdf_list": iter(["a.pdf", "/etc/x"])}, root, False)
    with pytest.raises(Refused):
        check_arguments({"pdf_list": [b"/etc/x"]}, root, False)


def test_check_arguments_refuses_env_passwords(root: Path) -> None:
    with pytest.raises(Refused):
        check_arguments({"pfx_password_env": "MCP_API_KEY"}, root, True)
    with pytest.raises(Refused):
        check_arguments({"key_password_env": "HOME"}, root, True)


def test_check_arguments_refuses_signing_network_unless_allowed(root: Path) -> None:
    with pytest.raises(Refused):
        check_arguments({"timestamp_url": "http://169.254.169.254/"}, root, False)
    with pytest.raises(Refused):
        check_arguments({"allow_fetching": True}, root, False)
    assert check_arguments({"allow_fetching": False}, root, False) == {"allow_fetching": False}
    assert check_arguments({"timestamp_url": "http://tsa.example/"}, root, True)


@pytest.mark.parametrize("value", ["eng", "eng+nld", "chi_sim"])
def test_language_codes_pass(root: Path, value: str) -> None:
    assert check_arguments({"language": value}, root, False) == {"language": value}


@pytest.mark.parametrize("value", ["../../tmp/evil", "/tmp/x", "eng --psm 6", "e"])
def test_language_paths_are_refused(root: Path, value: str) -> None:
    with pytest.raises(Refused):
        check_arguments({"ocr_language": value}, root, False)


def test_guarded_checks_var_keyword_arguments(root: Path) -> None:
    seen = {}

    def tool(pdf_paths, operation, output_dir=None, **kwargs):
        seen.update(kwargs)
        return "ok"

    wrapped = guarded(tool, root, False)
    assert wrapped(pdf_paths=["a.pdf"], operation="x") == "ok"
    assert "error" in wrapped(pdf_paths=["a.pdf"], operation="x", output_path="/etc/x")
    assert "error" in wrapped(pdf_paths=["/etc/x"], operation="x")


def test_guarded_answers_errors_without_traceback(root: Path) -> None:
    def tool(pdf_path):
        raise RuntimeError("boom")

    answer = guarded(tool, root, False)(pdf_path="a.pdf")
    assert answer == {"error": "Unexpected error (RuntimeError)"}


def test_ai_tools_are_off_by_default() -> None:
    names = {tool.name for tool in kk_http.exposed_tools(False)}
    assert "auto_fill_pdf_form" not in names
    assert "get_llm_backend_info" not in names
    assert "sign_pdf" in names
    with_ai = {tool.name for tool in kk_http.exposed_tools(True)}
    assert "auto_fill_pdf_form" in with_ai


def test_every_exposed_path_argument_is_recognised() -> None:
    """A new upstream argument that names a file must match is_path_arg."""
    import inspect

    suspicious = []
    for tool in kk_http.exposed_tools(True):
        for name in inspect.signature(tool.callable.resolve()).parameters:
            looks_like_path = any(part in name for part in ("path", "dir", "file", "pdf_list"))
            if looks_like_path and not kk_http.is_path_arg(name):
                suspicious.append(f"{tool.name}.{name}")
    assert suspicious == []


def _call(app, path: str, headers: list[tuple[bytes, bytes]]):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "headers": headers, "method": "POST"}
    asyncio.run(app(scope, receive, send))
    return sent


def test_bearer_auth() -> None:
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    guard = BearerAuth(app, "s3cret")
    assert _call(guard, "/mcp", [])[0]["status"] == 401
    assert _call(guard, "/mcp", [(b"authorization", b"Bearer wrong")])[0]["status"] == 401
    _call(guard, "/mcp", [(b"authorization", b"Bearer s3cret")])
    _call(guard, "/health", [])
    assert reached == ["/mcp", "/health"]
