import json
import re
import time
from playwright.sync_api import sync_playwright, expect

def resolve_selector(page, target_text):
    """
    Tiered selector resolution strategy
    """
    results = {}
    
    # Clean up the target text a bit (remove quotes, trim)
    clean_text = target_text.strip().strip('"\'')
    if not clean_text:
        return {"error": "Empty target text"}
        
    print(f"    🔍 Resolving: '{clean_text}'")

    # TIER 1 - Exact visible text
    try:
        loc = page.get_by_text(clean_text, exact=True)
        count = loc.count()
        if count == 1:
            return {
                "method": "text_exact",
                "confidence": 0.95,
                "locator": loc.first,
                "count": count
            }
        elif count > 1:
            results["text_exact"] = {"count": count, "locator": loc.first}
    except Exception as e:
        pass

    # TIER 2 - Role + Text (common interactive roles)
    roles_to_try = ["button", "link", "menuitem", "tab", "checkbox", "textbox"]
    for role in roles_to_try:
        try:
            loc = page.get_by_role(role, name=re.compile(re.escape(clean_text), re.IGNORECASE))
            count = loc.count()
            if count == 1:
                return {
                    "method": f"role_{role}",
                    "confidence": 0.85,
                    "locator": loc.first,
                    "count": count
                }
            elif count > 1:
                results[f"role_{role}"] = {"count": count, "locator": loc.first}
        except Exception:
            pass

    # TIER 3 - Fuzzy partial text match
    try:
        loc = page.get_by_text(clean_text, exact=False)
        count = loc.count()
        if count == 1:
            return {
                "method": "fuzzy_text",
                "confidence": 0.60,
                "locator": loc.first,
                "count": count
            }
        elif count > 1:
            results["fuzzy_text"] = {"count": count, "locator": loc.first}
    except Exception:
        pass
        
    # Return highest quality partial match if multi-match found, but lower confidence
    if "text_exact" in results:
        return {"method": "text_exact_multi", "confidence": 0.40, "locator": results["text_exact"]["locator"], "count": results["text_exact"]["count"]}
    for k, v in results.items():
        if k.startswith("role_"):
            return {"method": f"{k}_multi", "confidence": 0.35, "locator": v["locator"], "count": v["count"]}
    if "fuzzy_text" in results:
        return {"method": "fuzzy_text_multi", "confidence": 0.30, "locator": results["fuzzy_text"]["locator"], "count": results["fuzzy_text"]["count"]}

    return {"error": "Target not found on page", "confidence": 0.0}

def verify_locator(locator):
    """
    Verification runner (checks existence, visibility, enabled state)
    """
    try:
        expect(locator).to_be_visible(timeout=2000)
        expect(locator).to_be_enabled(timeout=2000)
        return "verified"
    except Exception as e:
        # It exists but may not be visible or enabled right now
        return "unverified"

def main():
    workflows = []
    try:
        with open("pilot_workflows.jsonl", "r") as f:
            for line in f:
                if line.strip():
                    workflows.append(json.loads(line))
    except FileNotFoundError:
        print("❌ pilot_workflows.jsonl not found.")
        return

    print(f"🚀 Starting Phase 4A: Controlled Selector Grounding for {len(workflows)} workflows")
    
    with sync_playwright() as p:
        # Launch headless Chromium
        browser = p.chromium.launch(headless=True)
        # Using a typical desktop viewport
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()

        for wf_entry in workflows:
            tool_name = wf_entry["tool"]
            workflow = wf_entry["workflow"]
            print(f"\n=========================================")
            print(f"🎬 Testing Tool: {tool_name}")
            print(f"   Intent: {workflow['intent']}")
            print(f"=========================================")
            
            steps = workflow.get("steps", [])
            for step in steps:
                action = step.get("action")
                target = step.get("target_description", "")
                
                print(f"➡️ Step {step['order']}: {action} -> {target}")
                
                # If it's a navigation step, we navigate
                if action == "navigate":
                    url = step.get("value", "") or target
                    # extremely naive url fixer for arbitrary target texts
                    if "http" not in url:
                        # strip out non-domain text if it's messy
                        match = re.search(r'([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', url)
                        if match:
                            url = "https://" + match.group(1)
                        else:
                            # Try to navigate to tool's domain (mocking)
                            print(f"    ⚠️ Invalid URL: {url}. Skipping navigation.")
                            continue
                            
                    print(f"    🌐 Navigating to {url}")
                    try:
                        print("Going to url...")
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        print("Done goto. Waiting 2000...")
                        # Give it a second to settle SPA
                        page.wait_for_timeout(2000)
                        print("Done wait.")
                    except Exception as e:
                        print(f"    ❌ Navigation failed: {e}")
                    print("Finished navigate block.")
                
                elif action in ["click", "select", "fill", "verify"]:
                    res = resolve_selector(page, target)
                    
                    if "error" in res:
                        print(f"    ❌ Resolution Failed: {res['error']}")
                    else:
                        conf = res["confidence"]
                        count = res["count"]
                        method = res["method"]
                        print(f"    ✅ Grounded! Method: {method} | Confidence: {conf} | Matches: {count}")
                        
                        # Verify
                        verification = verify_locator(res["locator"])
                        if verification == "verified":
                            print(f"    🟢 Status: Verified (Visible & Enabled)")
                        else:
                            print(f"    🔴 Status: Unverified (Hidden or Disabled)")
                            
        browser.close()

if __name__ == "__main__":
    main()
