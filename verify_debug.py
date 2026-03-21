from convex import ConvexClient
from dotenv import load_dotenv
import os

load_dotenv()
CONVEX_URL = os.getenv("CONVEX_URL")
if not CONVEX_URL: raise ValueError("No URL")
client = ConvexClient(CONVEX_URL)

def debug_query():
    print("Testing basic vector search without filter...")
    try:
        chunks = client.action("ingest:vectorSearch", {
            "query": "how to use Slack", 
            "limit": 5,
            "minScore": 0.0 # Force return everything
        })
        print(f"Found {len(chunks)} chunks:")
        for c in chunks:
            print(f"- [{c.get('score', 0):.2f}] {c['tool_name']}: {c['text'][:80]}...")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    debug_query()
