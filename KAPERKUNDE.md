# Kaperkunde's fork of pdf-mcp

This repository is Kaperkunde's reviewed, pinned copy of
[nfsarch33/pdf-mcp-server](https://github.com/nfsarch33/pdf-mcp-server),
kept to build one container image,
`ghcr.io/kaperkunde/pdf-mcp-server`, that a [PCP](https://pcp.gg) can run as
a service and add as an MCP server. Everything upstream wrote is unchanged
except the dependency lock; what this fork adds is listed below.

## The pin

| | |
| --- | --- |
| Upstream | https://github.com/nfsarch33/pdf-mcp-server |
| Tag | `v2.0.0` (the first release tag under Apache-2.0) |
| Tag object | `007d6be3dba0a84aa94c50c70db61b3916431ff4` |
| Commit | `f5f3cee3b17f24ea8221058702c4163370ce2c90` |
| Reviewed | 2026-11-05 |

Image tags in this fork are `kk-v<upstream version>-<n>` (for example
`kk-v2.0.0-1`): pushing one runs `.github/workflows/image.yml`, which tests,
builds `linux/amd64` and `linux/arm64`, and pushes to GHCR with provenance and
an SBOM. Refer to the image by the digest that workflow prints.

## What this fork adds

1. **Dependencies past known advisories.** `uv.lock` lifts pillow to 13.0.0,
   certifi to 2026.10.23 and pypdf to 6.21.4 (pip-audit found 39, 2 and 7
   advisories in the versions v2.0.0 locked). Nothing else in the lock moved.
2. **An HTTP entry point, `python -m pdf_mcp.kk_http`** (`pdf_mcp/kk_http.py`),
   serving the same tools over MCP Streamable HTTP at `/mcp`, with `GET /health`.
   Before any tool runs it:
   - resolves every file and directory argument (`*_path`, `*_paths`, `*_dir`,
     `*_file`, `pdf_list`, and what `batch_process` passes on) with
     `realpath` and refuses it outside `/data`; relative paths are taken from
     `/data`, and the tool gets the resolved path;
   - refuses `timestamp_url` and `allow_fetching: true` on `sign_pdf` and
     `sign_pdf_pem` (an RFC 3161 server, OCSP and CRL fetches), unless
     `PDF_MCP_ALLOW_SIGNING_NETWORK=1`;
   - refuses `pfx_password_env` and `key_password_env`, which would let a
     caller use, and probe, any variable of the server's environment; use the
     `*_password_file` arguments (a file under `/data`) instead;
   - accepts only Tesseract language codes (`eng`, `eng+nld`) for `language`
     and `ocr_language`, never a path;
   - answers errors without Python tracebacks.
3. **No LLM tools.** The four `ai` tools are not registered unless
   `PDF_MCP_ENABLE_AI_TOOLS=1`, and the image does not install `openai` (the
   `llm` extra). `PDF_MCP_ENABLE_REMOTE_LLM` and `OPENAI_API_KEY` are unset.
4. **The image** (`Dockerfile`): Python 3.12 slim (Debian trixie) and uv, both
   pinned by digest; packages from `uv.lock` only (`uv sync --frozen`, the
   `ocr` extra, no dev tools); `poppler-utils`, `tesseract-ocr` with English
   and Dutch; runs as uid 10001, never root; `/data` is its volume and
   working directory. No `setup_environment.sh`, nothing piped into a shell.
5. **Files in and out over MCP** (`kk_http.py`, `register_file_tools`), so
   an assistant can hand the tools a file and take the result back without
   access to the volume:
   - `put_file(file_path, content_base64, overwrite=false)` decodes strict
     base64 (padded, no white space), makes the folders on the way, writes
     `<name>.<random>.part` with an exclusive create and renames it into
     place; a file already there is refused unless `overwrite` is true.
     Answers `path`, `bytes`, `sha256` and `replaced`.
   - `get_file(file_path)` answers the file as an MCP embedded resource: a
     base64 `blob`, a `mimeType` read from its bytes (PDF, PNG, JPEG, TIFF,
     GIF, WebP, UTF-8 text, else `application/octet-stream`) and a
     `file:///data/<path>` URI whose last segment is the file's name.
   - `list_files(dir_path="/data")`: name, path, type (`file` or `folder`),
     bytes and modified time (UTC, ISO 8601), folders first, at most 5000.
   - `delete_file(file_path)`: a file, a link (never what it points to) or
     an empty folder; never `/data` itself.

   All four refuse `..`, resolve the folder a name is in with `realpath` and
   refuse it outside `/data` (a link is followed only to read or list
   through, and only into `/data`), refuse unknown and mistyped arguments
   (closed input schemas, no coercion), answer a refusal as a tool error
   (`isError`), and stop at `PDF_MCP_MAX_FILE_BYTES` (default 128 MiB),
   checked on the base64 length before decoding and again after. With
   [PCP](https://pcp.gg): a kept file's handle (`{"$result": id}`) given as
   `content_base64` arrives as base64, and `get_file`'s answer is kept as a
   file and comes back as a handle, so the bytes never pass through the
   assistant.
6. Upstream's `ai-review.yml` (sends pull request diffs to OpenAI) and
   `bootstrap-labels.yml` (a weekly scheduled job) workflows are removed.

## Running it

```sh
docker run -d --name pdf-mcp \
  -p 127.0.0.1:8000:8000 \
  -v pdf-mcp-data:/data \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  ghcr.io/kaperkunde/pdf-mcp-server@sha256:<digest>
```

Put PDFs in the `pdf-mcp-data` volume, or send them with `put_file`; tools
read and write there only, and `get_file` takes results out.

| Variable | Default | What it does |
| --- | --- | --- |
| `MCP_API_KEY` | unset | When set, every request but `/health` must send `Authorization: Bearer <key>`. |
| `MCP_ALLOWED_HOSTS` | unset | `Host` values accepted besides loopback (comma separated, `host:*` for any port, `*` for any host): the address you publish on, if it is not loopback. |
| `MCP_PORT`, `MCP_HOST`, `MCP_PATH` | `8000`, `0.0.0.0`, `/mcp` | Where it listens. |
| `MCP_STATELESS` | off | `1` for stateless Streamable HTTP (no session id). |
| `PDF_MCP_DATA_DIR` | `/data` | The one directory tools may touch. |
| `PDF_MCP_ENABLE_AI_TOOLS` | off | `1` registers the `ai` tools (they still find no LLM in this image). |
| `PDF_MCP_ALLOW_SIGNING_NETWORK` | off | `1` lets signing reach a timestamp server and fetch revocation data. |
| `PDF_MCP_MAX_FILE_BYTES` | `134217728` (128 MiB) | The largest file `put_file` takes and `get_file` answers. |

## Tools exposed by default (57)

| Verb | Tools |
| --- | --- |
| `form` | `get_pdf_form_fields`, `fill_pdf_form`, `fill_pdf_form_any`, `create_pdf_form`, `get_form_templates`, `create_pdf_form_from_template`, `flatten_pdf`, `clear_pdf_form_fields`, `detect_form_fields` |
| `security` | `encrypt_pdf`, `detect_pii_patterns` |
| `pages` | `merge_pdfs`, `extract_pages`, `rotate_pages`, `reorder_pages`, `insert_pages`, `remove_pages`, `optimize_pdf`, `split_pdf` |
| `text` | `add_text_annotation`, `update_text_annotation`, `remove_text_annotation`, `remove_annotations`, `redact_text_regex`, `add_page_numbers`, `add_bates_numbering`, `add_text_watermark`, `add_highlight`, `add_date_stamp`, `add_comment`, `update_comment`, `remove_comment` |
| `metadata` | `get_pdf_metadata`, `set_pdf_metadata`, `sanitize_pdf_metadata`, `detect_pdf_type` |
| `sign` | `verify_digital_signatures`, `sign_pdf`, `sign_pdf_pem`, `add_signature_image`, `update_signature_image`, `remove_signature_image` |
| `extract` | `get_pdf_text_blocks`, `extract_tables`, `extract_images`, `extract_links`, `detect_barcodes`, `extract_text`, `export_pdf` |
| `ocr` | `get_ocr_languages`, `get_image_info` |
| `batch` | `compare_pdfs`, `batch_process` |
| files (this fork) | `put_file`, `get_file`, `list_files`, `delete_file` |

Off unless `PDF_MCP_ENABLE_AI_TOOLS=1`: `get_llm_backend_info`,
`auto_fill_pdf_form`, `extract_structured_data`, `analyze_pdf_content`.

## What the review found at v2.0.0

- **Network.** Off by default upstream except where a caller asks:
  `sign_pdf`/`sign_pdf_pem` take `timestamp_url` and `allow_fetching`
  (pdf_tools.py:1945, 1853); this fork refuses both. Signature verification
  never fetches (`allow_fetching=False`, pdf_tools.py:1773). The LLM paths
  (pdf_tools.py:4332-4650, llm_setup.py:16-65) probe `localhost:8100`, call
  Ollama or OpenAI; OpenAI needs a key and an opt-in, but a caller's
  `backend="openai"` skips the opt-in for non-sensitive data
  (pdf_tools.py:4607). Only the `ai` tools reach them; this fork does not
  register those, and the image has neither `openai` nor `ollama`.
- **Paths.** Upstream takes any path the caller names (`_ensure_file`,
  `_prepare_output`, pdf_tools.py:143-159, and `mkdir(parents=True)` on
  outputs); this fork confines them to `/data`. Names the tools derive
  themselves (split by bookmark, extracted images) are sanitised upstream
  (pdf_tools.py:4112, 2834).
- **Secrets from the environment.** `_resolve_secret` reads any variable a
  caller names (pdf_tools.py:1867); refused here.
- **Processes.** No `shell=True` anywhere. Tesseract runs through pytesseract
  with an argument list; the language code reaches `tesseract -l`, so this
  fork accepts codes only. `llm_setup.py:90` runs `ollama list` (argument
  list, `ai` path only). Poppler is invoked only through pdf2image when
  fillpdf flattens as images, which no tool asks for.
- **Errors** carried a full traceback (server.py:62-65); this entry point
  does not.
- **`batch_process`** declares `**kwargs` (pdf_tools.py:3671), which MCP
  publishes as a required object argument `kwargs`; upstream then passes it
  on nested and the call fails. Here a caller sends `"kwargs": {}` (or the
  options for `optimize_pdf`) and they are unwrapped and checked like the
  rest.
- **Left as they are:** `redact_text_regex` runs a caller's regular
  expression (CPU only); tools run with no time limit; MuPDF, Pillow and
  Tesseract parse untrusted files (the reason for the non-root user, the
  single volume and the read-only container above). pip-audit covers PyPI
  advisories only, not MuPDF's own CVEs or Debian's packages in the image.

## Licences

The upstream code is Apache-2.0 (see `LICENSE` and `NOTICE`), and so are
this fork's additions. **The image is not only Apache-2.0**: it bundles
PyMuPDF and MuPDF, which are AGPL-3.0 (or a commercial licence from Artifex).
See `NOTICE` for what distributing the image means.
