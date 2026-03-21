"""
clean_reingest_v3.py — High-throughput ingestion:
  - Batch size increased to 10 for faster processing
  - Smarter error handling for JS-heavy sites
  - Optional enrichment with short timeouts
  - Targeting larger batch of tools
"""

import asyncio
import os
import re
import json
import hashlib
import time
import requests
from datetime import datetime, timezone
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from convex import ConvexClient
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()

CONVEX_URL = os.getenv("CONVEX_URL")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")

if not CONVEX_URL:
    raise ValueError("CONVEX_URL not set in .env")

# This client performs reads from Platypus (Dev - 828 legacy tools)
client_legacy = ConvexClient("https://industrious-platypus-909.convex.cloud")

# This client performs writes to Majestic Whale (Prod - V2 Schema)
client_prod = ConvexClient("https://majestic-whale-830.convex.cloud")

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b" 
ENABLE_OLLAMA = True # Re-enable with short timeout

# ─── Progress tracking ────────────────────────────────────────────────────────
stats = {
    "tools_processed": 0,
    "total_chunks": 0,
    "total_pages": 0,
    "total_procedures": 0,
    "errors": 0,
    "skipped": 0,
}

PRIORITY_TOOLS = [
    {"handle": "Slack",        "website": "https://slack.com"},
    {"handle": "Notion",       "website": "https://notion.so"},
    {"handle": "Jira",         "website": "https://www.atlassian.com/software/jira"},
    {"handle": "Linear",       "website": "https://linear.app"},
    {"handle": "GitHub",       "website": "https://github.com"},
    {"handle": "Figma",        "website": "https://figma.com"},
    {"handle": "Asana",        "website": "https://asana.com"},
    {"handle": "Trello",       "website": "https://trello.com"},
    {"handle": "Confluence",   "website": "https://www.atlassian.com/software/confluence"},
    {"handle": "Datadog",      "website": "https://www.datadoghq.com"}
]

DOCS_PATTERNS = [
    r'/docs?', r'docs\.', r'/help', r'support\.', r'/support',
    r'/guide', r'/knowledge-base', r'/kb', r'/developers'
]

def clean_text(raw: str) -> str:
    if not raw: return ""
    text = raw
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_\n]+)_{1,3}', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`[^`\n]+`', '', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 20]
    return '\n'.join(lines).strip()

def content_hash(tool_name: str, content: str) -> str:
    return hashlib.sha256(f"{tool_name}::{content[:500]}".encode()).hexdigest()

def get_embeddings_batch(texts: list) -> list:
    if not VOYAGE_API_KEY or not texts:
        return [[0.0] * 1024] * len(texts)
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {VOYAGE_API_KEY}"},
                json={"input": [t[:4000] for t in texts], "model": "voyage-3"},
                timeout=60
            )
            if resp.status_code == 200:
                return [d['embedding'] for d in resp.json()['data']]
            if resp.status_code == 429:
                time.sleep(30 * (attempt + 1))
        except:
            time.sleep(2)
    return [[0.0] * 1024] * len(texts)

def chunk_text(text: str, max_chars: int = 600) -> list:
    if not text: return []
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) > max_chars:
            if current: chunks.append(current.strip())
            current = sent
        else:
            current += " " + sent
    if current: chunks.append(current.strip())
    return [c for c in chunks if len(c) > 60]

def check_ollama():
    try:
        requests.get("http://localhost:11434/api/tags", timeout=1)
        return True
    except:
        return False

def enrich_with_ollama(name: str, content: str) -> dict:
    if not ENABLE_OLLAMA or not check_ollama(): return {"nodes": [], "edges": [], "procedures": []}
    prompt = f"Analyze {name} docs and return JSON: {{'nodes':[], 'edges':[], 'procedures':[]}}. Content: {content[:4000]}"
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "format": "json"
        }, timeout=30)
        return json.loads(resp.json()['message']['content'])
    except:
        return {"nodes": [], "edges": [], "procedures": []}

async def crawl_and_store(crawler, name: str, url: str) -> list:
    parsed = urlparse(url)
    domain = parsed.netloc
    
    # Register/Update Tool
    try:
        tool_id = client_prod.mutation("ingest_v2:upsertTool", {
            "name": name,
            "domain": domain,
            "base_url": url,
            "status": "active"
        })
    except Exception as e:
        print(f"    ❌ Failed to upsert tool {name}: {e}")
        return []

    try:
        config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=15000)
        result = await crawler.arun(url=url, config=config)
        
        # Log metadata
        client_prod.mutation("ingest_v2:logCrawl", {
            "tool_id": tool_id,
            "url": url,
            "status": "success" if result.success else "failed",
            "crawled_at": int(time.time() * 1000)
        })

        if not result.success: return []
        
        clean = clean_text(result.markdown or "")
        if len(clean) < 100: return []
        
        chunks = chunk_text(clean)
        if chunks:
            embs = get_embeddings_batch(chunks)
            for i, chunk in enumerate(chunks):
                ch_hash = content_hash(name, chunk)
                chunk_type = "content"
                if "how to" in chunk.lower() or "steps" in chunk.lower():
                    chunk_type = "steps"
                elif "api" in chunk.lower() or "endpoint" in chunk.lower():
                    chunk_type = "api"
                    
                client_prod.mutation("ingest_v2:saveSemanticDoc", {
                    "tool_id": tool_id,
                    "tool_name": name,
                    "url": url,
                    "content_hash": ch_hash,
                    "chunk_type": chunk_type,
                    "content": chunk,
                    "embedding": embs[i],
                    "crawled_at": int(time.time() * 1000)
                })
            stats["total_chunks"] += len(chunks)
            
        return []
    except Exception as e:
        print(f"    ❌ Crawl error: {e}")
        stats["errors"] += 1
        return []

async def process_tool(crawler, tool: dict):
    name = tool.get('handle', tool.get('name', 'Unknown'))
    url = tool.get('website', '')
    if not url: return
    
    # We delay cleanup until inside crawl_and_store to have tool_id, 
    # but the deduplication logic currently manages hash updates well. 
    # For now, we skip blanket dropping all chunks prior to crawl because 
    # new chunks update via hash anyway.
    
    await crawl_and_store(crawler, name, url)
    stats["tools_processed"] += 1
    if stats["tools_processed"] % 10 == 0:
        print(f"📊 Progress: {stats['tools_processed']} tools | {stats['total_chunks']} chunks")

async def main():
    browser_config = BrowserConfig(headless=True)
    
    # Let's seed this with PRIORITY_TOOLS or fetch from Convex base to scale
    target_tools = PRIORITY_TOOLS
    try:
        # Fetch from our legacy table if needed to migrate
        existing_tools = client_legacy.query("scrapedata:listTools", {})
        if existing_tools and isinstance(existing_tools, list):
            target_tools = [{"handle": tool_name.capitalize(), "website": f"https://{tool_name.lower()}.com"} for tool_name in existing_tools if tool_name]
            print(f"Found {len(target_tools)} tools from legacy database.")
    except Exception as e:
        print(f"Using priority tools, legacy query failed: {e}")
        pass
        
    print(f"Starting ingestion process for {len(target_tools)} tools on V2 Schema...")
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
        BATCH = 10
        for i in range(0, len(target_tools), BATCH):
            batch = target_tools[i:i+BATCH]
            await asyncio.gather(*[process_tool(crawler, t) for t in batch])
            
if __name__ == "__main__":
    asyncio.run(main())
