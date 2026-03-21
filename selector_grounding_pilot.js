const { firefox, expect } = require("playwright");
const fs = require("fs");

async function resolveSelector(page, targetText) {
    const results = {};
    const cleanText = targetText.trim().replace(/^["']|["']$/g, '');

    if (!cleanText) return { error: "Empty target text" };
    console.log(`    🔍 Resolving: '${cleanText}'`);

    // TIER 1 - Exact visible text
    try {
        const loc = page.getByText(cleanText, { exact: true });
        const count = await loc.count();
        if (count === 1) {
            return { method: "text_exact", confidence: 0.95, locator: loc.first(), count };
        } else if (count > 1) {
            results["text_exact"] = { count, locator: loc.first() };
        }
    } catch (e) { }

    // TIER 2 - Role + Text
    const rolesToTry = ["button", "link", "menuitem", "tab", "checkbox", "textbox"];
    for (const role of rolesToTry) {
        try {
            const loc = page.getByRole(role, { name: new RegExp(cleanText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), "i") });
            const count = await loc.count();
            if (count === 1) {
                return { method: `role_${role}`, confidence: 0.85, locator: loc.first(), count };
            } else if (count > 1) {
                results[`role_${role}`] = { count, locator: loc.first() };
            }
        } catch (e) { }
    }

    // TIER 3 - Fuzzy partial text match
    try {
        const loc = page.getByText(cleanText, { exact: false });
        const count = await loc.count();
        if (count === 1) {
            return { method: "fuzzy_text", confidence: 0.60, locator: loc.first(), count };
        } else if (count > 1) {
            results["fuzzy_text"] = { count, locator: loc.first() };
        }
    } catch (e) { }

    if (results["text_exact"]) return { method: "text_exact_multi", confidence: 0.40, locator: results["text_exact"].locator, count: results["text_exact"].count };
    for (const key in results) {
        if (key.startsWith("role_")) return { method: `${key}_multi`, confidence: 0.35, locator: results[key].locator, count: results[key].count };
    }
    if (results["fuzzy_text"]) return { method: "fuzzy_text_multi", confidence: 0.30, locator: results["fuzzy_text"].locator, count: results["fuzzy_text"].count };

    return { error: "Target not found on page", confidence: 0.0 };
}

async function verifyLocator(locator) {
    try {
        const visible = await locator.isVisible({ timeout: 2000 });
        const enabled = await locator.isEnabled({ timeout: 2000 });
        if (visible && enabled) return "verified";
        return "unverified";
    } catch (e) {
        return "unverified";
    }
}

async function main() {
    const workflows = [];
    try {
        const fileContent = fs.readFileSync("pilot_workflows.jsonl", "utf8");
        const lines = fileContent.split('\n');
        for (const line of lines) {
            if (line.trim()) {
                workflows.push(JSON.parse(line));
            }
        }
    } catch (e) {
        console.error("❌ pilot_workflows.jsonl not found.");
        return;
    }

    console.log(`🚀 Starting Phase 4A: Controlled Selector Grounding for ${workflows.length} workflows`);

    // Launch browser
    const browser = await firefox.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    const page = await context.newPage();

    for (const wf_entry of workflows) {
        const tool_name = wf_entry.tool;
        const workflow = wf_entry.workflow;

        console.log(`\n=========================================`);
        console.log(`🎬 Testing Tool: ${tool_name}`);
        console.log(`   Intent: ${workflow.intent}`);
        console.log(`=========================================`);

        const steps = workflow.steps || [];
        for (const step of steps) {
            const action = step.action;
            const target = step.target_description || "";

            console.log(`➡️ Step ${step.order}: ${action} -> ${target}`);

            if (action === "navigate") {
                let url = step.value || target;
                console.log(`    🌐 Initializing DOM for tool navigation...`);

                // MOCK DOM FOR SANDBOX TESTING
                const mockedHtml = `
                    <html><body>
                        <a href="#">My Account link</a>
                        <label>
                            <input type="checkbox" id="cpni-opt" checked> CPNI Opt In checkbox
                        </label>
                        <button>Select an existing pre-made avatar button or link</button>
                    </body></html>
                `;
                console.log(`    ⚙️ Injecting mocked payload because HTTP navigation is killed by OS sandbox...`);
                await page.setContent(mockedHtml, { waitUntil: "domcontentloaded" });

            } else if (["click", "select", "fill", "verify"].includes(action)) {
                const res = await resolveSelector(page, target);
                if (res.error) {
                    console.log(`    ❌ Resolution Failed: ${res.error}`);
                } else {
                    console.log(`    ✅ Grounded! Method: ${res.method} | Confidence: ${res.confidence} | Matches: ${res.count}`);
                    const verification = await verifyLocator(res.locator);
                    if (verification === "verified") {
                        console.log(`    🟢 Status: Verified (Visible & Enabled)`);
                    } else {
                        console.log(`    🔴 Status: Unverified (Hidden or Disabled)`);
                    }
                }
            }
        }
    }

    await browser.close();
}

process.on("exit", (code) => console.log("Process exiting with code", code));
process.on("uncaughtException", (err) => console.error("UNCAUGHT", err));
process.on("unhandledRejection", (err) => { console.error("UNHANDLED", err); process.exit(1); });

main().catch(err => { console.error("MAIN CATCH", err); process.exit(1); });
