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
    # As MCP sends it: the **kwargs object nested under its own name.
    seen.clear()
    assert wrapped(pdf_paths=["a.pdf"], operation="x", kwargs={"quality": "low"}) == "ok"
    assert seen == {"quality": "low"}
    assert "error" in wrapped(pdf_paths=["a.pdf"], operation="x", kwargs={"output_path": "/etc/x"})


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
    # The file tools take the same argument names.
    for name, params in (
        ("put_file", inspect.signature(kk_http.put_file_in).parameters),
        ("get_file", inspect.signature(kk_http.get_file_in).parameters),
        ("list_files", inspect.signature(kk_http.list_files_in).parameters),
        ("delete_file", inspect.signature(kk_http.delete_file_in).parameters),
    ):
        for arg in params:
            if arg in ("root", "limit"):
                continue
            looks_like_path = any(part in arg for part in ("path", "dir", "file", "pdf_list"))
            if looks_like_path and not kk_http.is_path_arg(arg):
                suspicious.append(f"{name}.{arg}")
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


def test_allowed_hosts_add_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    settings = kk_http.transport_security()
    assert settings.enable_dns_rebinding_protection
    assert "127.0.0.1:*" in settings.allowed_hosts
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "192.0.2.10:8000")
    settings = kk_http.transport_security()
    assert "127.0.0.1:*" in settings.allowed_hosts and "192.0.2.10:8000" in settings.allowed_hosts
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
    assert not kk_http.transport_security().enable_dns_rebinding_protection


# ---------------------------------------------------------------------------
# put_file, get_file, list_files, delete_file
# ---------------------------------------------------------------------------

import base64  # noqa: E402
import hashlib  # noqa: E402

from pdf_mcp.kk_http import (  # noqa: E402
    confine_entry,
    delete_file_in,
    get_file_in,
    list_files_in,
    put_file_in,
    sniff_mime,
)

LIMIT = 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_put_then_get_returns_the_same_bytes(root: Path) -> None:
    data = bytes(range(256)) * 3
    answer = put_file_in(root, LIMIT, "in/sub/x.bin", _b64(data))
    assert answer == {
        "path": str(root / "in" / "sub" / "x.bin"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "replaced": False,
    }
    resource = get_file_in(root, LIMIT, str(root / "in" / "sub" / "x.bin"))
    assert resource.type == "resource"
    assert base64.b64decode(resource.resource.blob) == data
    assert resource.resource.mime_type == "application/octet-stream"
    assert resource.resource.uri == (root / "in" / "sub" / "x.bin").as_uri()
    assert resource.resource.uri.rsplit("/", 1)[-1] == "x.bin"
    # No .part file is left behind.
    assert sorted(p.name for p in (root / "in" / "sub").iterdir()) == ["x.bin"]


def test_put_refuses_an_existing_file_unless_overwrite(root: Path) -> None:
    put_file_in(root, LIMIT, "a.txt", _b64(b"one"))
    with pytest.raises(Refused, match="overwrite"):
        put_file_in(root, LIMIT, "a.txt", _b64(b"two"))
    assert (root / "a.txt").read_bytes() == b"one"
    answer = put_file_in(root, LIMIT, "a.txt", _b64(b"two"), overwrite=True)
    assert answer["replaced"] is True
    assert (root / "a.txt").read_bytes() == b"two"
    (root / "folder").mkdir()
    with pytest.raises(Refused, match="folder"):
        put_file_in(root, LIMIT, "folder", _b64(b"x"), overwrite=True)


def test_put_refuses_bad_base64(root: Path) -> None:
    for bad in ("not base64!", "aGVsbG8", "aGVs\nbG8=", "aGVsbG8=é"):
        with pytest.raises(Refused, match="base64"):
            put_file_in(root, LIMIT, "a.txt", bad)
    assert not (root / "a.txt").exists()


def test_put_and_get_respect_the_size_cap(root: Path) -> None:
    with pytest.raises(Refused, match="larger"):
        put_file_in(root, LIMIT, "big.bin", _b64(b"x" * (LIMIT + 1)))
    # Refused before decoding: an oversized string that is not even base64.
    with pytest.raises(Refused, match="larger"):
        put_file_in(root, LIMIT, "big.bin", "!" * (LIMIT * 2))
    assert put_file_in(root, LIMIT, "edge.bin", _b64(b"x" * LIMIT))["bytes"] == LIMIT
    (root / "big.bin").write_bytes(b"x" * (LIMIT + 1))
    with pytest.raises(Refused, match="larger"):
        get_file_in(root, LIMIT, "big.bin")


def test_max_file_bytes_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("PDF_MCP_MAX_FILE_BYTES", raising=False)
    assert kk_http.max_file_bytes() == 128 * 1024 * 1024
    monkeypatch.setenv("PDF_MCP_MAX_FILE_BYTES", "2048")
    assert kk_http.max_file_bytes() == 2048
    for bad in ("0", "-1", "1e9", "lots"):
        monkeypatch.setenv("PDF_MCP_MAX_FILE_BYTES", bad)
        with pytest.raises(ValueError):
            kk_http.max_file_bytes()


@pytest.mark.parametrize(
    "value",
    ["../x.pdf", "sub/../../x.pdf", "/data/../etc/passwd", "/etc/passwd", "~/x.pdf", "", "a\x00b"],
)
def test_file_tools_refuse_paths_outside_root(root: Path, value: str) -> None:
    value = value.replace("/data", str(root))
    (root / "sub").mkdir(exist_ok=True)
    with pytest.raises(Refused):
        put_file_in(root, LIMIT, value, _b64(b"x"))
    with pytest.raises(Refused):
        get_file_in(root, LIMIT, value)
    with pytest.raises(Refused):
        delete_file_in(root, value)
    with pytest.raises(Refused):
        list_files_in(root, value)


def test_file_tools_refuse_a_link_out(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (root / "out").symlink_to(outside)
    (root / "file-out").symlink_to(outside / "secret.txt")
    with pytest.raises(Refused):
        put_file_in(root, LIMIT, "out/x.txt", _b64(b"x"))
    with pytest.raises(Refused):
        get_file_in(root, LIMIT, "out/secret.txt")
    with pytest.raises(Refused):
        get_file_in(root, LIMIT, "file-out")
    with pytest.raises(Refused):
        list_files_in(root, "out")
    with pytest.raises(Refused):
        delete_file_in(root, "out/secret.txt")
    # Overwriting the link replaces the link, never the file it points to.
    put_file_in(root, LIMIT, "file-out", _b64(b"mine"), overwrite=True)
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert not (root / "file-out").is_symlink()
    # Deleting a link removes the link only.
    (root / "file-out").unlink()
    (root / "file-out").symlink_to(outside / "secret.txt")
    assert delete_file_in(root, "file-out")["deleted"] == "link"
    assert (outside / "secret.txt").exists()
    assert list_files_in(root)["entries"] == []  # the link to a folder outside is not shown


def test_delete_never_removes_root(root: Path) -> None:
    for value in (str(root), str(root) + "/", "."):
        with pytest.raises(Refused, match="data folder"):
            delete_file_in(root, value)
    assert root.is_dir()


def test_delete_removes_files_and_empty_folders_only(root: Path) -> None:
    put_file_in(root, LIMIT, "f/a.txt", _b64(b"a"))
    with pytest.raises(Refused, match="not empty"):
        delete_file_in(root, "f")
    assert delete_file_in(root, "f/a.txt") == {"path": str(root / "f" / "a.txt"), "deleted": "file"}
    assert delete_file_in(root, "f") == {"path": str(root / "f"), "deleted": "folder"}
    with pytest.raises(Refused, match="no such"):
        delete_file_in(root, "f")


def test_list_puts_folders_first(root: Path) -> None:
    put_file_in(root, LIMIT, "b.pdf", _b64(b"%PDF-1.7"))
    put_file_in(root, LIMIT, "A.txt", _b64(b"hello"))
    (root / "zeta").mkdir()
    (root / "Alpha").mkdir()
    (root / "inside").symlink_to(root / "b.pdf")
    answer = list_files_in(root, str(root))
    assert answer["path"] == str(root)
    assert answer["truncated"] is False
    assert [(e["name"], e["type"]) for e in answer["entries"]] == [
        ("Alpha", "folder"),
        ("zeta", "folder"),
        ("A.txt", "file"),
        ("b.pdf", "file"),
        ("inside", "file"),
    ]
    a_txt = answer["entries"][2]
    assert a_txt["bytes"] == 5 and a_txt["path"] == str(root / "A.txt")
    assert a_txt["modified"].endswith("+00:00")
    assert answer["entries"][0]["bytes"] is None
    with pytest.raises(Refused, match="no such folder"):
        list_files_in(root, "b.pdf")


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"%PDF-1.7\n...", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xe0....", "image/jpeg"),
        (b"II*\x00....", "image/tiff"),
        (b"MM\x00*....", "image/tiff"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        ("Grüße\nline two\t.".encode(), "text/plain"),
        (b"\x00\x01\x02binary", "application/octet-stream"),
        (b"\xff\xfe\xfd", "application/octet-stream"),
        (b"", "application/octet-stream"),
    ],
)
def test_sniff_mime(data: bytes, mime: str) -> None:
    assert sniff_mime(data) == mime


def test_confine_entry_keeps_the_last_name(root: Path) -> None:
    (root / "link").symlink_to(root / "target")
    assert confine_entry("link", root, "file_path") == root / "link"
    assert confine_entry(str(root), root, "file_path") == root
    assert confine_entry("./", root, "file_path") == root


def test_file_tools_through_the_server(root: Path, monkeypatch) -> None:
    from mcp_types import EmbeddedResource

    monkeypatch.setenv("PDF_MCP_DATA_DIR", str(root))
    monkeypatch.setenv("PDF_MCP_MAX_FILE_BYTES", str(LIMIT))
    server, names = kk_http.build_server()
    assert set(kk_http.FILE_TOOL_NAMES) <= set(names)
    listed = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in kk_http.FILE_TOOL_NAMES:
        schema = listed[name].input_schema
        assert schema["additionalProperties"] is False
        assert all(prop.get("description") for prop in schema["properties"].values())
    assert listed["get_file"].annotations.read_only_hint is True
    assert listed["list_files"].annotations.read_only_hint is True
    assert listed["put_file"].annotations.read_only_hint is False
    assert listed["put_file"].annotations.destructive_hint is False
    assert listed["delete_file"].annotations.destructive_hint is True

    data = b"%PDF-1.7 pretend"
    put = asyncio.run(server.call_tool("put_file", {"file_path": "x.pdf", "content_base64": _b64(data)}))
    assert put.structured_content["bytes"] == len(data)
    got = asyncio.run(server.call_tool("get_file", {"file_path": str(root / "x.pdf")}))
    (block,) = got.content
    assert isinstance(block, EmbeddedResource)
    assert block.resource.mime_type == "application/pdf"
    assert base64.b64decode(block.resource.blob) == data
    with pytest.raises(Exception, match="Extra inputs"):
        asyncio.run(server.call_tool("get_file", {"file_path": "x.pdf", "mode": "raw"}))
    with pytest.raises(Exception, match="overwrite"):
        asyncio.run(server.call_tool("put_file", {"file_path": "x.pdf", "content_base64": _b64(data)}))
    # Strict: no coercion of "yes" into true.
    with pytest.raises(Exception, match="valid boolean"):
        asyncio.run(server.call_tool("put_file", {"file_path": "x.pdf", "content_base64": "", "overwrite": "yes"}))
