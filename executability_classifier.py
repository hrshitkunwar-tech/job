import os
import json
import time
import re
from convex import ConvexClient

# We are connecting to the production DB
CONVEX_URL = "https://majestic-whale-830.convex.cloud"
client = ConvexClient(CONVEX_URL)

# Strong positive UI action signals
UI_BOOSTERS = [
    "click", "select", "choose", "open", "navigate", "go to", "press", "enter", "type", "fill", "submit", "button", "dropdown", "menu", "tab", "dialog"
]

# Strong negative passive/API signals
UI_PENALTIES = [
    "api key", "curl", "endpoint", "webhook", "json response"
]

# Hard Negative Kill Switches
HARD_NEGATIVES = [
    "used by google analytics", "maximum storage duration", "data retention", 
    "cookie name:", "privacy policy", "consent management", "pixel tracker", 
    "http cookie", "cookie", "tracker", "expiry"
]

def determine_executability(text):
    text_lower = text.lower()
    
    # Tweak #2 - Hard Negative Blocks
    for neg in HARD_NEGATIVES:
        if neg in text_lower:
            return {
                "is_ui_executable": False,
                "execution_surface": "pixel" if "cookie" in neg or "pixel" in neg or "tracker" in neg else "concept",
                "is_complete_procedure": False,
                "procedure_confidence": 0.0
            }
    
    booster_count = sum(1 for w in UI_BOOSTERS if w in text_lower)
    penalty_count = sum(1 for w in UI_PENALTIES if w in text_lower)
    
    # Calculate simple score
    score = booster_count - (penalty_count * 2) # Penalties weigh heavily
    
    is_ui_executable = False
    execution_surface = "concept"
    
    if score >= 2:
        is_ui_executable = True
        execution_surface = "ui"
    elif penalty_count >= 1 and "api" in text_lower or "curl" in text_lower:
        execution_surface = "api"
        
    # Tweak #1 - Workflow Boundary Detector
    is_complete_procedure = False
    procedure_confidence = 0.0
    
    if is_ui_executable:
        # Check for completeness signals
        # Numbered steps: "1.", "2.", "Step 1"
        has_numbered_steps = bool(re.search(r'(?m)^\s*(?:step\s*\d+:?|\d+\.)', text_lower))
        # Imperative chains / "To start..."
        has_clear_start = bool(re.search(r'\b(to install|to configure|to set up|how to|in order to|to create)\b', text_lower))
        
        sentence_count = len(re.split(r'[.!?]+', text))
        
        if has_numbered_steps:
            procedure_confidence += 0.5
        if has_clear_start:
            procedure_confidence += 0.3
        if sentence_count > 2:
            procedure_confidence += 0.2
        else:
            procedure_confidence -= 0.4  # Fragment
            
        if procedure_confidence >= 0.5:
            is_complete_procedure = True
            
        procedure_confidence = min(max(procedure_confidence, 0.0), 1.0)
        
    return {
        "is_ui_executable": is_ui_executable,
        "execution_surface": execution_surface,
        "is_complete_procedure": is_complete_procedure,
        "procedure_confidence": procedure_confidence
    }

def main():
    print("🚀 Starting UI Executability Classifier (Semantic Gate 2)...")
    
    processed = 0
    passed = 0
    
    while True:
        docs = client.query("ingest_v2:getUnverifiedActionableDocs", {"limit": 100})
        
        if not docs:
            print("No unverified docs found. Sleeping for 10s...")
            time.sleep(10)
            continue
            
        print(f"Fetched {len(docs)} unverified UI docs.")
        
        for doc in docs:
            res = determine_executability(doc["content"])
            
            # Update DB
            client.mutation("ingest_v2:updateDocUIExecutability", {
                "doc_id": doc["_id"],
                "is_ui_executable": res["is_ui_executable"],
                "execution_surface": res["execution_surface"],
                "is_complete_procedure": res.get("is_complete_procedure", False),
                "procedure_confidence": float(res.get("procedure_confidence", 0.0))
            })
            
            processed += 1
            if res["is_ui_executable"] and res.get("is_complete_procedure", False):
                passed += 1
                
            if processed % 10 == 0:
                print(f"Gate 2 Processed {processed} docs. Yield: {passed}/{processed} ({(passed/processed)*100:.1f}%)")

if __name__ == "__main__":
    main()

