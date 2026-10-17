# Flask catalog website

The site runs separately from the scraper but shares its PostgreSQL catalog.

## Run locally

Install the website dependencies and start Flask:

```bash
pip install -r site-requirements.txt
flask --app app run
```

For deployment on Vercel, the Python builder installs dependencies from the repository-root `requirements.txt`. The website dependencies are included there as well.

## Environment variables

- `DATABASE_URL`: Neon PostgreSQL connection URL. Required.
- `SITE_NAME`: site brand, default `Video Atlas`.
- `SITE_URL`: public canonical base URL, without a trailing slash.
- `SOURCE_URL`: original publisher base URL, default `https://mobilexh.live`.
- `THUMBNAIL_ORIGIN`: verified image origin compatible with the paths stored in the database. Thumbnail paths were harvested from several image CDN domains, so do not blindly reuse the video playback domain.
- `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`: optional cache. Pages still work without Redis.

## Authorized playback

The site does not extract streams from a page view. By default, the video page links visitors to the original publisher. For media you are authorized to serve, you may configure `AUTHORIZED_STREAM_RESOLVER_URL`, `AUTHORIZED_STREAM_RESOLVER_TOKEN`, and `ALLOWED_STREAM_HOSTS` (comma-separated hostnames). The resolver must return JSON with an `hls_url` value on HTTPS and a host in that allowlist. Do not reuse expired signatures or attempt to circumvent access controls; respect copyright and distribution rights.

## Database indexes

Run `siteapp/schema_indexes.sql` once with a database administration client that can create indexes concurrently. Do not run concurrent index builds inside Flask request handlers.

## SEO and scale notes

Canonical URLs, category metadata, robots.txt, a category sitemap, and accessible pagination are included. Video pages are intentionally not all enumerated in one sitemap; before indexing at large scale, add segmented video sitemaps and an incremental sitemap manifest. Pagination uses LIMIT/OFFSET for modest page numbers; use indexed keyset/cursor pagination before enabling deep page-number navigation. Consider CDN edge caching after launch.
