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

## Continuous queue with completion-based cooldown

Start **Scrape one category** using blank category and `chain=true`. After the selected category run finishes, the workflow checks Neon for remaining eligible categories. If at least one remains, it waits **900 seconds (15 minutes)** and dispatches the next workflow. The interval begins **after scraping and diagnostic commits finish**, not on a wall-clock 15-minute cron schedule. If a category fails and is marked `blocked`, the queue continues to the next eligible category, preserving all successful per-page commits. When there are no remaining pending/running/failed categories, chaining stops automatically; blocked categories require an explicit manual retry. Set `chain=false` to run just one category.

**Tradeoff:** This straightforward cooldown holds a GitHub-hosted runner for 15 minutes between categories and counts toward GitHub Actions usage. If many categories are queued, replace it with an external delayed dispatcher to avoid runner-time costs. The cooling-down workflow will show as active until the 15 minutes have elapsed. No scheduled cron is used.
