# Category scraper

1. Add repository Actions secret `DATABASE_URL` (Neon PostgreSQL connection URL).
2. Run **Test scraper** first; enable `live` to fetch one listing page read-only.
3. Run **Scrape one category** with blank category and chain=true. Each successful run dispatches the next category.
4. If a run fails, rerun it: pages already stored in Neon remain checkpointed.

The source categories come from `category_list.json`. The scraper uses 3 concurrent HTTP requests, delay, exponential retry, and 30 pages per category; empty responses and HTTP 403/429 abort rather than being counted successful. GitHub Actions may time out at 350 minutes: rerun to resume. A category is completed only after **all 30 pages** succeed.

Tables: `categories` (category name/slug/status), `videos` (one row per video ID), `video_categories` (many-to-many), `scrape_pages` (checkpoints). All video and creator URLs are saved as paths without hostnames. Only the thumbnail path is stored, without a domain, as requested. Note that upstream CDN paths may require their original host or signed tokens to work; the frontend must provide a valid image origin. SEO metadata can later be added to `categories` per slug.

The workflow requires Actions write permission for self-dispatch; set the repository workflow token to read/write in Actions settings as necessary. Check source terms and access permissions before large crawls; no anti-bot bypass is included. This only indexes listing metadata, not video files.

SQL status: `SELECT slug,status,last_error FROM categories ORDER BY sort_order;`

## Recovery from a 404 and database durability

Connections use `autocommit=True`; every `save()` block is a real independent transaction. Successful pages survive later failures or runner termination. An HTTP 404 after a shorter final page is treated as verified pagination end only if **all preceding pages have successful checkpoints** and the preceding page has more items than the final page. `categories.terminal_page` records the actual end. A 404 by itself is not sufficient and does not mark the category completed.

Verify progress in Neon SQL Editor:

```sql
SELECT count(*) FROM videos;
SELECT category_slug,count(*) AS saved_pages,max(page) AS last_page FROM scrape_pages GROUP BY category_slug;
SELECT slug,status,terminal_page,last_error FROM categories ORDER BY sort_order LIMIT 15;
```

If a previous run used the older non-autocommit scraper and crashed, its entire transaction may have rolled back. Rerun on the latest main branch to repopulate Neon safely.

## Failed response diagnostics and queue recovery

When a page returns HTTP 200 with no extractable listing records, or HTTP 403/429, the scraper saves the received HTML to `diagnostics/<category>/<page>-<UTC timestamp>.html` plus a JSON metadata report alongside it. The production workflow now directly commits these files into the repository's `diagnostics/` folder using `git add diagnostics/`, `git commit`, and `git push`, even if scraping fails. No GitHub Actions artifacts are created. Browse `diagnostics/<category>/` in GitHub after the run; this requires Actions `contents: write` permissions. GitHub repository history will retain prior diagnostic HTML, so delete unneeded diagnostics periodically. Responses can contain cookies, identifiers, or other private information; do not publish raw diagnostics.

A category encountering missing listing data is labeled `blocked` rather than `completed`. The automatic queue skips blocked categories and can continue to the next pending one. Use the explicit `category` workflow input to retry a blocked category after investigating the HTML. Previously committed successful pages are preserved. If the source actually requires age verification or denies automated access, no automatic bypass is attempted.

Review blocked categories:
```sql
SELECT slug,status,last_error FROM categories WHERE status='blocked' ORDER BY sort_order;
```

## Continuous queue without runner cooldown

Start **Scrape one category** using blank category and `chain=true`. After the selected category run finishes, the workflow checks Neon for remaining eligible categories and dispatches the next workflow immediately. This avoids spending Actions minutes on an idle 15-minute sleep. The scraper still uses its configured per-request delay (`REQUEST_DELAY: '1.5'`) and three concurrent workers. If a category fails and is marked `blocked`, the queue skips it and continues to the next eligible category, preserving successful page checkpoints. When no pending/running/failed categories remain, chaining stops automatically; blocked categories require an explicit manual retry. Set `chain=false` to run just one category.

**Tradeoff:** There is no longer an extra 15-minute pause between categories. If the source starts returning HTTP 403/429 or other access errors, stop the chain or increase `REQUEST_DELAY` before retrying.


## Video catalog website

The Flask site is in `app.py` and `siteapp/`. Deployment configuration for Vercel is in `vercel.json`; configure the environment variables described in [SITE_README.md](SITE_README.md) before deploying. Vercel installs dependencies from the repository-root `requirements.txt`, so keep the website dependencies listed there. The separate `site-requirements.txt` is available for a smaller standalone website environment.

The site reads the existing Neon catalog populated by the scraper. It does not download or host video files. By default, video pages link to the original publisher; playback through an HLS resolver requires an explicitly configured, authorized resolver and allowlisted hosts. Set a verified `THUMBNAIL_ORIGIN` that matches the stored image paths; do not assume all thumbnails share one CDN.


## Video media enrichment

A separate manually triggered workflow enriches media metadata for existing public.videos rows. It does not replace or run the category scraper.

1. Open **Actions → Enrich video media → Run workflow**.
2. Start with dry_run=true, video_id=1193895, and max_videos=1. The diagnostic output shows the main model ID, source JSON keys, normalized paths, source hosts, rejected fields, and any errors. This is read-only against public.videos and does not create the tracker table.
3. Run a small representative dry-run batch before enabling writes. For a real update, set dry_run=false. Leave refresh_existing=false unless you intentionally want valid extracted paths to replace already-populated paths.
4. Default inputs are batch size 500, concurrency 20, maximum videos 0 (no application-level limit), dry run enabled, and refresh disabled. These are starting points, not measured throughput guarantees.

The worker reads DATABASE_URL from the existing GitHub Actions secret and uses the existing httpx, beautifulsoup4, and psycopg dependencies. It verifies videoModel.id against the database ID and checks the media directory before storing a path. It only extracts from the primary model or an object whose ID and canonical page URL match that model; it does not borrow assets from recommended videos. It does not download image/video files, modify access_status, change unrelated video columns, bypass age gates, or retry access-restricted pages indefinitely.

### Retry checkpoint and CDN host preservation

The first non-dry-run run creates one small sidecar table, public.video_media_enrichment, used to record retry status and per-field CDN hostnames without changing public.videos. Validated paths are written in batches to the existing table; a new null/empty value never overwrites existing data. Retryable failures use bounded attempts and backoff. Successful partial results and valid pages with optional media missing are checkpointed to prevent endless refetching.

**CDN hostname caveat:** the catalog stores URL paths without domains, and hosts can differ between the thumbnail, image, preview, sprite, and trailer. The sidecar's media_hosts JSON stores the exact source host per media field for URLs that this worker actually processes. Older paths created by the category scraper do not have verified host metadata in that sidecar; do not assume every pre-existing path can be reconstructed from trailer_cdn_version alone. That column only captures a numeric thumb-vN shard for the validated trailer host when one is present. If the frontend needs to construct working URLs for every asset, join public.video_media_enrichment.media_hosts by video_id and use a verified per-field hostname; do not guess a common CDN origin.

Inspect worker results and retry state with this SQL:

    SELECT status, count(*) FROM public.video_media_enrichment GROUP BY status ORDER BY status;
    SELECT video_id, status, attempts, next_retry_at, reason_code, missing_fields
    FROM public.video_media_enrichment
    ORDER BY updated_at DESC
    LIMIT 50;

The manual workflow runs Python compile and regression tests before making requests. A safe rollout is: run the single-video diagnostic, review the output, run a small dry-run batch, then deliberately enable database writes.
