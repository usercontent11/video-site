#!/usr/bin/env python3
"""Enrich missing media metadata for existing public.videos records.

The category scraper is intentionally left untouched. This worker uses the same
DATABASE_URL / psycopg infrastructure but isolates media extraction and status
tracking in this module.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb
from bs4 import BeautifulSoup

SOURCE_ORIGIN = os.getenv("SOURCE_ORIGIN", "https://mobilexh.live").rstrip("/")
SITE_HOST = (urlsplit(SOURCE_ORIGIN).hostname or "mobilexh.live").lower()
MEDIA_HOST_SUFFIXES = ("xhcdn.com", "cdnsolutions.media")
MAX_HTML_BYTES = int(os.getenv("MEDIA_MAX_HTML_BYTES", str(3 * 1024 * 1024)))
MAX_REDIRECTS = 5
HTTP_ATTEMPTS = 3
MAX_RECORD_ATTEMPTS = 5
USER_AGENT = "VideoCatalogMediaEnricher/1.0 (+GitHub Actions; metadata-only)"

URL_COLUMNS = (
    "thumbnail_path",
    "image_path",
    "preview_thumb_path",
    "sprite_path",
    "trailer_path",
    "trailer_fallback_path",
)
ALL_MEDIA_COLUMNS = URL_COLUMNS + ("thumb_id", "trailer_cdn_version")
JSON_TO_COLUMN = {
    "thumbURL": "thumbnail_path",
    "imageURL": "image_path",
    "previewThumbURL": "preview_thumb_path",
    "spriteURL": "sprite_path",
    "trailerURL": "trailer_path",
    "trailerFallbackUrl": "trailer_fallback_path",
    "thumbId": "thumb_id",
}
DB_MEDIA_COLUMNS = ", ".join(ALL_MEDIA_COLUMNS)

TRACKER_DDL = """
CREATE TABLE IF NOT EXISTS public.video_media_enrichment (
    video_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    attempts SMALLINT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_http_status INTEGER,
    reason_code TEXT,
    last_error TEXT,
    missing_fields TEXT[] NOT NULL DEFAULT '{}',
    media_hosts JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_video_media_enrichment_retry
    ON public.video_media_enrichment(status, next_retry_at, video_id);
"""

LOG = logging.getLogger("media_enricher")


class ExtractionError(Exception):
    def __init__(self, code: str, message: str, *, terminal: bool = False,
                 http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.terminal = terminal
        self.http_status = http_status


@dataclass
class PageResponse:
    html: str
    final_url: str
    status_code: int
    content_type: str


@dataclass
class ExtractedMedia:
    video_id: int
    page_url: str
    fields: dict[str, Any] = field(default_factory=dict)
    hosts: dict[str, str] = field(default_factory=dict)
    source_keys: dict[str, str] = field(default_factory=dict)
    rejected_fields: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    status: str = "partial"
    reason_code: str | None = None
    error: str | None = None
    http_status: int | None = None
    elapsed_seconds: float = 0.0
    attempts_before: int = 0


def video_directory(video_id: int | str) -> str:
    """Return the observed nine-digit grouping; reject longer IDs explicitly."""
    value = int(video_id)
    if value < 0:
        raise ValueError("video ID must be a non-negative integer")
    digits = str(value)
    if len(digits) > 9:
        raise ValueError(
            f"video ID {value} exceeds the observed nine-digit CDN layout; "
            "directory grouping is unverified, so refusing to infer a path"
        )
    padded = digits.zfill(9)
    return "/".join(padded[i:i + 3] for i in (0, 3, 6))


def _same_or_subdomain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _allowed_site_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in ("http", "https")
        and bool(host)
        and (_same_or_subdomain(host, SITE_HOST) or _same_or_subdomain(host, "mobilexh.live"))
        and not parsed.username
        and not parsed.password
    )


def build_page_url(page_path: str) -> str:
    candidate = urljoin(SOURCE_ORIGIN + "/", str(page_path or "").strip())
    parsed = urlsplit(candidate)
    if not _allowed_site_url(candidate) or parsed.scheme != "https":
        raise ExtractionError("invalid_page_url", "page_path is not a valid HTTPS source-site URL", terminal=True)
    if not parsed.path.startswith("/videos/"):
        raise ExtractionError("invalid_page_path", "page_path is not under /videos/", terminal=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _host_allowed_media(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(_same_or_subdomain(host, domain) for domain in MEDIA_HOST_SUFFIXES)


def _contains_video_directory(path: str, expected: str) -> bool:
    parts = path.split("/")
    wanted = expected.split("/")
    return any(parts[i:i + 3] == wanted for i in range(max(0, len(parts) - 2)))


def normalize_media_url(raw_url: Any, video_id: int, column: str) -> tuple[str, str]:
    """Validate a primary object's media URL and return (path+query, original host)."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("empty URL")
    value = raw_url.strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must be absolute HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in media URLs")
    host = parsed.hostname.lower().rstrip(".")
    if not _host_allowed_media(host):
        raise ValueError("unexpected media hostname")
    if not parsed.path or not parsed.path.startswith("/a/"):
        raise ValueError("media path must start with /a/")
    path_parts = parsed.path.split("/")
    if len(path_parts) < 4 or not path_parts[2] or not path_parts[3]:
        raise ValueError("media path is incomplete (expected /a/{token}/...)")
    expected_dir = video_directory(video_id)
    if not _contains_video_directory(parsed.path, expected_dir):
        raise ValueError(f"media path does not contain video directory {expected_dir}")

    extension = parsed.path.lower().rsplit(".", 1)[-1] if "." in parsed.path.rsplit("/", 1)[-1] else ""
    image_extensions = {"jpg", "jpeg", "png", "webp", "avif", "gif"}
    video_extensions = {"mp4", "webm", "m3u8"}
    if column in ("thumbnail_path", "image_path", "sprite_path"):
        if extension not in image_extensions:
            raise ValueError("field does not point to a recognized image file")
    elif column == "preview_thumb_path":
        if extension not in image_extensions | video_extensions:
            raise ValueError("preview field does not point to a recognized preview file")
    elif column in ("trailer_path", "trailer_fallback_path"):
        if extension not in video_extensions:
            raise ValueError("trailer field does not point to a recognized video file")

    # urlsplit preserves the escaped path and query. Fragments are client-side only.
    normalized = urlunsplit(("", "", parsed.path, parsed.query, ""))
    if not normalized.startswith("/a/") or "/../" in normalized:
        raise ValueError("normalized media path is malformed")
    return normalized, host


def trailer_cdn_version(host: str | None) -> int | None:
    if not host:
        return None
    match = re.match(r"^thumb-v([0-9]+)\.", host.lower())
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 32767 else None


def _parse_initial_state(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="initials-script")
    if script is None:
        raise ExtractionError("parse_failure", "script#initials-script not found")
    raw = script.string or script.get_text() or ""
    assignment = re.search(r"(?:window\s*\.\s*)?initials\s*=\s*", raw)
    if assignment:
        raw = raw[assignment.end():]
    raw = raw.strip().lstrip("\ufeff")
    decoder = json.JSONDecoder()
    try:
        state, _end = decoder.raw_decode(raw)
    except json.JSONDecodeError as exc:
        raise ExtractionError("parse_failure", f"initial state is not valid JSON: {exc.msg}") from exc
    if not isinstance(state, dict):
        raise ExtractionError("parse_failure", "initial state root is not an object")
    return state


def _numeric_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if str(value).strip().isdigit() else None


def _card_lists(state: dict[str, Any]) -> list[list[Any]]:
    """Known list locations observed in actual initials state, not a global key scan."""
    found: list[list[Any]] = []
    pages = state.get("pagesCategoryComponent")
    if isinstance(pages, dict):
        props = pages.get("trendingVideoListProps")
        if isinstance(props, dict) and isinstance(props.get("videoThumbProps"), list):
            found.append(props["videoThumbProps"])

    related = state.get("relatedVideosComponent")
    if isinstance(related, dict):
        initial = related.get("videoTabInitialData")
        if isinstance(initial, dict):
            props = initial.get("videoListProps")
            if isinstance(props, dict) and isinstance(props.get("videoThumbProps"), list):
                found.append(props["videoThumbProps"])
    return found


def _same_page_url(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        a, b = urlsplit(left), urlsplit(right)
    except ValueError:
        return False
    if not _allowed_site_url(left) or not _allowed_site_url(right):
        return False
    return a.path.rstrip("/") == b.path.rstrip("/")


def extract_media_from_html(html: str, video_id: int, requested_url: str) -> ExtractedMedia:
    """Extract only from the page's verified primary videoModel and same-page card."""
    try:
        video_directory(video_id)
    except ValueError as exc:
        raise ExtractionError("unsupported_video_id_layout", str(exc), terminal=True) from exc
    try:
        state = _parse_initial_state(html)
    except ExtractionError:
        raise

    primary = state.get("videoModel")
    if not isinstance(primary, dict) or primary.get("modelName") not in (None, "videoModel"):
        raise ExtractionError("primary_model_missing", "top-level videoModel is missing")
    primary_id = _numeric_id(primary.get("id"))
    if primary_id != int(video_id):
        raise ExtractionError(
            "id_mismatch",
            f"top-level videoModel.id={primary.get('id')!r} does not match requested video ID {video_id}",
            terminal=True,
        )

    entity = state.get("videoEntity")
    if isinstance(entity, dict):
        entity_id = _numeric_id(entity.get("id"))
        if entity_id is not None and entity_id != int(video_id):
            raise ExtractionError(
                "id_mismatch",
                f"top-level videoEntity.id={entity.get('id')!r} does not match requested video ID {video_id}",
                terminal=True,
            )

    primary_page_url = primary.get("pageURL") or requested_url
    if not _allowed_site_url(str(primary_page_url)):
        raise ExtractionError("primary_page_url_invalid", "primary videoModel.pageURL is not on the source site", terminal=True)

    same_id_card: dict[str, Any] | None = None
    for card_list in _card_lists(state):
        for item in card_list:
            if not isinstance(item, dict) or _numeric_id(item.get("id")) != int(video_id):
                continue
            if item.get("videoType") not in (None, "video"):
                continue
            if _same_page_url(item.get("pageURL"), primary_page_url):
                same_id_card = item
                break
        if same_id_card is not None:
            break

    candidates: dict[str, list[tuple[str, Any]]] = {
        "thumbURL": [("videoModel.thumbURL", primary.get("thumbURL"))],
        "imageURL": [("same_page_videoThumbProps.imageURL", same_id_card.get("imageURL") if same_id_card else None)],
        "previewThumbURL": [("videoModel.previewThumbURL", primary.get("previewThumbURL"))],
        "spriteURL": [("videoModel.spriteURL", primary.get("spriteURL"))],
        "trailerURL": [("videoModel.trailerURL", primary.get("trailerURL"))],
        "trailerFallbackUrl": [("videoModel.trailerFallbackUrl", primary.get("trailerFallbackUrl"))],
        "thumbId": [("videoModel.thumbId", primary.get("thumbId"))],
    }

    # A same-ID, same-canonical-page card may fill fields absent from the player model.
    if same_id_card is not None:
        for key in ("thumbURL", "imageURL", "previewThumbURL", "spriteURL", "trailerURL", "trailerFallbackUrl", "thumbId"):
            candidates[key].append(("same_page_videoThumbProps." + key, same_id_card.get(key)))

    result = ExtractedMedia(video_id=int(video_id), page_url=str(primary_page_url))
    for json_key, column in JSON_TO_COLUMN.items():
        chosen = None
        chosen_source = None
        for source_name, raw in candidates[json_key]:
            if raw is None or raw == "":
                continue
            if column == "thumb_id":
                thumb_id = _numeric_id(raw)
                if thumb_id is None or thumb_id < 0:
                    result.rejected_fields[column] = "thumbId is not a non-negative integer"
                    continue
                chosen, chosen_source = thumb_id, source_name
                break
            try:
                path_value, host = normalize_media_url(raw, int(video_id), column)
            except (ValueError, TypeError) as exc:
                result.rejected_fields[column] = str(exc)
                continue
            chosen, chosen_source = path_value, source_name
            result.hosts[column] = host
            break
        result.fields[column] = chosen
        if chosen_source:
            result.source_keys[column] = chosen_source

    trailer_host = result.hosts.get("trailer_path")
    result.fields["trailer_cdn_version"] = trailer_cdn_version(trailer_host)
    if result.fields.get("trailer_cdn_version") is not None:
        result.source_keys["trailer_cdn_version"] = "hostname of validated trailerURL"

    # Successful parse but no validated resources is terminal; it must not loop forever.
    if not any(result.fields.get(column) is not None for column in URL_COLUMNS):
        result.status = "no_media"
        result.reason_code = "no_media"
    elif all(result.fields.get(column) is not None for column in ALL_MEDIA_COLUMNS):
        result.status = "complete"
        result.reason_code = "complete"
    else:
        result.status = "partial"
        result.reason_code = "partial"
    if result.rejected_fields:
        result.error = "Rejected media fields: " + json.dumps(result.rejected_fields, ensure_ascii=True)[:780]
    return result


class HostPacer:
    """Space out request starts to one source host across concurrent workers."""
    def __init__(self, interval_seconds: float):
        self.interval = max(0.0, interval_seconds)
        self.lock = asyncio.Lock()
        self.next_start = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            await asyncio.sleep(delay)


class AdaptiveLimiter:
    """A bounded, dynamically shrinkable request concurrency limiter."""
    def __init__(self, initial_limit: int):
        self.limit = max(1, int(initial_limit))
        self.active = 0
        self.condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self.condition:
            while self.active >= self.limit:
                await self.condition.wait()
            self.active += 1

    async def release(self) -> None:
        async with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    async def reduce(self) -> int:
        async with self.condition:
            self.limit = max(1, self.limit // 2)
            self.condition.notify_all()
            return self.limit


async def _read_limited_response(client: httpx.AsyncClient, url: str) -> PageResponse:
    current = url
    for _redirect_index in range(MAX_REDIRECTS + 1):
        if not _allowed_site_url(current):
            raise ExtractionError("redirect_outside_source", "redirect left the configured source site", terminal=True)
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ExtractionError("bad_redirect", "redirect response did not include Location", http_status=response.status_code)
                target = urljoin(current, location)
                if not _allowed_site_url(target):
                    raise ExtractionError("redirect_outside_source", "redirect points outside the configured source site", terminal=True, http_status=response.status_code)
                current = target
                continue
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    raise ExtractionError("html_too_large", f"HTML response exceeds {MAX_HTML_BYTES} bytes")
            content_type = response.headers.get("content-type", "")
            return PageResponse(
                html=bytes(body).decode(response.encoding or "utf-8", errors="replace"),
                final_url=current,
                status_code=response.status_code,
                content_type=content_type,
            )
    raise ExtractionError("too_many_redirects", f"more than {MAX_REDIRECTS} redirects", terminal=True)


def _failure_result(video_id: int, page_url: str, exc: ExtractionError, elapsed: float) -> ExtractedMedia:
    return ExtractedMedia(
        video_id=video_id,
        page_url=page_url,
        fields={column: None for column in ALL_MEDIA_COLUMNS},
        status="inaccessible" if exc.terminal else "retry",
        reason_code=exc.code,
        error=str(exc)[:900],
        http_status=exc.http_status,
        elapsed_seconds=elapsed,
    )


async def extract_record(
    client: httpx.AsyncClient,
    row: dict[str, Any],
    limiter: AdaptiveLimiter,
    pacer: HostPacer,
) -> ExtractedMedia:
    video_id = int(row["id"])
    started = time.perf_counter()
    try:
        page_url = build_page_url(row["page_path"])
    except ExtractionError as exc:
        return _failure_result(video_id, str(row.get("page_path") or ""), exc, time.perf_counter() - started)

    last_error: ExtractionError | None = None
    for attempt in range(HTTP_ATTEMPTS):
        try:
            await limiter.acquire()
            try:
                await pacer.wait()
                page = await _read_limited_response(client, page_url)
            finally:
                await limiter.release()

            if page.status_code in (401, 403, 404, 410, 451):
                code = "source_restricted" if page.status_code in (401, 403, 451) else "not_found"
                raise ExtractionError(
                    code,
                    f"source returned HTTP {page.status_code}; not retrying this page automatically",
                    terminal=True,
                    http_status=page.status_code,
                )
            if page.status_code == 429 or page.status_code in (500, 502, 503, 504):
                new_limit = await limiter.reduce()
                raise ExtractionError(
                    "http_retryable",
                    f"source returned HTTP {page.status_code}; concurrency reduced to {new_limit}",
                    http_status=page.status_code,
                )
            if page.status_code < 200 or page.status_code >= 300:
                raise ExtractionError("http_failure", f"source returned HTTP {page.status_code}", http_status=page.status_code)
            if page.content_type and "html" not in page.content_type.lower() and "text/plain" not in page.content_type.lower():
                raise ExtractionError("unexpected_content_type", f"expected HTML, got {page.content_type}", http_status=page.status_code)
            try:
                result = extract_media_from_html(page.html, video_id, page.final_url)
            except ExtractionError as exc:
                if exc.code in ("id_mismatch", "primary_model_missing", "primary_page_url_invalid"):
                    exc.terminal = True
                if exc.terminal:
                    exc.http_status = page.status_code
                    raise
                # A normal 200 challenge/error page should not be fetched repeatedly.
                sample = page.html[:120_000].lower()
                challenge = any(term in sample for term in (
                    "captcha", "challenge-platform", "checking your browser",
                    "verify your age", "ageverificationinfo", "access denied",
                ))
                if challenge:
                    raise ExtractionError(
                        "source_restricted",
                        "HTTP 200 page looks like an age-verification or anti-bot challenge; no bypass attempted",
                        terminal=True,
                        http_status=page.status_code,
                    ) from exc
                exc.http_status = page.status_code
                raise
            result.http_status = page.status_code
            result.elapsed_seconds = time.perf_counter() - started
            return result
        except ExtractionError as exc:
            last_error = exc
            if exc.terminal or attempt >= HTTP_ATTEMPTS - 1:
                break
            pause = min(30.0, (2 ** attempt) + random.uniform(0.2, 1.0))
            LOG.warning("video_id=%s reason=%s retry=%s/%s sleep=%.1fs",
                        video_id, exc.code, attempt + 1, HTTP_ATTEMPTS - 1, pause)
            await asyncio.sleep(pause)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_error = ExtractionError("network_failure", str(exc)[:800])
            if attempt >= HTTP_ATTEMPTS - 1:
                break
            await asyncio.sleep(min(30.0, (2 ** attempt) + random.uniform(0.2, 1.0)))
        except Exception as exc:
            last_error = ExtractionError("unexpected_error", f"{type(exc).__name__}: {exc}")
            break

    assert last_error is not None
    if last_error.http_status is None and isinstance(last_error, ExtractionError):
        last_error.http_status = None
    return _failure_result(video_id, page_url, last_error, time.perf_counter() - started)


def _as_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _is_empty_db_value(column: str, value: Any) -> bool:
    if value is None:
        return True
    return column in URL_COLUMNS and isinstance(value, str) and not value.strip()


def prepare_result_for_row(row: dict[str, Any], result: ExtractedMedia, refresh_existing: bool) -> tuple[dict[str, Any], dict[str, str], list[str], str]:
    """Only persist host metadata when its matching path is (or will be) stored."""
    fields_to_write: dict[str, Any] = {}
    hosts_to_store: dict[str, str] = {}
    effective: dict[str, Any] = {}
    for column in URL_COLUMNS:
        new_value = result.fields.get(column)
        old_value = row.get(column)
        use_new = new_value is not None and (refresh_existing or _is_empty_db_value(column, old_value) or old_value == new_value)
        if use_new:
            fields_to_write[column] = new_value
            effective[column] = new_value
            host = result.hosts.get(column)
            if host:
                hosts_to_store[column] = host
        else:
            effective[column] = old_value

    new_thumb_id = result.fields.get("thumb_id")
    old_thumb_id = row.get("thumb_id")
    if new_thumb_id is not None and (refresh_existing or old_thumb_id is None or old_thumb_id == new_thumb_id):
        fields_to_write["thumb_id"] = new_thumb_id
        effective["thumb_id"] = new_thumb_id
    else:
        effective["thumb_id"] = old_thumb_id

    # CDN version is meaningful only for the trailer URL that is actually stored.
    new_trailer = result.fields.get("trailer_path")
    stored_trailer = effective.get("trailer_path")
    can_use_cdn = new_trailer is not None and stored_trailer == new_trailer
    new_version = result.fields.get("trailer_cdn_version") if can_use_cdn else None
    old_version = row.get("trailer_cdn_version")
    if new_version is not None and (refresh_existing or old_version is None or old_version == new_version):
        fields_to_write["trailer_cdn_version"] = new_version
        effective["trailer_cdn_version"] = new_version
    else:
        effective["trailer_cdn_version"] = old_version

    missing = [column for column in ALL_MEDIA_COLUMNS if _is_empty_db_value(column, effective.get(column))]
    if not any(result.fields.get(column) is not None for column in URL_COLUMNS):
        status = result.status if result.status != "partial" else "no_media"
    elif not missing:
        status = "complete"
    else:
        status = "partial"
    return fields_to_write, hosts_to_store, missing, status


def _selection_where(*, dry_run: bool, refresh_existing: bool, video_id: int | None) -> tuple[str, list[Any]]:
    if video_id is not None:
        return "v.id = %s", [video_id]
    if refresh_existing:
        missing = "TRUE"
    else:
        missing = "(" + " OR ".join(
            [f"NULLIF(v.{column}, '') IS NULL" for column in URL_COLUMNS]
            + ["v.thumb_id IS NULL", "v.trailer_cdn_version IS NULL"]
        ) + ")"
    where = [missing]
    params: list[Any] = []
    if not dry_run and not refresh_existing:
        where.append(
            "(e.video_id IS NULL OR "
            "(e.status = 'retry' AND e.attempts < %s AND e.next_retry_at <= now()))"
        )
        params.append(MAX_RECORD_ATTEMPTS)
    return " AND ".join(where), params


def count_eligible(db: psycopg.Connection, *, dry_run: bool, refresh_existing: bool, video_id: int | None) -> int:
    where, params = _selection_where(dry_run=dry_run, refresh_existing=refresh_existing, video_id=video_id)
    join = "" if dry_run else "LEFT JOIN public.video_media_enrichment e ON e.video_id = v.id"
    query = f"SELECT count(*) FROM public.videos v {join} WHERE {where}"
    return int(db.execute(query, params).fetchone()[0])


def fetch_batch(
    db: psycopg.Connection,
    *,
    last_id: int,
    batch_size: int,
    dry_run: bool,
    refresh_existing: bool,
    video_id: int | None,
) -> list[dict[str, Any]]:
    where, params = _selection_where(dry_run=dry_run, refresh_existing=refresh_existing, video_id=video_id)
    join = "" if dry_run else "LEFT JOIN public.video_media_enrichment e ON e.video_id = v.id"
    tracker_select = "0::int AS attempts" if dry_run else "COALESCE(e.attempts, 0)::int AS attempts"
    cursor_clause = "" if video_id is not None else " AND v.id > %s"
    if video_id is None:
        params.append(last_id)
    params.append(batch_size)
    columns = ", ".join(f"v.{column}" for column in ALL_MEDIA_COLUMNS)
    query = f"""
        SELECT v.id, v.page_path, v.title, {columns}, {tracker_select}
        FROM public.videos v
        {join}
        WHERE {where}{cursor_clause}
        ORDER BY v.id
        LIMIT %s
    """
    raw_rows = db.execute(query, params).fetchall()
    out = []
    for values in raw_rows:
        # id, page_path, title, 8 media columns, tracker attempts
        item = {"id": int(values[0]), "page_path": values[1], "title": values[2]}
        for index, column in enumerate(ALL_MEDIA_COLUMNS, start=3):
            item[column] = values[index]
        item["attempts"] = int(values[3 + len(ALL_MEDIA_COLUMNS)])
        out.append(item)
    return out


def _retry_delay_minutes(attempt: int) -> int:
    return min(720, 5 * (2 ** max(0, attempt - 1)))


def _tracker_status(result: ExtractedMedia, previous_attempts: int, missing_fields: list[str], row_status: str) -> tuple[str, int, int | None]:
    if row_status in ("complete", "partial", "no_media"):
        return row_status, 0, None
    if result.status == "inaccessible" or result.reason_code in ("id_mismatch", "primary_model_missing", "invalid_page_url", "invalid_page_path", "redirect_outside_source", "source_restricted", "not_found"):
        return "inaccessible", min(previous_attempts + 1, 32767), None
    next_attempt = previous_attempts + 1
    if next_attempt >= MAX_RECORD_ATTEMPTS:
        return "exhausted", next_attempt, None
    return "retry", next_attempt, _retry_delay_minutes(next_attempt)


def _database_update(db: psycopg.Connection, row_updates: list[tuple[int, dict[str, Any]]], refresh_existing: bool) -> int:
    if not row_updates:
        return 0
    source_columns = ("id",) + ALL_MEDIA_COLUMNS
    cast_by_column = {
        "id": "BIGINT",
        "thumbnail_path": "TEXT",
        "image_path": "TEXT",
        "preview_thumb_path": "TEXT",
        "sprite_path": "TEXT",
        "trailer_path": "TEXT",
        "trailer_fallback_path": "TEXT",
        "thumb_id": "BIGINT",
        "trailer_cdn_version": "SMALLINT",
    }
    values_sql = []
    params: list[Any] = []
    for video_id, values in row_updates:
        expressions = []
        for column in source_columns:
            expressions.append("%s::" + cast_by_column[column])
            params.append(video_id if column == "id" else values.get(column))
        values_sql.append("(" + ", ".join(expressions) + ")")
    src_columns = ", ".join(source_columns)
    assignments = []
    for column in ALL_MEDIA_COLUMNS:
        if column in URL_COLUMNS:
            if refresh_existing:
                expr = f"COALESCE(src.{column}, NULLIF(v.{column}, ''), v.{column})"
            else:
                expr = f"COALESCE(NULLIF(v.{column}, ''), src.{column}, v.{column})"
        else:
            expr = f"COALESCE(src.{column}, v.{column})" if not refresh_existing else f"COALESCE(src.{column}, v.{column})"
        assignments.append(f"{column} = {expr}")
    sql = f"""
        UPDATE public.videos AS v
        SET {", ".join(assignments)}
        FROM (VALUES {", ".join(values_sql)}) AS src({src_columns})
        WHERE v.id = src.id
    """
    with db.transaction():
        cursor = db.execute(sql, params)
        return cursor.rowcount


def _write_tracker(db: psycopg.Connection, records: list[tuple[dict[str, Any], ExtractedMedia, str, int, int | None, list[str], dict[str, str]]]) -> None:
    """Upsert every batch's tracking data with one parameterized SQL statement."""
    if not records:
        return
    values_sql: list[str] = []
    params: list[Any] = []
    for row, result, status, attempts, retry_minutes, missing, hosts in records:
        values_sql.append(
            "(%s::BIGINT, %s::TEXT, %s::SMALLINT, %s::INT, %s::INT, "
            "%s::TEXT, %s::TEXT, %s::TEXT[], %s::JSONB)"
        )
        last_error = (result.error or result.reason_code or "")[:900] or None
        params.extend([
            row["id"], status, attempts, retry_minutes, result.http_status,
            result.reason_code, last_error, missing, Jsonb(hosts),
        ])
    sql = f"""
        INSERT INTO public.video_media_enrichment
            (video_id, status, attempts, next_retry_at, last_attempt_at,
             last_http_status, reason_code, last_error, missing_fields, media_hosts, updated_at)
        SELECT src.video_id, src.status, src.attempts,
               CASE WHEN src.retry_minutes IS NULL THEN NULL
                    ELSE now() + (src.retry_minutes * interval '1 minute') END,
               now(), src.last_http_status, src.reason_code, src.last_error,
               src.missing_fields, src.media_hosts, now()
        FROM (VALUES {", ".join(values_sql)}) AS src(
            video_id, status, attempts, retry_minutes, last_http_status,
            reason_code, last_error, missing_fields, media_hosts
        )
        ON CONFLICT (video_id) DO UPDATE SET
            status = EXCLUDED.status,
            attempts = EXCLUDED.attempts,
            next_retry_at = EXCLUDED.next_retry_at,
            last_attempt_at = EXCLUDED.last_attempt_at,
            last_http_status = EXCLUDED.last_http_status,
            reason_code = EXCLUDED.reason_code,
            last_error = EXCLUDED.last_error,
            missing_fields = EXCLUDED.missing_fields,
            media_hosts = COALESCE(public.video_media_enrichment.media_hosts, '{}'::jsonb) || EXCLUDED.media_hosts,
            updated_at = now()
    """
    db.execute(sql, params)

async def _process_rows(
    rows: list[dict[str, Any]],
    client: httpx.AsyncClient,
    concurrency: int,
    min_interval: float,
    limiter: AdaptiveLimiter | None = None,
    pacer: HostPacer | None = None,
) -> list[ExtractedMedia]:
    limiter = limiter or AdaptiveLimiter(concurrency)
    pacer = pacer or HostPacer(min_interval)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=max(1, concurrency * 2))
    results: dict[int, ExtractedMedia] = {}

    async def worker() -> None:
        while True:
            row = await queue.get()
            try:
                if row is None:
                    return
                results[row["id"]] = await extract_record(client, row, limiter, pacer)
            finally:
                queue.task_done()

    worker_count = min(max(1, concurrency), len(rows))
    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    for row in rows:
        await queue.put(row)
    for _ in workers:
        await queue.put(None)
    await queue.join()
    await asyncio.gather(*workers)
    return [results[row["id"]] for row in rows]


def _diagnostic_payload(row: dict[str, Any], result: ExtractedMedia, refresh_existing: bool) -> dict[str, Any]:
    fields_to_write, hosts_to_store, missing, status = prepare_result_for_row(row, result, refresh_existing)
    return {
        "video_id": result.video_id,
        "title": row.get("title"),
        "requested_page_path": row.get("page_path"),
        "resolved_primary_page_url": result.page_url,
        "status": status,
        "reason_code": result.reason_code,
        "http_status": result.http_status,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "extracted_fields": result.fields,
        "normalized_paths_to_write": fields_to_write,
        "source_keys": result.source_keys,
        "hosts_to_persist_for_written_paths": hosts_to_store,
        "rejected_fields": result.rejected_fields,
        "missing_after_extraction": missing,
        "error": result.error,
    }


async def run(args: argparse.Namespace) -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing; configure the GitHub Actions secret without printing it")
    if args.video_id is not None and args.video_id < 0:
        raise ValueError("--video-id must be a non-negative integer")
    if args.batch_size < 1 or args.batch_size > 5000:
        raise ValueError("--batch-size must be between 1 and 5000")
    if args.concurrency < 1 or args.concurrency > 100:
        raise ValueError("--concurrency must be between 1 and 100")
    if args.max_videos < 0:
        raise ValueError("--max-videos must be >= 0")
    if not args.dry_run and args.refresh_existing:
        LOG.warning("Refresh mode enabled: validated non-empty media paths can be replaced.")

    LOG.info("Media Enrichment Started")
    LOG.info("Batch size: %d; concurrency: %d; max videos: %s; dry run: %s; refresh existing: %s",
             args.batch_size, args.concurrency, "unlimited" if args.max_videos == 0 else args.max_videos,
             args.dry_run, args.refresh_existing)

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.7",
    }

    stats = {
        "processed": 0, "complete": 0, "partial": 0, "failed": 0,
        "no_media": 0, "inaccessible": 0, "database_rows_updated": 0,
        "retry_scheduled": 0, "exhausted": 0,
    }
    field_counts = {column: 0 for column in URL_COLUMNS}
    field_counts.update({"thumb_id": 0, "trailer_cdn_version": 0})
    start = time.perf_counter()
    with psycopg.connect(database_url, autocommit=True) as db:
        if not args.dry_run:
            db.execute(TRACKER_DDL, prepare=False)
        total = count_eligible(db, dry_run=args.dry_run, refresh_existing=args.refresh_existing, video_id=args.video_id)
        if args.max_videos:
            total = min(total, args.max_videos)
        LOG.info("Total eligible videos: %d", total)
        if total == 0:
            if args.video_id is not None:
                LOG.error("Video ID %s was not found in public.videos or is not eligible.", args.video_id)
                return 2
            LOG.info("No eligible videos.")
            return 0

        async with httpx.AsyncClient(
            timeout=timeout, limits=limits, headers=headers, follow_redirects=False
        ) as client:
            # Keep host pacing and adaptive concurrency state across database batches.
            limiter = AdaptiveLimiter(args.concurrency)
            pacer = HostPacer(args.min_request_interval)
            last_id = -1
            while stats["processed"] < total:
                remaining = total - stats["processed"]
                rows = fetch_batch(
                    db,
                    last_id=last_id,
                    batch_size=min(args.batch_size, remaining),
                    dry_run=args.dry_run,
                    refresh_existing=args.refresh_existing,
                    video_id=args.video_id,
                )
                if not rows:
                    break
                if args.video_id is None:
                    last_id = rows[-1]["id"]
                results = await _process_rows(rows, client, args.concurrency, args.min_request_interval, limiter, pacer)
    
                update_rows: list[tuple[int, dict[str, Any]]] = []
                tracker_records = []
                for row, result in zip(rows, results):
                    result.attempts_before = int(row.get("attempts") or 0)
                    stats["processed"] += 1
                    if result.status == "complete":
                        stats["complete"] += 1
                    elif result.status == "partial":
                        stats["partial"] += 1
                    elif result.status == "no_media":
                        stats["no_media"] += 1
                    else:
                        stats["failed"] += 1
                    if result.status == "inaccessible":
                        stats["inaccessible"] += 1
                    if args.dry_run and args.video_id is not None:
                        print(json.dumps(_diagnostic_payload(row, result, args.refresh_existing), indent=2, ensure_ascii=False))
                    elif args.dry_run:
                        LOG.info("DRY RUN video_id=%s result=%s extracted=%d reason=%s",
                                 result.video_id, result.status,
                                 sum(result.fields.get(c) is not None for c in ALL_MEDIA_COLUMNS),
                                 result.reason_code)
    
                    for column in ALL_MEDIA_COLUMNS:
                        if result.fields.get(column) is not None:
                            field_counts[column] += 1
    
                    fields_to_write, hosts_to_store, missing, row_status = prepare_result_for_row(
                        row, result, args.refresh_existing
                    )
                    if not args.dry_run:
                        if fields_to_write:
                            update_rows.append((row["id"], fields_to_write))
                        tracker_status, attempts, retry_minutes = _tracker_status(
                            result, result.attempts_before, missing, row_status
                        )
                        if tracker_status == "retry":
                            stats["retry_scheduled"] += 1
                        if tracker_status == "exhausted":
                            stats["exhausted"] += 1
                        tracker_records.append((
                            row, result, tracker_status, attempts, retry_minutes,
                            missing, hosts_to_store,
                        ))
    
                if not args.dry_run:
                    # Keep video values and their retry/host checkpoint atomic.
                    with db.transaction():
                        stats["database_rows_updated"] += _database_update(db, update_rows, args.refresh_existing)
                        _write_tracker(db, tracker_records)
    
                elapsed = max(0.001, time.perf_counter() - start)
                LOG.info(
                    "Processed %d/%d | complete=%d partial=%d failed=%d no_media=%d updated_rows=%d throughput=%.2f videos/s",
                    stats["processed"], total, stats["complete"], stats["partial"], stats["failed"],
                    stats["no_media"], stats["database_rows_updated"], stats["processed"] / elapsed,
                )
                if args.video_id is not None:
                    break
    
    elapsed = max(0.001, time.perf_counter() - start)
    LOG.info("Media Enrichment Finished")
    LOG.info("Processed: %d; full success: %d; partial: %d; failed: %d; no media: %d",
             stats["processed"], stats["complete"], stats["partial"], stats["failed"], stats["no_media"])
    for column, count in field_counts.items():
        LOG.info("%s extracted: %d", column, count)
    LOG.info("Database rows updated: %d; retries scheduled: %d; exhausted: %d",
             stats["database_rows_updated"], stats["retry_scheduled"], stats["exhausted"])
    LOG.info("Average extraction time: %.3fs/video; throughput: %.2f videos/s",
             elapsed / max(1, stats["processed"]), stats["processed"] / elapsed)
    return 0 if stats["failed"] == 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("BATCH_SIZE", "500")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "20")))
    parser.add_argument("--max-videos", type=int, default=int(os.getenv("MAX_VIDEOS", "0")),
                        help="0 means no application-level limit")
    parser.add_argument("--video-id", type=int, default=None,
                        help="fetch and print diagnostics for one public.videos.id")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="extract and report without DB writes (default)")
    parser.add_argument("--apply", action="store_true",
                        help="persist validated media paths and tracking status to Neon")
    parser.add_argument("--refresh-existing", action="store_true", default=False,
                        help="replace populated paths only when a new validated value is available")
    parser.add_argument("--min-request-interval", type=float,
                        default=float(os.getenv("MEDIA_MIN_REQUEST_INTERVAL", "0.15")),
                        help="minimum seconds between source-host request starts")
    args = parser.parse_args(argv)
    if args.apply:
        args.dry_run = False
    if args.min_request_interval < 0 or args.min_request_interval > 10:
        parser.error("--min-request-interval must be between 0 and 10 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Completed batches are committed; rerun to resume.")
        return 130
    except Exception as exc:
        LOG.exception("Media enrichment failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
