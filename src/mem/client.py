"""HTTP client — standard library only.

The CLI's whole job is to exchange **coordinates**, never bodies
(``MEM-CLI.md`` C1): this module never reads a note file off disk for anything
except ``mem add``, which is the one verb that legitimately carries content.

A connection failure becomes ``server_unreachable`` (exit ``2``) and is **never**
retried or queued — a refused write is the design (``MEM-SERVER.md`` §6).
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import MemError
from .version import API_VERSION


def _multipart(
    fields: dict[str, str], file_name: str, filename: str, content: bytes
) -> tuple[bytes, str]:
    """Encode one file plus some scalar fields, without a dependency."""
    boundary = uuid.uuid4().hex
    buffer = io.BytesIO()
    for key, value in fields.items():
        buffer.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        )
        buffer.write(str(value).encode())
        buffer.write(b"\r\n")
    buffer.write(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_name}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
    )
    buffer.write(content)
    buffer.write(f"\r\n--{boundary}--\r\n".encode())
    return buffer.getvalue(), f"multipart/form-data; boundary={boundary}"


def _multipart_many(items: list[dict[str, Any]], project: str, message: str) -> tuple[Iterable[bytes], str]:
    """Stream one multipart request without assembling the whole batch in RAM."""
    boundary = uuid.uuid4().hex
    metadata = [{k: v for k, v in item.items() if k not in {"content", "content_path", "path"}}
                for item in items]
    fields = {"project": project, "message": message, "items": json.dumps(metadata)}

    def chunks():
        for name, value in fields.items():
            yield f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            yield value.encode("utf-8")
            yield b"\r\n"
        for index, item in enumerate(items):
            filename = str(item["filename"]).replace('"', "_").replace("\r", "_").replace("\n", "_")
            yield (f'--{boundary}\r\nContent-Disposition: form-data; name="file{index}"; '
                   f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
            if "content_path" in item:
                with Path(item["content_path"]).open("rb") as stream:
                    while part := stream.read(64 * 1024):
                        yield part
            else:
                body = item["content"]
                for offset in range(0, len(body), 64 * 1024):
                    yield body[offset:offset + 64 * 1024]
            yield b"\r\n"
        yield f"--{boundary}--\r\n".encode()

    return chunks(), f"multipart/form-data; boundary={boundary}"


#: Default per-request timeout, kept short for interactive verbs.
DEFAULT_TIMEOUT = 20.0

#: A cold hybrid search or index rebuild loads the embedding model (~130 MB of ONNX)
#: and syncs the index, which comfortably exceeds the interactive timeout. The
#: embedding verbs passed to the server need a longer allowance, or a slow-but-alive
#: daemon is reported as `server_unreachable`/exit 2 instead of its real error.
EMBED_TIMEOUT = 180.0


class Client:
    # BUG(medium): the server URL is never validated, so a malformed --server is
    # only discovered inside the first request.
    # Why valid: __init__ only does server.rstrip("/"); _request builds
    # urllib.request.Request outside its try and catches no ValueError, so a URL
    # with no colon (e.g. --server 127.0.0.1) raises "unknown url type" and
    # escapes main(), which catches only MemError/KeyboardInterrupt -- a
    # traceback with exit 1 and no JSON, against errors.py's "every failure
    # travels as one of these codes" contract.
    # Fix hint: validate with urllib.parse.urlsplit here, require an http/https
    # scheme and a non-empty netloc, and raise MemError("invalid_request", ...).
    def __init__(
        self, server: str, token: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | Iterable[bytes] | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        url = self.server + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        body = data
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        if headers:
            request_headers.update(headers)
        if auth and self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.setdefault("User-Agent", API_VERSION)

        # BUG(medium): urllib.request.Request is constructed outside the try
        # below, and ValueError is absent from the caught tuple.
        # Why valid: a server URL with no colon (e.g. --server 127.0.0.1) makes
        # Request raise "ValueError: unknown url type"; the except that follows
        # catches only URLError/socket.timeout/ConnectionError/OSError, and
        # main() catches only MemError/KeyboardInterrupt, so the run exits 1 as
        # a traceback with no JSON error object.
        # Fix hint: move the Request construction inside the try and add a
        # dedicated except ValueError that raises MemError("invalid_request",
        # ...).
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from exc
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MemError(
                "server_unreachable",
                f"cannot reach {self.server}: {reason}",
                {"server": self.server},
            ) from exc

        if status == 204 or not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise MemError(
                "internal_error", f"server returned non-JSON ({status})"
            ) from exc

    @staticmethod
    def _error_from_http(exc: urllib.error.HTTPError) -> MemError:
        raw = b""
        try:
            raw = exc.read()
            payload = json.loads(raw)
            error = payload.get("error", {})
            code = error.get("code")
            if code:
                return MemError(
                    code,
                    error.get("message", exc.reason or code),
                    error.get("detail") or {},
                    http_status=exc.code,
                )
        except Exception:  # noqa: BLE001 - fall through to the generic mapping
            pass
        # A status we recognise but that arrived without a code.
        #
        # No 409 here: the only 409 the server still sends is `device_exists`
        # (exit 8), and `staging_diverged`/exit 5 is retired (S30). Mapping a
        # code-less 409 to a retired code was the last way a user could still
        # *see* exit 5, so an unclassifiable 409 now falls through to
        # `internal_error` instead.
        by_status = {
            401: "unauthorized",
            404: "no_project",
            412: "stale_revision",
            422: "schema_invalid",
            423: "lock_held",
            503: "server_unreachable",
        }
        code = by_status.get(exc.code, "internal_error")
        detail = {}
        if raw:
            detail["body"] = raw[:400].decode("utf-8", "replace")
        return MemError(code, f"HTTP {exc.code} from server", detail, http_status=exc.code)

    # -- endpoints ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", auth=False)

    def enroll(
        self, display_name: str, enroll_secret: str, *, alias: str,
        hostname: str, os_name: str, arch: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/enroll",
            json_body={"display_name": display_name, "enroll_secret": enroll_secret,
                       "alias": alias, "hostname": hostname, "os": os_name, "arch": arch},
            auth=False,
        )

    def status(self, project: str) -> dict[str, Any]:
        return self._request("GET", "/v1/status", params={"project": project})

    def queue_progress(self, through: int | None = None) -> dict[str, Any]:
        return self._request("GET", "/v1/queue", params={"through": through})

    def policy(self) -> dict[str, Any]:
        return self._request("GET", "/v1/policy")

    def projects(self) -> Any:
        return self._request("GET", "/v1/projects")

    def notes(self, project: str, **filters: Any) -> Any:
        params = {"project": project, **filters}
        return self._request("GET", "/v1/notes", params=params)

    def note(
        self,
        project: str,
        note_id: str,
        revision: str | None = None,
        *,
        section: str | None = None,
        span: tuple[int, int] | None = None,
        max_bytes: int | None = None,
        link_only: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/notes/{urllib.parse.quote(note_id)}",
            params={
                "project": project,
                "revision": revision,
                # A slice, so a caller can consume a search hit's `line_range` (S35).
                "section": section,
                "range": f"{span[0]}-{span[1]}" if span else None,
                "max_bytes": max_bytes,
                "link_only": "true" if link_only else None,
            },
        )

    def search(self, project: str, query: str, **filters: Any) -> Any:
        params = {"project": project, "q": query, **filters}
        return self._request("GET", "/v1/search", params=params)

    def outline(self, project: str, note_id: str, revision: str | None = None) -> dict[str, Any]:
        """Headings only — the cheap rung, served without transferring a body."""
        return self._request(
            "GET",
            "/v1/outline",
            params={"project": project, "id": note_id, "revision": revision},
        )

    def manifest(self, project: str, topic: str = "") -> dict[str, Any]:
        return self._request("GET", "/v1/manifest", params={"project": project, "topic": topic})

    def index_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/index/status")

    def index_rebuild(self) -> dict[str, Any]:
        return self._request("POST", "/v1/index/rebuild")

    def devices(self) -> Any:
        return self._request("GET", "/v1/devices")

    def retire_device(self, device_id: str) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/devices/retire", json_body={"device_id": device_id}
        )

    def raw_url(self, project: str, note_id: str, revision: str | None = None) -> str:
        url = f"{self.server}/raw/{urllib.parse.quote(project)}/{urllib.parse.quote(note_id)}"
        if revision:
            url += "?" + urllib.parse.urlencode({"rev": revision})
        return url

    def raw_bytes(
        self, project: str, note_id: str, revision: str | None = None
    ) -> bytes:
        """Fetch the exact committed bytes of a note."""
        url = self.raw_url(project, note_id, revision)
        headers = {"Accept": "text/markdown"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from exc
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise MemError(
                "server_unreachable", f"cannot reach {self.server}: {exc}"
            ) from exc

    def raw(self, project: str, note_id: str, revision: str | None = None) -> str:
        """Fetch a note body. Used by ``mem read --outline``, which genuinely
        needs the text; every other verb exchanges coordinates only."""
        return self.raw_bytes(project, note_id, revision).decode("utf-8", "replace")

    def add(
        self,
        project: str,
        path: str,
        content: bytes,
        *,
        filename: str,
        idempotency_key: str,
        if_match: str | None = None,
        origin_path: str | None = None,
        message: str | None = None,
        kind: str = "note",
    ) -> dict[str, Any]:
        """Write **and version** one note — one atomic act (`S23`)."""
        fields = {"project": project, "path": path, "kind": kind}
        # The absolute path on THIS device. It cannot be derived server-side from
        # the note id, which is why S12 added the field -- and why not sending it
        # silently degrades `origin` into a copy of the id.
        if origin_path:
            fields["origin_path"] = origin_path
        if message:
            fields["message"] = message
        body, content_type = _multipart(fields, "file", filename, content)
        headers = {"Idempotency-Key": idempotency_key}
        if if_match:
            headers["If-Match"] = if_match
        return self._request(
            "POST", "/v1/add", data=body, content_type=content_type, headers=headers
        )

    def add_many(self, project: str, items: list[dict[str, Any]], *, message: str,
                 idempotency_key: str) -> dict[str, Any]:
        body, content_type = _multipart_many(items, project, message)
        return self._request(
            "POST", "/v1/add-many", data=body, content_type=content_type,
            headers={"Idempotency-Key": idempotency_key},
        )

    def rm(
        self,
        project: str,
        note_id: str,
        *,
        idempotency_key: str,
        if_match: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Remove **and version** one note — one atomic act (`S23`)."""
        headers = {"Idempotency-Key": idempotency_key}
        if if_match:
            headers["If-Match"] = if_match
        return self._request(
            "POST",
            "/v1/rm",
            json_body={"project": project, "id": note_id, "message": message},
            headers=headers,
        )

    def mv(self, project: str, source: str, destination: str, *,
           idempotency_key: str | None = None, if_match: str | None = None,
           message: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if if_match:
            headers["If-Match"] = if_match
        return self._request("POST", "/v1/mv",
                             json_body={"project": project, "source": source,
                                        "destination": destination, "message": message,
                                        "dry_run": dry_run}, headers=headers)

    def repath(
        self,
        project: str,
        old_path: str,
        new_path: str,
        *,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        message: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if if_match:
            headers["If-Match"] = if_match
        return self._request(
            "POST",
            "/v1/repath",
            json_body={
                "project": project,
                "old_path": old_path,
                "new_path": new_path,
                "message": message,
                "dry_run": dry_run,
            },
            headers=headers,
        )
