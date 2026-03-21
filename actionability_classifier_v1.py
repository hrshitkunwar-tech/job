import os
import json
import time
import requests
from dotenv import load_dotenv
from convex import ConvexClient

load_dotenv()

# We are connecting to the production DB
CONVEX_URL = "https://majestic-whale-830.convex.cloud"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set.")

client = ConvexClient(CONVEX_URL)

import re

# Action verbs strongly correlated with UI workflows
ACTION_VERBS = ["click", "navigate", "select", "type", "enter", "press", "go to", "choose", "scroll", "hover", "drag"]
# Words correlated with APIs / config
API_WORDS = ["endpoint", "curl", "post", "get", "request", "api key", "json", "authorization", "install", "npm", "pip"]

def classify_chunk(text):
    text_lower = text.lower()
    
    # Heuristic 1: Ordered lists or steps (e.g. 1. Click..., Step 1:, etc.)
    has_steps = bool(re.search(r'(?m)^(?:step\s*\d+:?|\d+\.)|\b(first|then|next|finally)\b', text_lower))
    
    # Count action verbs mapping
    ui_verb_count = sum(1 for verb in ACTION_VERBS if verb in text_lower)
    api_word_count = sum(1 for word in API_WORDS if word in text_lower)
    
    # Thresholding logic
    is_actionable = False
    action_type = "noise"
    action_confidence = 0.0
    
    if has_steps and ui_verb_count >= 1:
        is_actionable = True
        action_type = "ui_steps"
        action_confidence = min(0.5 + (ui_verb_count * 0.1), 0.95)
    elif api_word_count >= 2:
        is_actionable = True
        action_type = "api_flow"
        action_confidence = min(0.6 + (api_word_count * 0.1), 0.95)
    elif "what is" in text_lower or "overview" in text_lower:
        action_type = "concept"
        action_confidence = 0.8
    elif ui_verb_count >= 2:
        is_actionable = True
        action_type = "ui_steps"
        action_confidence = 0.6
        
    return {
        "is_actionable": is_actionable,
        "action_confidence": action_confidence,
        "action_type": action_type
    }

def main():
    print("🚀 Starting Actionability Classifier v1...")
    
    processed = 0
    while True:
        docs = client.query("ingest_v2:getUnclassifiedDocs", {"limit": 50})
        
        if not docs:
            print("No unclassified docs found. Sleeping for 10s...")
            time.sleep(10)
            continue
            
        print(f"Fetched {len(docs)} unclassified docs.")
        
        for doc in docs:
            res = classify_chunk(doc["content"])
            if res:
                # Update DB
                client.mutation("ingest_v2:updateDocActionability", {
                    "doc_id": doc["_id"],
                    "is_actionable": res["is_actionable"],
                    "action_confidence": float(res.get("action_confidence", 0.0)),
                    "action_type": res["action_type"]
                })
                processed += 1
                if processed % 10 == 0:
                    print(f"Processed {processed} docs.")
            else:
                # Fallback if api fails, just mark as noise so we don't loop
                client.mutation("ingest_v2:updateDocActionability", {
                    "doc_id": doc["_id"],
                    "is_actionable": False,
                    "action_confidence": 0.0,
                    "action_type": "noise"
                })

if __name__ == "__main__":
    main()
