"""pdf-mcp over Streamable HTTP, confined to one data directory (Kaperkunde fork).

Run with ``python -m pdf_mcp.kk_http``. This is the entry point of the
container image; the upstream stdio server (``pdf-mcp serve``) is untouched.

What it adds on top of the upstream tools, all enforced here before a tool
runs:

* **Paths stay under the data directory** (``PDF_MCP_DATA_DIR``, default
  ``/data``). Every argument that names a file or directory (``*_path``,
  ``*_paths``, ``*_dir``, ``*_file``, ``pdf_list``) is resolved with
  :func:`os.path.realpath` (symlinks followed) and refused unless it lands
  inside the data directory. A relative path is taken relative to it. The
  tool is handed the resolved path.
* **No network from the tools by default.** ``sign_pdf`` and
  ``sign_pdf_pem`` refuse ``timestamp_url`` and ``allow_fetching=true``
  (an RFC 3161 server, OCSP/CRL fetches) unless
  ``PDF_MCP_ALLOW_SIGNING_NETWORK=1``. The ``ai`` tools (LLM backends) are
  not registered unless ``PDF_MCP_ENABLE_AI_TOOLS=1``.
* **No environment variables as passwords.** ``pfx_password_env`` and
  ``key_password_env`` are refused: they would let a caller probe, and use,
  any variable of the server's environment (``MCP_API_KEY`` included). Use
  ``*_password_file`` (a file under the data directory) instead.
* **OCR language codes only**: ``language`` / ``ocr_language`` must look
  like ``eng`` or ``eng+nld``, never a path for Tesseract to load.
* **Optional bearer token**: when ``MCP_API_KEY`` is set, every request but
  ``GET /health`` must send ``Authorization: Bearer <MCP_API_KEY>``.
* **Host header check** (DNS rebinding): loopback, which is how a tunnel or
  ``-p 127.0.0.1:…`` reaches it, plus ``MCP_ALLOWED_HOSTS`` (comma
  separated, ``host:*`` for any port; ``*`` turns the check off).
* Errors are answered without tracebacks.
* **Files in and out**: ``put_file``, ``get_file``, ``list_files`` and
  ``delete_file`` move files into and out of the data directory over MCP
  (base64 in, an embedded resource out), within ``PDF_MCP_MAX_FILE_BYTES``
  (default 128 MiB). They refuse ``..``, resolve the folder a name is in
  with :func:`os.path.realpath` and act on the name itself, never on the
  data directory.

Settings (environment): ``MCP_HOST`` (default ``0.0.0.0``), ``MCP_PORT``
(default ``8000``), ``MCP_PATH`` (default ``/mcp``), ``MCP_STATELESS``
(``1`` for stateless Streamable HTTP), plus the ones above.
"""

from __future__ import annotations

import base64
import binascii
import errno
import functools
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import stat
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Iterable

logger = logging.getLogger("pdf_mcp.kk_http")

TRUE_VALUES = {"1", "true", "yes", "on"}

# Arguments that name a file or a directory on the server.
_PATH_ARG = re.compile(r"(?:_path|_paths|_dir|_file)$")
_PATH_ARGS_EXTRA = {"pdf_list"}
# Arguments that name an environment variable to read a secret from.
_ENV_SECRET_ARGS = {"pfx_password_env", "key_password_env"}
# Arguments that make the signing tools reach the network.
_NETWORK_SIGNING_ARGS = {"timestamp_url", "allow_fetching"}
# Tesseract language codes: "eng", "eng+nld", "chi_sim".
_LANGUAGE_ARGS = {"language", "ocr_language"}
_LANGUAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,31}(?:\+[A-Za-z][A-Za-z0-9_]{1,31}){0,7}$")

DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"]

# The largest file put_file takes and get_file hands out.
DEFAULT_MAX_FILE_BYTES = 128 * 1024 * 1024
# The most entries list_files answers with.
MAX_LIST_ENTRIES = 5000


class Refused(Exception):
    """An argument this server does not accept; answered as a tool error."""


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


def data_root() -> Path:
    root = os.path.realpath(os.environ.get("PDF_MCP_DATA_DIR", "/data"))
    return Path(root)


def is_path_arg(name: str) -> bool:
    return name in _PATH_ARGS_EXTRA or bool(_PATH_ARG.search(name))


def confine(value: Any, root: Path, name: str) -> str:
    """Return ``value`` resolved inside ``root``, or raise :class:`Refused`."""
    if not isinstance(value, str) or not value:
        raise Refused(f"{name} must be a path")
    if "\x00" in value:
        raise Refused(f"{name} must not contain NUL")
    if value.startswith("~"):
        raise Refused(f"{name}: '~' is not expanded here; use a path under {root}")
    candidate = value if os.path.isabs(value) else os.path.join(root, value)
    resolved = os.path.realpath(candidate)
    if os.path.commonpath([resolved, str(root)]) != str(root):
        raise Refused(f"{name} must be under {root}: {value}")
    return resolved


def check_arguments(arguments: dict[str, Any], root: Path, allow_signing_network: bool) -> dict[str, Any]:
    """Check one call's arguments and return them with paths resolved."""
    checked = dict(arguments)
    for name, value in arguments.items():
        if value is None:
            continue
        if is_path_arg(name):
            # A list arrives as a list, or, for an Iterable[str] parameter
            # (merge_pdfs), as pydantic's one-shot iterator: read it once here.
            if not isinstance(value, (str, bytes, dict)) and hasattr(value, "__iter__"):
                checked[name] = [confine(item, root, name) for item in value]
            else:
                checked[name] = confine(value, root, name)
        elif name in _ENV_SECRET_ARGS:
            raise Refused(f"{name} is disabled on this server; use the matching *_file argument")
        elif name in _NETWORK_SIGNING_ARGS and not allow_signing_network:
            if name == "allow_fetching" and value is False:
                continue
            raise Refused(
                f"{name} is disabled on this server (no network); set PDF_MCP_ALLOW_SIGNING_NETWORK=1 to allow"
            )
        elif name in _LANGUAGE_ARGS:
            if not isinstance(value, str) or not _LANGUAGE.match(value):
                raise Refused(f"{name} must be Tesseract language codes like 'eng' or 'eng+nld'")
    return checked


def guarded(fn: Callable[..., Any], root: Path, allow_signing_network: bool) -> Callable[..., Any]:
    """Wrap a tool: check its arguments, run it, answer errors without tracebacks."""
    from .pdf_tools import PdfToolError

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = signature.bind_partial(*args, **kwargs)
            plain = {}
            for name, value in bound.arguments.items():
                kind = signature.parameters[name].kind
                if kind is inspect.Parameter.VAR_KEYWORD:
                    # MCP publishes **kwargs as a required object argument named
                    # after it (batch_process: "kwargs"), so it arrives nested.
                    if set(value) == {name} and isinstance(value[name], dict):
                        value = value[name]
                    # batch_process passes **kwargs on to another tool: same checks.
                    bound.arguments[name] = check_arguments(value, root, allow_signing_network)
                elif kind is inspect.Parameter.VAR_POSITIONAL:
                    raise Refused("positional arguments are not accepted")
                else:
                    plain[name] = value
            bound.arguments.update(check_arguments(plain, root, allow_signing_network))
            result = fn(*bound.args, **bound.kwargs)
        except Refused as exc:
            return {"error": str(exc)}
        except PdfToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Tool %s failed", fn.__name__)
            return {"error": f"Unexpected error ({type(exc).__name__})"}
        return str(result) if isinstance(result, Path) else result

    return wrapper


def exposed_tools(enable_ai: bool) -> Iterable[Any]:
    from .registry import iter_all

    for tool in iter_all():
        if tool.verb == "ai" and not enable_ai:
            continue
        yield tool


# ---------------------------------------------------------------------------
# Files in and out of the data directory
# ---------------------------------------------------------------------------


def max_file_bytes() -> int:
    """``PDF_MCP_MAX_FILE_BYTES``, a positive whole number of bytes (default 128 MiB)."""
    raw = os.environ.get("PDF_MCP_MAX_FILE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_FILE_BYTES
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("PDF_MCP_MAX_FILE_BYTES must be a positive number of bytes")
    return int(raw)


def confine_entry(value: Any, root: Path, name: str) -> Path:
    """Return the entry ``value`` names under ``root``, or raise :class:`Refused`.

    The folder it is in is resolved with :func:`os.path.realpath` and must be
    ``root`` or inside it; the last name is kept as it is, so a symbolic link
    is the entry itself, not what it points to. ``..`` is refused outright.
    Returns ``root`` itself when ``value`` names it.
    """
    if not isinstance(value, str) or not value:
        raise Refused(f"{name} must be a path")
    if "\x00" in value:
        raise Refused(f"{name} must not contain NUL")
    if value.startswith("~"):
        raise Refused(f"{name}: '~' is not expanded here; use a path under {root}")
    if ".." in value.split("/"):
        raise Refused(f"{name} must not contain '..'")
    candidate = value if os.path.isabs(value) else os.path.join(root, value)
    normalized = os.path.normpath(candidate)
    if normalized == str(root) or (os.path.realpath(normalized) == str(root) and not os.path.islink(normalized)):
        return root
    folder = confine(os.path.dirname(normalized), root, name)
    return Path(folder) / os.path.basename(normalized)


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)
_TEXT_CONTROLS = set(range(0x20)) - {0x09, 0x0A, 0x0C, 0x0D}


def sniff_mime(data: bytes) -> str:
    """The media type ``data`` holds, from its first bytes; never from a name."""
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        if not any(ord(ch) in _TEXT_CONTROLS for ch in text[:65536]):
            return "text/plain"
    return "application/octet-stream"


def _decode_base64(content: Any, limit: int) -> bytes:
    if not isinstance(content, str):
        raise Refused("content_base64 must be a string")
    # Refuse an oversized file before decoding it: 4 characters carry 3 bytes.
    if len(content) // 4 * 3 - content[-2:].count("=") > limit:
        raise Refused(f"the file is larger than {limit} bytes")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise Refused("content_base64 is not valid base64 (standard alphabet, padded, no white space)") from None
    if len(data) > limit:
        raise Refused(f"the file is larger than {limit} bytes")
    return data


def put_file_in(root: Path, limit: int, file_path: Any, content_base64: Any, overwrite: bool = False) -> dict[str, Any]:
    """Write a file under ``root``: a ``.part`` file made exclusively, then renamed into place."""
    data = _decode_base64(content_base64, limit)
    target = confine_entry(file_path, root, "file_path")
    if target == root:
        raise Refused("file_path must name a file, not the data folder")
    try:
        os.makedirs(target.parent, exist_ok=True)
    except OSError as exc:
        raise Refused(f"file_path: cannot make its folder ({exc.strerror})") from None
    replaced = False
    try:
        existing = os.lstat(target)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISDIR(existing.st_mode):
            raise Refused("file_path names a folder")
        if not overwrite:
            raise Refused("file_path exists; pass overwrite=true to replace it")
        replaced = True
    part = target.parent / f"{target.name}.{secrets.token_hex(8)}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(part, flags, 0o666)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, target)
        except BaseException:
            try:
                os.unlink(part)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise Refused(f"file_path: cannot write ({exc.strerror})") from None
    return {
        "path": str(target),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "replaced": replaced,
    }


def read_file_in(root: Path, limit: int, file_path: Any) -> tuple[Path, bytes]:
    """Read a file under ``root`` (a link is followed only to a file under ``root``)."""
    entry = confine_entry(file_path, root, "file_path")
    real = Path(confine(str(entry), root, "file_path"))
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise Refused(f"file_path: no such file: {file_path}") from None
    except OSError as exc:
        raise Refused(f"file_path: cannot read ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused("file_path does not name a file")
        if info.st_size > limit:
            raise Refused(f"the file is larger than {limit} bytes")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused(f"the file is larger than {limit} bytes")
    return real, data


def get_file_in(root: Path, limit: int, file_path: Any):
    """A file under ``root`` as an MCP embedded resource (base64 blob)."""
    from mcp_types import BlobResourceContents, EmbeddedResource

    real, data = read_file_in(root, limit, file_path)
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=real.as_uri(),
            mime_type=sniff_mime(data),
            blob=base64.b64encode(data).decode("ascii"),
        ),
    )


def _modified(info: os.stat_result) -> str:
    return datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")


def list_files_in(root: Path, dir_path: Any = None) -> dict[str, Any]:
    """The entries of a folder under ``root``: folders first, then files, each by name."""
    entry = confine_entry(str(root) if dir_path is None else dir_path, root, "dir_path")
    folder = Path(confine(str(entry), root, "dir_path"))
    if not folder.is_dir():
        raise Refused(f"dir_path: no such folder: {dir_path}")
    entries = []
    truncated = False
    try:
        with os.scandir(folder) as scan:
            for item in scan:
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                path = folder / item.name
                if item.is_symlink():
                    # Shown only when it points at something under root.
                    try:
                        confine(str(path), root, "dir_path")
                    except Refused:
                        continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                is_folder = stat.S_ISDIR(info.st_mode)
                if not is_folder and not stat.S_ISREG(info.st_mode):
                    continue
                entries.append(
                    {
                        "name": item.name,
                        "path": str(path),
                        "type": "folder" if is_folder else "file",
                        "bytes": None if is_folder else info.st_size,
                        "modified": _modified(info),
                    }
                )
    except OSError as exc:
        raise Refused(f"dir_path: cannot list ({exc.strerror})") from None
    entries.sort(key=lambda e: (e["type"] != "folder", e["name"].casefold(), e["name"]))
    return {"path": str(folder), "entries": entries, "truncated": truncated}


def delete_file_in(root: Path, file_path: Any) -> dict[str, Any]:
    """Remove a file, a link or an empty folder under ``root``; never ``root`` itself."""
    target = confine_entry(file_path, root, "file_path")
    if target == root:
        raise Refused("the data folder itself cannot be deleted")
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        raise Refused(f"file_path: no such file or folder: {file_path}") from None
    try:
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(target)
            kind = "folder"
        else:
            os.unlink(target)
            kind = "link" if stat.S_ISLNK(info.st_mode) else "file"
    except OSError as exc:
        if stat.S_ISDIR(info.st_mode) and exc.errno == errno.ENOTEMPTY:
            raise Refused("file_path: the folder is not empty") from None
        raise Refused(f"file_path: cannot delete ({exc.strerror})") from None
    return {"path": str(target), "deleted": kind}


FILE_TOOL_NAMES = ("put_file", "get_file", "list_files", "delete_file")


def register_file_tools(server: Any, root: Path, limit: int) -> list[str]:
    """Add put_file, get_file, list_files and delete_file to ``server``."""
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import EmbeddedResource, ToolAnnotations
    from pydantic import ConfigDict, Field

    def answer(run: Callable[[], Any]) -> Any:
        try:
            return run()
        except Refused as exc:
            raise ToolError(str(exc)) from None

    FilePath = Annotated[
        str,
        Field(
            strict=True,
            min_length=1,
            description=f"Where the file is: a path under {root} (absolute, or relative to it). '..' is refused.",
        ),
    ]

    def put_file(
        file_path: FilePath,
        content_base64: Annotated[
            str,
            Field(
                strict=True,
                description=(
                    "The file's bytes in standard base64 (padded, no white space), at most "
                    f"{limit} bytes once decoded. A PCP file handle given here arrives as base64."
                ),
            ),
        ],
        overwrite: Annotated[
            bool,
            Field(
                strict=True,
                description="Replace a file already at file_path. Off by default: an existing file is refused.",
            ),
        ] = False,
    ) -> dict[str, Any]:
        return answer(lambda: put_file_in(root, limit, file_path, content_base64, overwrite))

    put_file.__doc__ = (
        f"Save a file into {root} from base64 bytes, for the PDF tools to work on. Makes the folders "
        "on the way, writes the file whole or not at all, and refuses a file already there unless "
        "overwrite is true (which replaces it). Answers its path, size and SHA-256."
    )

    def get_file(file_path: FilePath) -> EmbeddedResource:
        return answer(lambda: get_file_in(root, limit, file_path))

    get_file.__doc__ = (
        f"Get a file from {root} as an embedded resource (base64 blob, its media type read from its "
        f"bytes), for example a PDF another tool wrote. At most {limit} bytes."
    )

    def list_files(
        dir_path: Annotated[
            str,
            Field(
                strict=True,
                min_length=1,
                description=f"The folder to list: {root} or a folder under it (absolute, or relative to it).",
            ),
        ] = str(root),
    ) -> dict[str, Any]:
        return answer(lambda: list_files_in(root, dir_path))

    list_files.__doc__ = (
        f"List files and folders in {root} or a folder under it: name, path, type (file or folder), "
        f"size in bytes and when it was modified, folders first. At most {MAX_LIST_ENTRIES} entries."
    )

    def delete_file(file_path: FilePath) -> dict[str, Any]:
        return answer(lambda: delete_file_in(root, file_path))

    delete_file.__doc__ = (
        f"Delete a file or an empty folder under {root}; a link is removed itself, not what it points "
        f"to. Never {root} itself."
    )

    tools = (
        (
            put_file,
            ToolAnnotations(
                title="Save a file",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        (
            get_file,
            ToolAnnotations(
                title="Get a file", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
        ),
        (
            list_files,
            ToolAnnotations(
                title="List files", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
        ),
        (
            delete_file,
            ToolAnnotations(
                title="Delete a file",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
    )
    # This module's annotations are strings (PEP 563); these name local values.
    scope = dict(locals())
    for fn, hints in tools:
        fn.__annotations__ = typing.get_type_hints(fn, localns=scope, include_extras=True)
        server.tool(annotations=hints)(fn)
        # Closed objects: an unknown argument is refused, not ignored.
        tool = server._tool_manager.get_tool(fn.__name__)
        base = tool.fn_metadata.arg_model
        closed = type(
            base.__name__, (base,), {"model_config": ConfigDict(extra="forbid", arbitrary_types_allowed=True)}
        )
        tool.fn_metadata.arg_model = closed
        tool.parameters = closed.model_json_schema(by_alias=True)
    return [fn.__name__ for fn, _ in tools]


def build_server():
    from mcp.server.mcpserver import MCPServer
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    root = data_root()
    allow_signing_network = env_flag("PDF_MCP_ALLOW_SIGNING_NETWORK")
    enable_ai = env_flag("PDF_MCP_ENABLE_AI_TOOLS")

    server = MCPServer("PDF Handler")
    names = []
    for tool in exposed_tools(enable_ai):
        wrapped = guarded(tool.callable.resolve(), root, allow_signing_network)
        wrapped.__doc__ = tool.description
        server.tool()(wrapped)
        names.append(tool.name)
    names.extend(register_file_tools(server, root, max_file_bytes()))

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    logger.info(
        "pdf-mcp: %d tools, data under %s, ai tools %s, signing network %s",
        len(names),
        root,
        "on" if enable_ai else "off",
        "on" if allow_signing_network else "off",
    )
    return server, names


class BearerAuth:
    """ASGI middleware: every HTTP request but ``GET /health`` sends the key."""

    def __init__(self, app: Any, key: str, open_paths: Iterable[str] = ("/health",)) -> None:
        self.app = app
        self.key = key.encode("utf-8")
        self.open_paths = set(open_paths)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        given = b""
        for header, value in scope.get("headers", []):
            if header == b"authorization":
                given = value
                break
        scheme, _, token = given.partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(token.strip(), self.key):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def transport_security():
    from mcp.server.transport_security import TransportSecuritySettings

    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # Loopback always; MCP_ALLOWED_HOSTS adds to it (an address it is published on).
    hosts = DEFAULT_ALLOWED_HOSTS + [h.strip() for h in raw.split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        # Requests from a browser page are not expected; none is allowed.
        allowed_origins=[],
    )


def build_app():
    server, _ = build_server()
    app = server.streamable_http_app(
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
        stateless_http=env_flag("MCP_STATELESS"),
        transport_security=transport_security(),
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
    )
    key = os.environ.get("MCP_API_KEY", "")
    if key:
        return BearerAuth(app, key)
    logger.warning("MCP_API_KEY is not set: /mcp answers anyone who can reach this port")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("PDF_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        build_app(),
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level=os.environ.get("PDF_MCP_LOG_LEVEL", "INFO").lower(),
    )


if __name__ == "__main__":
    main()
