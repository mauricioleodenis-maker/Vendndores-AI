# Revisión Haiku: plans-niches

Review complete, but I did not write `docs/revision/haiku-scraping.md`, because the reviewer rules prohibit report .md files; the findings are below instead.
High: `app/factory/service.py:321` calls `_scrape`, and its broad `except` at line 144 does not roll back. A concurrent `IntegrityError` on `uq_kb_documents_tenant_hash` (`app/scraping/service.py:249-250`) leaves the session unusable, so the later `flush` at line 369 fails. Medium: `registrable_domain` (`crawler.py:50-56`) has no public-suffix list, so `*.wixsite.com` or `*.github.io` sites crawl each other's pages. Also medium: `robots.py:169-174` fails open on 5xx and fetch errors, and `extractor.py:186` escapes `</untrusted_source` case-sensitively only, unlike `_INJECTION` at line 47.
Low: `store_pages` runs one SELECT per page (N+1, capped at 20), `_PHONE`/`_PRICE` are noisy, and `templates/scraping/` is empty so there is no UI to review. Tests lack coverage for robots 5xx, the store race, and the case-variant delimiter.
Verified sound: `safe_fetch` SSRF controls, offsite-redirect discard, the 20-page cap, and the Instagram domain gate.
