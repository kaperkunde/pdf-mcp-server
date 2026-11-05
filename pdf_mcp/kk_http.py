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

Settings (environment): ``MCP_HOST`` (default ``0.0.0.0``), ``MCP_PORT``
(default ``8000``), ``MCP_PATH`` (default ``/mcp``), ``MCP_STATELESS``
(``1`` for stateless Streamable HTTP), plus the ones above.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

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
            raise Refused(f"{name} is disabled on this server (no network); set PDF_MCP_ALLOW_SIGNING_NETWORK=1 to allow")
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
