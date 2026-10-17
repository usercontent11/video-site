"""No stream extraction is done on a page view. Supply authorized playback URLs through a resolver."""
import os
from urllib.parse import urlsplit

import httpx


def resolve(video):
    endpoint = os.getenv("AUTHORIZED_STREAM_RESOLVER_URL")
    token = os.getenv("AUTHORIZED_STREAM_RESOLVER_TOKEN")
    if not endpoint or not token:
        return None
    try:
        r = httpx.post(
            endpoint,
            json={"video_id": video["id"], "video_path": video["video_path"]},
            headers={"Authorization": "Bearer " + token},
            timeout=3.0,
        )
        r.raise_for_status()
        data = r.json()
        url = data.get("hls_url")
        parsed = urlsplit(url or "")
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        allowed = {
            h.strip().lower()
            for h in os.getenv("ALLOWED_STREAM_HOSTS", "").split(",")
            if h.strip()
        }
        if parsed.hostname.lower() not in allowed:
            return None
        return url
    except Exception:
        return None
