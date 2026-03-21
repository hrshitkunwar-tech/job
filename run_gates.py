"""
run_gates.py — Run Gate 1 (actionability) + Gate 2 (UI executability)
in a single pass against newly crawled docs.
"""
from convex import ConvexClient
import re
from collections import Counter

client = ConvexClient("https://majestic-whale-830.convex.cloud")

# ── Gate 1 ──────────────────────────────────────────────────────────────────
ACTION_VERBS = ["click","navigate","select","type","enter","press","go to","choose","scroll","hover","drag"]
API_WORDS    = ["endpoint","curl","post","get","request","api key","json","authorization","install","npm","pip"]

def gate1(text):
    t = text.lower()
    has_steps = bool(re.search(r'(?m)^(?:step\s*\d+:?|\d+\.)|\b(first|then|next|finally)\b', t))
    ui  = sum(1 for v in ACTION_VERBS if v in t)
    api = sum(1 for w in API_WORDS    if w in t)
    if has_steps and ui >= 1: return True,  "ui_steps",  min(0.5 + ui * 0.1, 0.95)
    if api >= 2:               return True,  "api_flow",  min(0.6 + api * 0.1, 0.95)
    if "what is" in t or "overview" in t: return False, "concept", 0.8
    if ui >= 2:                return True,  "ui_steps",  0.6
    return False, "noise", 0.0

# ── Gate 2 ──────────────────────────────────────────────────────────────────
UI_BOOSTERS  = ["click","select","choose","open","navigate","go to","press","enter","type","fill",
                "submit","button","dropdown","menu","tab","dialog"]
UI_PENALTIES = ["api key","curl","endpoint","webhook","json response"]
HARD_NEG     = ["used by google analytics","maximum storage duration","cookie name:","privacy policy",
                "http cookie","cookie","tracker"]

def gate2(text):
    t = text.lower()
    for neg in HARD_NEG:
        if neg in t: return False, "concept", False, 0.0
    b  = sum(1 for w in UI_BOOSTERS  if w in t)
    p  = sum(1 for w in UI_PENALTIES if w in t)
    score   = b - p * 2
    is_ui   = score >= 2
    surface = "ui" if is_ui else ("api" if p >= 1 else "concept")
    has_steps = bool(re.search(r'(?m)^\s*(?:step\s*\d+:?|\d+\.)', t))
    has_start = bool(re.search(r'\b(to install|to configure|how to|in order to|to create)\b', t))
    sents   = len(re.split(r'[.!?]+', text))
    conf    = 0.0
    if is_ui:
        if has_steps: conf += 0.5
        if has_start: conf += 0.3
        conf += 0.2 if sents > 2 else -0.4
    conf = min(max(conf, 0.0), 1.0)
    return is_ui, surface, conf >= 0.5, conf


def main():
    # ── Gate 1 ────────────────────────────────────────────────────────────
    print("\n🚀 Gate 1 — Actionability Classifier")
    g1_proc = g1_pass = 0
    while True:
        docs = client.query("ingest_v2:getUnclassifiedDocs", {"limit": 100})
        if not docs:
            break
        print(f"  Processing {len(docs)} unclassified docs...")
        for doc in docs:
            is_act, act_type, conf = gate1(doc["content"])
            client.mutation("ingest_v2:updateDocActionability", {
                "doc_id":            doc["_id"],
                "is_actionable":     is_act,
                "action_type":       act_type,
                "action_confidence": float(conf),
            })
            g1_proc += 1
            if is_act: g1_pass += 1
    print(f"  ✅ Gate 1: {g1_pass}/{g1_proc} passed ({g1_pass*100//max(g1_proc,1)}%)")

    # ── Gate 2 ────────────────────────────────────────────────────────────
    print("\n🚀 Gate 2 — UI Executability Classifier")
    g2_proc = g2_pass = 0
    while True:
        docs = client.query("ingest_v2:getUnverifiedActionableDocs", {"limit": 100})
        if not docs:
            break
        print(f"  Processing {len(docs)} actionable docs...")
        for doc in docs:
            is_ui, surf, is_proc, conf = gate2(doc["content"])
            client.mutation("ingest_v2:updateDocUIExecutability", {
                "doc_id":               doc["_id"],
                "is_ui_executable":     is_ui,
                "execution_surface":    surf,
                "is_complete_procedure": is_proc,
                "procedure_confidence": float(conf),
            })
            g2_proc += 1
            if is_ui and is_proc: g2_pass += 1
    print(f"  ✅ Gate 2: {g2_pass}/{g2_proc} passed ({g2_pass*100//max(g2_proc,1)}%)")

    # ── Final tally ───────────────────────────────────────────────────────
    pilot = client.query("ingest_v2:getPilotDocs", {"limit": 500})
    print(f"\n📊 Total Qualified Docs (both gates): {len(pilot)}")
    tools = Counter(d["tool_name"] for d in pilot)
    print("   Tool breakdown (Gate 2 qualified):")
    for t, n in tools.most_common():
        print(f"     {t:<22} {n:>3} doc(s)")
    print()


if __name__ == "__main__":
    main()
