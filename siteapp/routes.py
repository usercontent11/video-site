import re
from flask import Blueprint, current_app, abort, render_template, request, Response, url_for
from . import catalog
from .playback import resolve

bp = Blueprint("site", __name__)


def page_number():
    try:
        return min(max(int(request.args.get("page", "1")), 1), 10000)
    except ValueError:
        return 1


def with_media(v):
    item = dict(v)
    p = item.get("thumbnail_path") or ""
    origin = current_app.config["THUMBNAIL_ORIGIN"]
    # Thumbnail paths originate from multiple source CDNs; configure a compatible image origin.
    item["thumbnail_url"] = origin + p if origin and p.startswith("/") else ""
    return item


@bp.app_context_processor
def common():
    return {
        "site_name": current_app.config["SITE_NAME"],
        "base_url": current_app.config["SITE_URL"],
    }


@bp.get("/")
def index():
    page = page_number()
    data = [with_media(x) for x in catalog.home(page, 24)]
    return render_template(
        "index.html",
        videos=data,
        page=page,
        canonical=current_app.config["SITE_URL"] + url_for("site.index"),
        title="Explore videos",
    )


@bp.get("/category/<slug>")
def category(slug):
    if not re.fullmatch(r"[a-z0-9-]{1,120}", slug):
        abort(404)
    cats = catalog.categories()
    matched = next((x for x in cats if x["slug"] == slug), None)
    if not matched:
        abort(404)
    page = page_number()
    data = [with_media(x) for x in catalog.category(slug, page, 24)]
    return render_template(
        "category.html",
        category=matched,
        videos=data,
        page=page,
        canonical=current_app.config["SITE_URL"] + url_for("site.category", slug=slug),
        title=matched["name"] + " videos",
    )


@bp.get("/video/<vid>")
def video(vid):
    if not re.fullmatch(r"[0-9]{1,24}", vid):
        abort(404)
    v = catalog.detail(vid)
    if not v:
        abort(404)
    v = with_media(v)
    cats = catalog.video_categories(vid)
    related = [with_media(x) for x in catalog.related(vid)]
    stream = resolve(v)
    source = current_app.config["SOURCE_URL"] + v["video_path"]
    return render_template(
        "video.html",
        video=v,
        categories=cats,
        related=related,
        stream=stream,
        source=source,
        canonical=current_app.config["SITE_URL"] + url_for("site.video", vid=vid),
        title=v["title"],
    )


@bp.get("/robots.txt")
def robots():
    base = current_app.config["SITE_URL"]
    return Response(
        f"User-agent: *\nAllow: /\nDisallow: /healthz\nSitemap: {base}/sitemap.xml\n",
        mimetype="text/plain",
    )


@bp.get("/sitemap.xml")
def sitemap():
    base = current_app.config["SITE_URL"]
    urls = [base + "/"] + [
        base + url_for("site.category", slug=c["slug"]) for c in catalog.categories()
    ]
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join("<url><loc>" + u + "</loc></url>" for u in urls)
        + "</urlset>"
    )
    return Response(
        xml,
        mimetype="application/xml",
        headers={"Cache-Control": "public,max-age=1800"},
    )


@bp.get("/api/categories")
def api_categories():
    return {"categories": catalog.categories()}


@bp.get("/healthz")
def health():
    return {"status": "ok"}
