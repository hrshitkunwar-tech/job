import os
import json
import time
import requests
from dotenv import load_dotenv
from convex import ConvexClient

load_dotenv()

CONVEX_URL = "https://majestic-whale-830.convex.cloud"
# Since OpenAI quota is out, we will use Ollama for structuring as well
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5-coder:7b" 

client = ConvexClient(CONVEX_URL)

prompt_template = """
You are an expert autonomous SaaS agent workflow extractor.
Your job is to read actionable 'ui_steps' documentation and extract standard executeable workflow procedures.

Text:
{chunk}

CRITICAL RULES:
1. If the text does not clearly describe user UI actions (e.g., clicking specific buttons, navigating specific menus, typing text), DO NOT produce a workflow.
2. If the text is passive, refers to checking cookies, reading a concept, or is an API reference, YOU MUST REJECT IT.
3. Every target_description MUST be highly grounded and concrete (e.g. "Save Changes Button", "Settings menu item"). Abstract targets ("Video tag element") are unacceptable, use exact words if present.

If you reject the workflow based on the rules above, output EXACTLY this JSON:
{{
  "discarded": true,
  "reason": "not_ui_executable"
}}

Otherwise, output MUST be strictly valid JSON matching this schema:
{{
  "intent": string (e.g. "Create a new project"),
  "description": string,
  "preconditions": [string],
  "steps": [
    {{
      "order": int,
      "action": string (one of "navigate", "click", "select", "fill", "verify"),
      "target_description": string (the exact visible text or element to interact with),
      "value": string (optional, text to fill or option to select)
    }}
  ]
}}
"""

# Canonical mapping of semantic verbs
ACTION_MAPPING = {
    "press": "click",
    "tap": "click",
    "choose": "select",
    "pick": "select",
    "enter": "fill",
    "input": "fill",
    "type": "fill"
}

def normalize_steps(workflow):
    if not workflow or "steps" not in workflow:
        return workflow
        
    for step in workflow["steps"]:
        action = step.get("action", "").lower()
        if action in ACTION_MAPPING:
            step["action"] = ACTION_MAPPING[action]
            
    return workflow

def extract_workflow(chunk_text):
    prompt = prompt_template.format(chunk=chunk_text)
    
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json"
            },
            timeout=120
        )
        data = response.json()
        result = json.loads(data["message"]["content"])
        
        # Verify JSON
        if result.get("discarded"):
            return "discarded"
            
        if "intent" in result and "steps" in result:
            return normalize_steps(result)
            
        return None
    except Exception as e:
        print(f"Error extracting workflow: {e}")
        return None

def main():
    print("🚀 Starting Workflow Extractor Pilot...")
    
    # We will fetch only docs marked 'is_actionable=True' and 'action_type=ui_steps'
    # We'll batch this for the pilot
    docs = client.query("ingest_v2:getPilotDocs", {"limit": 20})
    print(f"Fetched {len(docs)} pilot docs.")
    
    success = 0
    discarded = 0
    failed = 0
    
    for doc in docs:
        print(f"Processing doc {doc['_id']} from tool {doc['tool_name']}...")
        workflow = extract_workflow(doc["content"])
        if workflow == "discarded":
            discarded += 1
            print("  ⏭️  Discarded by LLM guardrail (Not UI Executable)")
        elif workflow:
            # We will save this locally to view before committing to the Convex DB
            with open("pilot_workflows.jsonl", "a") as f:
                row = {
                    "tool": doc["tool_name"],
                    "doc_id": doc["_id"],
                    "workflow": workflow
                }
                f.write(json.dumps(row) + "\n")
            success += 1
            print(f"  ✅ Extracted workflow: {workflow['intent']} ({len(workflow['steps'])} steps)")
        else:
            failed += 1
            print("  ❌ Failed to structure workflow")
            
    total = len(docs)
    if total > 0:
        print("\n=== Observability Metrics ===")
        print(f"Workflows Created: {success}")
        print(f"Workflows Discarded: {discarded}")
        print(f"Failures/Timeouts: {failed}")
        print(f"UI Workflow Yield: {(success/total)*100:.1f}%")

if __name__ == "__main__":
    main()
