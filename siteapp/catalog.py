from .db import connection
from . import cache


def fetch_cached(key, ttl, query, params=()):
    cached = cache.get(key)
    if cached is not None:
        return cached
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    cache.set(key, rows, ttl)
    return rows


def categories():
    return fetch_cached("categories:v1", 1800, """
      SELECT c.slug,c.name,c.status
      FROM categories c WHERE EXISTS
       (SELECT 1 FROM video_categories vc WHERE vc.category_slug=c.slug)
      ORDER BY c.sort_order LIMIT 800
    """)


def home(page, limit):
    offset = (page - 1) * limit
    return fetch_cached(f"home:v2:{page}", 240, """
      SELECT v.id,v.title,v.video_path,v.thumbnail_path,v.duration_seconds,v.views,v.creator_name
      FROM videos v ORDER BY v.id DESC LIMIT %s OFFSET %s
    """, (limit, offset))


def category(slug, page, limit):
    offset = (page - 1) * limit
    return fetch_cached(f"cat:v2:{slug}:{page}", 360, """
      SELECT v.id,v.title,v.video_path,v.thumbnail_path,v.duration_seconds,v.views,v.creator_name
      FROM video_categories vc JOIN videos v ON v.id=vc.video_id
      WHERE vc.category_slug=%s
      ORDER BY v.id DESC LIMIT %s OFFSET %s
    """, (slug, limit, offset))


def detail(vid):
    rows = fetch_cached(f"video:v2:{vid}", 600, """
      SELECT id,title,video_path,thumbnail_path,duration_seconds,views,creator_name,creator_path
      FROM videos WHERE id=%s LIMIT 1
    """, (vid,))
    return rows[0] if rows else None


def video_categories(vid):
    return fetch_cached(f"video-cats:v1:{vid}", 1200, """
      SELECT c.slug,c.name FROM video_categories vc JOIN categories c
      ON c.slug=vc.category_slug WHERE vc.video_id=%s ORDER BY c.sort_order LIMIT 24
    """, (vid,))


def related(vid, limit=12):
    return fetch_cached(f"related:v1:{vid}", 600, """
      SELECT DISTINCT ON (v.id) v.id,v.title,v.thumbnail_path,v.duration_seconds,v.views,
      v.creator_name FROM video_categories mine
      JOIN video_categories other ON other.category_slug=mine.category_slug
      JOIN videos v ON v.id=other.video_id
      WHERE mine.video_id=%s AND v.id<>%s
      ORDER BY v.id DESC LIMIT %s
    """, (vid, vid, limit))
