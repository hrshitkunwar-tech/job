"""
crawl_expansion_50.py — Phase 5: Scale from 14 → 50 tools
════════════════════════════════════════════════════════════════
Pulls top-50 priority-scored tools from the Convex prod DB,
crawls each tool's homepage + help/docs subdomain,
chunks + embeds content via Voyage-3,
and writes semantic docs back to Convex for Gate 1 & Gate 2.

Skips tools already in the qualified pipeline (Gate 2 passed).
Runs crawls concurrently in batches of 8.

Usage:
    python crawl_expansion_50.py              # Top 50 by priority score
    python crawl_expansion_50.py --tier T1    # T1 public-only (no auth)
    python crawl_expansion_50.py --limit 20   # Smaller run for testing
    python crawl_expansion_50.py --dry-run    # Print queue without crawling
"""

import asyncio
import os
import re
import json
import hashlib
import time
import sys
import argparse
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from convex import ConvexClient
from dotenv import load_dotenv

load_dotenv()

# ─── Clients ──────────────────────────────────────────────────────────────────
CONVEX_PROD  = "https://majestic-whale-830.convex.cloud"
VOYAGE_KEY   = os.getenv("VOYAGE_API_KEY", "")

client = ConvexClient(CONVEX_PROD)

# ─── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--tier",    default=None,  help="T1, T2, or T3")
parser.add_argument("--limit",   default=50,    type=int)
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--batch",   default=8,     type=int)
args = parser.parse_args()

TIER_AUTH_MAP = {"T1": 1, "T2": 2, "T3": 3}
MAX_AUTH = TIER_AUTH_MAP.get(args.tier, 99) if args.tier else 99

# ─── Progress ──────────────────────────────────────────────────────────────────
stats = {
    "fetched": 0,
    "skipped_already_qualified": 0,
    "crawled": 0,
    "chunks": 0,
    "errors": 0,
    "elapsed_s": 0,
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    if not raw: return ""
    t = re.sub(r'<[^>]+>', ' ', raw)
    t = re.sub(r'!\[.*?\]\(.*?\)', '', t)
    t = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', t)
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'^#{1,6}\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', t)
    t = re.sub(r'```[\s\S]*?```', '', t)
    t = re.sub(r'[ \t]{2,}', ' ', t)
    lines = [l.strip() for l in t.split('\n') if len(l.strip()) > 20]
    return '\n'.join(lines).strip()

def chunk_text(text: str, max_chars: int = 600) -> list:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) > max_chars:
            if current: chunks.append(current.strip())
            current = s
        else:
            current += " " + s
    if current: chunks.append(current.strip())
    return [c for c in chunks if len(c) > 60]

def content_hash(tool: str, chunk: str) -> str:
    return hashlib.sha256(f"{tool}::{chunk[:500]}".encode()).hexdigest()

def get_embeddings_batch(texts: list) -> list:
    if not VOYAGE_KEY:
        return [[0.0] * 1024] * len(texts)
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {VOYAGE_KEY}"},
                json={"input": [t[:4000] for t in texts], "model": "voyage-3"},
                timeout=60,
            )
            if resp.status_code == 200:
                return [d['embedding'] for d in resp.json()['data']]
            if resp.status_code == 429:
                time.sleep(30 * (attempt + 1))
        except Exception as e:
            print(f"    ⚠️  Voyage error: {e}")
            time.sleep(2)
    return [[0.0] * 1024] * len(texts)

def infer_docs_url(base_url: str, name: str) -> str | None:
    """Guess help/docs URL from base URL pattern."""
    parsed = urlparse(base_url)
    domain = parsed.netloc.replace("www.", "")
    candidates = [
        f"https://help.{domain}",
        f"https://docs.{domain}",
        f"https://support.{domain}",
        f"{base_url.rstrip('/')}/help",
        f"{base_url.rstrip('/')}/docs",
    ]
    # Try the first candidate that resolves (quick HEAD check)
    for url in candidates:
        try:
            r = requests.head(url, timeout=4, allow_redirects=True)
            if r.status_code < 400:
                return url
        except Exception:
            continue
    return None

# ─── Already-qualified tool names (skip these) ─────────────────────────────
def get_qualified_tool_names() -> set:
    try:
        docs = client.query("ingest_v2:getPilotDocs", {"limit": 500})
        return {d['tool_name'].lower() for d in docs if d.get('tool_name')}
    except Exception:
        return set()

# ─── Crawl + Store ─────────────────────────────────────────────────────────
async def crawl_and_store(crawler, tool: dict) -> int:
    """Crawl a tool and write chunks to Convex. Returns number of chunks written."""
    name = tool['name']
    base_url = tool.get('base_url', f"https://{tool.get('domain', '')}")
    domain = tool.get('domain', urlparse(base_url).netloc)

    # Upsert tool record
    try:
        tool_id = client.mutation("ingest_v2:upsertTool", {
            "name": name, "domain": domain,
            "base_url": base_url, "status": "crawling",
        })
    except Exception as e:
        print(f"    ❌  upsertTool failed for {name}: {e}")
        return 0

    # Determine pages to crawl
    pages = [base_url]
    docs_url = infer_docs_url(base_url, name)
    if docs_url:
        pages.append(docs_url)
        print(f"    📚  Found docs: {docs_url}")

    total_chunks = 0
    for url in pages:
        try:
            cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=18000)
            result = await crawler.arun(url=url, config=cfg)

            client.mutation("ingest_v2:logCrawl", {
                "tool_id": tool_id, "url": url,
                "status": "success" if result.success else "failed",
                "crawled_at": int(time.time() * 1000),
            })

            if not result.success:
                print(f"    ⚠️   Crawl failed: {url} — {result.error_message}")
                continue

            clean = clean_text(result.markdown or "")
            if len(clean) < 100:
                print(f"    ⚠️   Too short after cleaning: {url}")
                continue

            chunks = chunk_text(clean)
            if not chunks:
                continue

            embs = get_embeddings_batch(chunks)

            for i, chunk in enumerate(chunks):
                ch_type = "steps" if ("how to" in chunk.lower() or "steps" in chunk.lower()) else \
                          "api" if ("api" in chunk.lower() or "endpoint" in chunk.lower()) else "content"
                client.mutation("ingest_v2:saveSemanticDoc", {
                    "tool_id": tool_id,
                    "tool_name": name,
                    "url": url,
                    "content_hash": content_hash(name, chunk),
                    "chunk_type": ch_type,
                    "content": chunk,
                    "embedding": embs[i],
                    "crawled_at": int(time.time() * 1000),
                })
            total_chunks += len(chunks)
            print(f"    ✅  {url} → {len(chunks)} chunks")

        except Exception as e:
            print(f"    ❌  Error crawling {url}: {str(e)[:80]}")
            stats["errors"] += 1

    # Mark tool active
    try:
        client.mutation("ingest_v2:upsertTool", {
            "name": name, "domain": domain,
            "base_url": base_url, "status": "active",
        })
    except Exception:
        pass

    return total_chunks

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    t0 = time.time()

    print("\n" + "═" * 60)
    print("🚀  Phase 5 — Crawl Expansion  (14 → 50 tools)")
    print(f"    Tier filter: {args.tier or 'all'}   Limit: {args.limit}   Batch: {args.batch}")
    print("═" * 60 + "\n")

    # Pull priority-ranked queue from Convex
    print("📋  Fetching priority queue from Convex...")
    all_tools = client.query("ingest_v2:getCrawlQueue", {"limit": 500})
    print(f"    → {len(all_tools)} tools in queue\n")

    # Filter by tier (auth_complexity)
    if args.tier:
        all_tools = [t for t in all_tools if (t.get("auth_complexity") or 99) <= MAX_AUTH]
        print(f"    After tier filter ({args.tier}): {len(all_tools)} tools")

    # Get already-qualified tools to skip
    qualified = get_qualified_tool_names()
    print(f"    Already qualified: {len(qualified)} tools (skipping)")

    # Skip already-qualified tools
    queue = []
    for t in all_tools:
        if t['name'].lower() in qualified:
            stats["skipped_already_qualified"] += 1
            continue
        queue.append(t)
        if len(queue) >= args.limit:
            break

    print(f"\n🎯  Crawl queue ({len(queue)} tools, sorted by priority score):")
    for i, t in enumerate(queue[:10]):
        tier_str = f"T{min(t.get('auth_complexity', 2), 4)}" if t.get('auth_complexity') else "T?"
        print(f"    {i+1:2}.  [{tier_str}] score={t.get('priority_score', '?'):2}  {t['name']}")
    if len(queue) > 10:
        print(f"    ... and {len(queue) - 10} more")

    if args.dry_run:
        print("\n⚠️   DRY RUN — not crawling. Remove --dry-run to execute.\n")
        return

    print(f"\n{'─' * 60}")
    print(f"  Starting crawl — {len(queue)} tools in batches of {args.batch}")
    print(f"{'─' * 60}\n")

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for i in range(0, len(queue), args.batch):
            batch = queue[i:i + args.batch]
            batch_num = (i // args.batch) + 1
            total_batches = (len(queue) + args.batch - 1) // args.batch
            print(f"\n{'─' * 40}")
            print(f"⚙️   Batch {batch_num}/{total_batches}  ({[t['name'] for t in batch]})")
            print(f"{'─' * 40}")

            async def do_tool(t):
                print(f"\n🔧  Crawling: {t['name']}  ({t.get('base_url', t.get('domain', '?'))})")
                n = await crawl_and_store(crawler, t)
                stats["crawled"] += 1
                stats["chunks"] += n
                print(f"    → {n} chunks stored  [total: {stats['chunks']}]")

            await asyncio.gather(*[do_tool(t) for t in batch])

    elapsed = time.time() - t0
    stats["elapsed_s"] = round(elapsed, 1)

    print(f"\n{'═' * 60}")
    print(f"📊  Phase 5 Crawl Complete")
    print(f"{'═' * 60}")
    print(f"  Tools crawled:          {stats['crawled']}")
    print(f"  Already qualified:      {stats['skipped_already_qualified']}")
    print(f"  Total chunks written:   {stats['chunks']}")
    print(f"  Errors:                 {stats['errors']}")
    print(f"  Elapsed:                {elapsed:.0f}s  ({elapsed/60:.1f}min)")
    print(f"\n  Next: Run Gate 1 → Gate 2 classifiers:")
    print(f"    python actionability_classifier_v1.py")
    print(f"    python executability_classifier.py")
    print(f"{'═' * 60}\n")

if __name__ == "__main__":
    asyncio.run(main())
