import json
import logging
import os

import httpx

log = logging.getLogger(__name__)
URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")


def get(key):
    if not URL or not TOKEN:
        return None
    try:
        r = httpx.get(URL + "/get/" + key,
                      headers={"Authorization": "Bearer " + TOKEN}, timeout=0.65)
        r.raise_for_status()
        value = r.json().get("result")
        return json.loads(value) if value else None
    except Exception:
        log.warning("Redis unavailable: proceeding without cache")
        return None


def set(key, value, ttl=300):
    if not URL or not TOKEN:
        return
    try:
        httpx.post(URL + "/pipeline",
                   headers={"Authorization": "Bearer " + TOKEN},
                   json=[["SET", key, json.dumps(value, default=str), "EX", str(ttl)]],
                   timeout=0.65)
    except Exception:
        log.warning("Redis cache write failed")
