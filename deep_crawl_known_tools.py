"""
deep_crawl_known_tools.py — Deep crawl specific article URLs for known tools.

Instead of crawling index pages, this script fetches specific help articles
that are known to contain step-by-step UI workflows — the content that 
passes Gate 2 (is_complete_procedure = True).

Tools covered:
  - Notion, Slack, Linear, Figma, HubSpot, Zapier, Loom, Retool, Monday, Sentry
  - GitHub (existing, but adding more specific workflow pages)
  
Run:
    python deep_crawl_known_tools.py [--tool slack] [--dry-run] [--limit 5]
"""

import asyncio
import os
import re
import hashlib
import time
import argparse
import requests
from urllib.parse import urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from convex import ConvexClient
from dotenv import load_dotenv

load_dotenv()

CONVEX_PROD = "https://majestic-whale-830.convex.cloud"
VOYAGE_KEY  = os.getenv("VOYAGE_API_KEY", "")
client      = ConvexClient(CONVEX_PROD)

# ─── Known deep help article URLs per tool ────────────────────────────────────
TOOL_ARTICLE_URLS = {
    "Notion": [
        "https://www.notion.so/help/create-a-page",
        "https://www.notion.so/help/add-and-edit-content-within-a-page",
        "https://www.notion.so/help/databases",
        "https://www.notion.so/help/share-collaborate-with-your-team",
        "https://www.notion.so/help/intro-to-databases",
    ],
    "Slack": [
        "https://slack.com/help/articles/201402297-Create-a-channel",
        "https://slack.com/help/articles/217626328-Send-a-message",
        "https://slack.com/help/articles/202288908-Compose-a-message",
        "https://slack.com/help/articles/212281468-Understand-slash-commands",
        "https://slack.com/help/articles/203950418-Use-search-in-Slack",
    ],
    "Linear": [
        "https://linear.app/docs/introduction",
        "https://linear.app/docs/creating-issues",
        "https://linear.app/docs/workflows",
        "https://linear.app/docs/cycles",
        "https://linear.app/docs/github",
    ],
    "Figma": [
        "https://help.figma.com/hc/en-us/articles/360040451373-Create-a-new-design-file",
        "https://help.figma.com/hc/en-us/articles/360040029053-Use-the-Pen-tool",
        "https://help.figma.com/hc/en-us/articles/360040035874-Create-frames-and-groups",
        "https://help.figma.com/hc/en-us/articles/360040030374-Create-and-use-components",
        "https://help.figma.com/hc/en-us/articles/360039951073-Export-assets-and-designs",
    ],
    "HubSpot": [
        "https://knowledge.hubspot.com/crm-setup/import-contacts-companies-deals-or-tickets",
        "https://knowledge.hubspot.com/contacts/create-contacts",
        "https://knowledge.hubspot.com/deals/create-deals",
        "https://knowledge.hubspot.com/email/create-and-send-emails",
        "https://knowledge.hubspot.com/workflows/create-workflows",
    ],
    "Zapier": [
        "https://zapier.com/help/create/basics/create-zaps",
        "https://zapier.com/help/create/basics/set-up-your-zap-trigger",
        "https://zapier.com/help/create/basics/set-up-your-zap-action",
        "https://zapier.com/help/create/code-webhooks/add-filters-to-zaps",
        "https://zapier.com/help/create/basics/test-and-publish-your-zap",
    ],
    "Loom": [
        "https://support.loom.com/hc/en-us/articles/36000217586-Record-a-video-with-Loom",
        "https://support.loom.com/hc/en-us/articles/360016895572-Share-your-Loom-video",
        "https://support.loom.com/hc/en-us/articles/360040391551-Edit-your-videos-in-Loom",
        "https://support.loom.com/hc/en-us/articles/10671803166733-Add-a-call-to-action",
        "https://support.loom.com/hc/en-us/articles/360002078386-Trim-and-cut-your-video",
    ],
    "Retool": [
        "https://docs.retool.com/docs/quickstart",
        "https://docs.retool.com/docs/build-your-first-app",
        "https://docs.retool.com/docs/connecting-to-data-sources",
        "https://docs.retool.com/docs/tables",
        "https://docs.retool.com/docs/buttons",
    ],
    "Monday": [
        "https://support.monday.com/hc/en-us/articles/36000275541-How-to-create-a-new-board",
        "https://support.monday.com/hc/en-us/articles/11820832095761-Add-items-to-a-board",
        "https://support.monday.com/hc/en-us/articles/360002755741-Set-up-automations",
        "https://support.monday.com/hc/en-us/articles/360002615920-Invite-people-to-your-team",
        "https://support.monday.com/hc/en-us/articles/360002756181-Gantt-view",
    ],
    "Sentry": [
        "https://docs.sentry.io/product/issues/",
        "https://docs.sentry.io/product/alerts/create-alerts/",
        "https://docs.sentry.io/product/releases/setup/",
        "https://docs.sentry.io/concepts/key-terms/tracing/",
        "https://docs.sentry.io/product/dashboards/",
    ],
    "GitHub": [
        "https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository",
        "https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request",
        "https://docs.github.com/en/issues/tracking-your-work-with-issues/creating-an-issue",
        "https://docs.github.com/en/actions/quickstart",
        "https://docs.github.com/en/codespaces/getting-started/quickstart",
    ],
    "Writesonic": [
        "https://help.writesonic.com/en/articles/6447648-ai-article-writer-5-0",
        "https://help.writesonic.com/en/articles/8186244-chatsonic-your-ai-assistant",
        "https://help.writesonic.com/en/articles/6200534-how-to-create-landing-page-copy",
    ],
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

def get_embeddings(texts: list) -> list:
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
            print(f"    ⚠️  Voyage: {e}")
            time.sleep(2)
    return [[0.0] * 1024] * len(texts)

# ─── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--tool",    default=None, help="Single tool name to crawl")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--limit",   default=5, type=int, help="Max articles per tool")
args = parser.parse_args()

# ─── Deep Crawl ───────────────────────────────────────────────────────────────
async def crawl_article(crawler, tool_name: str, tool_id: str, url: str) -> int:
    try:
        cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=20000)
        result = await crawler.arun(url=url, config=cfg)
        if not result.success:
            print(f"      ⚠️   fail: {url[:60]}")
            return 0
        clean = clean_text(result.markdown or "")
        if len(clean) < 100:
            return 0
        chunks = chunk_text(clean)
        if not chunks:
            return 0
        embs = get_embeddings(chunks)
        for i, chunk in enumerate(chunks):
            ch_type = "steps" if any(x in chunk.lower() for x in ["how to","step","click","navigate"]) else "content"
            client.mutation("ingest_v2:saveSemanticDoc", {
                "tool_id":       tool_id,
                "tool_name":     tool_name,
                "url":           url,
                "content_hash":  content_hash(tool_name, chunk),
                "chunk_type":    ch_type,
                "content":       chunk,
                "embedding":     embs[i],
                "crawled_at":    int(time.time() * 1000),
            })
        print(f"      ✅  {url.split('/')[-1][:40]} → {len(chunks)} chunks")
        return len(chunks)
    except Exception as e:
        print(f"      ❌  {url[:60]}: {str(e)[:60]}")
        return 0

async def main():
    tools_to_run = {args.tool: TOOL_ARTICLE_URLS[args.tool]} if args.tool else TOOL_ARTICLE_URLS

    print(f"\n{'═'*60}")
    print(f"🔍  Deep-Crawl — Help Article Ingestion")
    print(f"    Tools: {len(tools_to_run)}   Articles per tool: up to {args.limit}")
    print(f"{'═'*60}\n")

    if args.dry_run:
        for tool, urls in tools_to_run.items():
            print(f"  {tool}:")
            for u in urls[:args.limit]:
                print(f"    • {u}")
        return

    total_chunks = 0
    browser_cfg = BrowserConfig(headless=True, verbose=False)
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for tool_name, urls in tools_to_run.items():
            print(f"\n🔧  {tool_name} ({min(len(urls), args.limit)} articles)")
            domain = urlparse(urls[0]).netloc
            try:
                tool_id = client.mutation("ingest_v2:upsertTool", {
                    "name": tool_name, "domain": domain,
                    "base_url": f"https://{domain}", "status": "active",
                })
            except Exception as e:
                print(f"    ❌  upsertTool: {e}")
                continue

            tool_chunks = 0
            for url in urls[:args.limit]:
                n = await crawl_article(crawler, tool_name, tool_id, url)
                tool_chunks += n
                await asyncio.sleep(0.5)  # polite delay

            total_chunks += tool_chunks
            print(f"    → {tool_chunks} chunks total for {tool_name}")

    print(f"\n{'═'*60}")
    print(f"📊  Deep-Crawl Complete")
    print(f"    Total chunks written: {total_chunks}")
    print(f"\n  Next: python run_gates.py")
    print(f"{'═'*60}\n")

if __name__ == "__main__":
    asyncio.run(main())
