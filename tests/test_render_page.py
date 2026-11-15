"""Tests for render_page (a PDF page to a PNG), plain and through the HTTP entry point."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import kk_http, pdf_tools
from pdf_mcp.pdf_tools import PdfToolError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return Path(os.path.realpath(data))


def _pdf(path: Path, pages: int = 2, size: tuple[float, float] = (200, 100)) -> Path:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((10, 50), f"page {number + 1}")
    doc.save(str(path))
    doc.close()
    return path


def test_renders_a_page_to_a_png_at_the_dpi(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "a.pdf")
    result = pdf_tools.render_page(str(src), str(tmp_path / "out" / "p.png"), page_number=2, dpi=144)
    out = Path(result["output_path"])
    assert out.read_bytes().startswith(PNG_MAGIC)
    assert (result["width"], result["height"]) == (400, 200)
    assert result["dpi"] == 144.0
    assert result["page_number"] == 2
    assert result["page_count"] == 2
    assert result["bytes"] == out.stat().st_size


def test_defaults_to_the_first_page_at_150_dpi(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "a.pdf")
    result = pdf_tools.render_page(str(src), str(tmp_path / "p.png"))
    assert result["page_number"] == 1
    assert result["dpi"] == 150.0
    assert result["width"] == round(200 * 150 / 72)


def test_pages_differ(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "a.pdf")
    one = pdf_tools.render_page(str(src), str(tmp_path / "1.png"), page_number=1)
    two = pdf_tools.render_page(str(src), str(tmp_path / "2.png"), page_number=2)
    assert Path(one["output_path"]).read_bytes() != Path(two["output_path"]).read_bytes()


def test_max_size_shrinks_the_picture_below_what_dpi_asks(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "a.pdf")
    result = pdf_tools.render_page(str(src), str(tmp_path / "p.png"), dpi=300, max_size=300)
    assert max(result["width"], result["height"]) <= 300
    assert result["width"] == 300
    assert result["dpi"] == pytest.approx(108.0, abs=0.1)


def test_a_huge_page_is_held_to_the_default_longest_side(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "big.pdf", pages=1, size=(14400, 14400))
    result = pdf_tools.render_page(str(src), str(tmp_path / "p.png"), dpi=300)
    assert max(result["width"], result["height"]) <= pdf_tools.RENDER_DEFAULT_MAX_SIZE


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"page_number": 0}, "page_number"),
        ({"page_number": 3}, "out of range"),
        ({"page_number": True}, "page_number"),
        ({"page_number": 1.5}, "page_number"),
        ({"dpi": 35}, "dpi"),
        ({"dpi": 301}, "dpi"),
        ({"dpi": "150"}, "dpi"),
        ({"max_size": 63}, "max_size"),
        ({"max_size": 8193}, "max_size"),
    ],
)
def test_refuses_what_is_out_of_bounds(tmp_path: Path, kwargs: dict, message: str) -> None:
    src = _pdf(tmp_path / "a.pdf")
    with pytest.raises(PdfToolError, match=message):
        pdf_tools.render_page(str(src), str(tmp_path / "p.png"), **kwargs)
    assert not (tmp_path / "p.png").exists()


def test_refuses_an_output_that_is_not_a_png_and_a_missing_file(tmp_path: Path) -> None:
    src = _pdf(tmp_path / "a.pdf")
    with pytest.raises(PdfToolError, match=r"\.png"):
        pdf_tools.render_page(str(src), str(tmp_path / "p.jpg"))
    with pytest.raises(PdfToolError, match="not found"):
        pdf_tools.render_page(str(tmp_path / "missing.pdf"), str(tmp_path / "p.png"))


def test_registered_after_the_upstream_tools() -> None:
    from pdf_mcp.registry import iter_all

    names = [tool.name for tool in iter_all()]
    assert names[-1] == "render_page"
    assert next(t for t in iter_all() if t.name == "render_page").verb == "extract"


def test_stated_as_a_write(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PDF_MCP_DATA_DIR", str(root))
    server, names = kk_http.build_server()
    assert "render_page" in names
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "render_page")
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is False
    assert tool.description.startswith("Render a PDF page")


def test_put_file_render_page_get_file(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PDF_MCP_DATA_DIR", str(root))
    server, _ = kk_http.build_server()
    data = _pdf(root.parent / "src.pdf").read_bytes()

    def call(name: str, arguments: dict):
        return asyncio.run(server.call_tool(name, arguments))

    call("put_file", {"file_path": "in/doc.pdf", "content_base64": base64.b64encode(data).decode()})
    answer = call(
        "render_page", {"pdf_path": "in/doc.pdf", "output_path": "out/page.png", "page_number": 2}
    ).structured_content["result"]
    assert answer["output_path"] == str(root / "out" / "page.png")
    got = call("get_file", {"file_path": answer["output_path"]})
    resource = got.content[0].resource
    assert resource.mime_type == "image/png"
    assert base64.b64decode(resource.blob).startswith(PNG_MAGIC)
    assert (root / "in" / "doc.pdf").read_bytes() == data


def test_through_the_server_it_stays_in_data_and_never_writes_over(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PDF_MCP_DATA_DIR", str(root))
    server, _ = kk_http.build_server()
    _pdf(root / "a.pdf")

    def render(arguments: dict) -> dict:
        return asyncio.run(server.call_tool("render_page", arguments)).structured_content["result"]

    assert "error" in render({"pdf_path": "a.pdf", "output_path": "/etc/page.png"})
    assert "error" in render({"pdf_path": "../a.pdf", "output_path": "p.png"})
    first = render({"pdf_path": "a.pdf", "output_path": "p.png"})
    assert first["output_path"] == str(root / "p.png")
    again = render({"pdf_path": "a.pdf", "output_path": "p.png"})
    assert "already exists" in again["error"]
    assert "is the input" in render({"pdf_path": "a.pdf", "output_path": "a.pdf"})["error"]
    assert "error" in render({"pdf_path": "a.pdf", "output_path": "q.png", "dpi": 9999})
