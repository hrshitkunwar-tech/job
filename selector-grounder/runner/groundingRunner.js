import { chromium } from "playwright";
import * as fs from "fs";
import { resolveSelector, verifyLocator, normalizeTargetText, attemptMenuActivation, VISIBILITY_STATUS } from "../resolver/resolveTarget.js";
import path from "path";
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ─── Failure taxonomy (Phase 4B.1 refined) ───────────────────────────────
const FAILURE = {
    NOT_FOUND: "NOT_FOUND",
    MULTI_MATCH: "MULTI_MATCH",
    HIDDEN_IN_MENU: "HIDDEN_IN_MENU",  // in DOM but behind collapsed menu
    OFFSCREEN: "OFFSCREEN",
    DISABLED: "DISABLED",
    DETACHED: "DETACHED",
    AUTH_REDIRECT: "AUTH_REDIRECT",
    TIMEOUT: "TIMEOUT",
    SHADOW_DOM: "SHADOW_DOM",
    IFRAME: "IFRAME",
    UNVERIFIED: "UNVERIFIED",
};

function classifyFailure(res, verification) {
    if (res.error) {
        if (res.error.includes("Timeout")) return FAILURE.TIMEOUT;
        if (res.error.includes("iframe")) return FAILURE.IFRAME;
        return FAILURE.NOT_FOUND;
    }
    if (res.is_ambiguous && res.match_count > 3) return FAILURE.MULTI_MATCH;
    if (verification) {
        const map = {
            [VISIBILITY_STATUS.HIDDEN_IN_MENU]: FAILURE.HIDDEN_IN_MENU,
            [VISIBILITY_STATUS.OFFSCREEN]: FAILURE.OFFSCREEN,
            [VISIBILITY_STATUS.DISABLED]: FAILURE.DISABLED,
            [VISIBILITY_STATUS.DETACHED]: FAILURE.DETACHED,
            [VISIBILITY_STATUS.UNVERIFIED]: FAILURE.UNVERIFIED,
        };
        return map[verification.status] ?? FAILURE.UNVERIFIED;
    }
    return null;
}

export async function runPilot(workflowsFile) {
    const workflows = [];
    try {
        const raw = fs.readFileSync(workflowsFile, "utf8");
        for (const line of raw.split('\n')) {
            if (line.trim()) workflows.push(JSON.parse(line));
        }
    } catch (e) {
        console.error(`❌ Could not read ${workflowsFile}: ${e.message}`);
        return;
    }

    console.log(`\n🚀 Phase 4A.7 — GitHub Deep Probe  (${workflows.length} workflows)\n`);

    // ─── Hardened context options ──────────────────────────────────────────
    const HARDENED_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';

    const browser = await chromium.launch({
        headless: false,
        channel: "chrome",
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
    });

    // ─── Per-run metrics tracking ──────────────────────────────────────────
    let totalTargets = 0;
    let resolvedCount = 0;
    let verifiedCount = 0;
    let ambiguousCount = 0;
    const failureCounts = Object.fromEntries(Object.values(FAILURE).map(k => [k, 0]));
    const failures = [];      // detailed traces
    const latencies = [];

    for (const wf_entry of workflows) {
        const tool_name = wf_entry.tool;
        const workflow = wf_entry.workflow;

        console.log(`\n${"═".repeat(50)}`);
        console.log(`🎬  Tool: ${tool_name}  |  ${workflow.intent}`);
        console.log(`${"═".repeat(50)}`);

        // Only run the github pilot for now
        if (tool_name.toLowerCase() !== "github") {
            console.log(`   ⏭️  Skipping (only github configured for Phase 4A.7)`);
            continue;
        }

        // ─── Auth & context ────────────────────────────────────────────────
        const authPath = path.join(__dirname, `../auth/${tool_name.toLowerCase()}.json`);
        const hasAuth = fs.existsSync(authPath);

        const contextOptions = {
            userAgent: HARDENED_UA,
            viewport: { width: 1280, height: 800 },
        };
        if (hasAuth) {
            console.log(`   🔐  Loading storageState: ${authPath}`);
            contextOptions.storageState = authPath;
        } else {
            console.log(`   ⚠️   No auth session — running unauthenticated`);
        }

        const context = await browser.newContext(contextOptions);
        const page = await context.newPage();
        page.setDefaultTimeout(20000);

        // ─── Step loop ────────────────────────────────────────────────────
        for (const step of (workflow.steps || [])) {
            const { action, target_description: target = "", value } = step;

            if (["click", "select", "fill", "verify"].includes(action)) {
                totalTargets++;
            }

            const normalized = normalizeTargetText(target);
            console.log(`\n  ➡️  Step ${step.order}: ${action}  →  "${normalized}"`);

            // ── navigate ─────────────────────────────────────────────────
            if (action === "navigate") {
                const url = (value && !value.includes(" ")) ? value
                    : target.match(/^https?:\/\//) ? target
                        : null;
                if (!url) {
                    console.log(`    ⚠️  No valid URL for navigate step — skipping`);
                    continue;
                }
                console.log(`    🌐  → ${url}`);
                try {
                    await page.goto(url, { waitUntil: "networkidle", timeout: 20000 });
                    await page.waitForTimeout(500);
                    const finalUrl = page.url();
                    if (finalUrl.includes("/login") || finalUrl.includes("/signin")) {
                        console.log(`    🚨  AUTH_REDIRECT detected: ${finalUrl}`);
                        failureCounts[FAILURE.AUTH_REDIRECT]++;
                        failures.push({ step: step.order, target, code: FAILURE.AUTH_REDIRECT, detail: finalUrl });
                    }
                } catch (e) {
                    const code = e.message.includes("Timeout") ? FAILURE.TIMEOUT : FAILURE.NOT_FOUND;
                    console.log(`    ❌  Navigation failed [${code}]: ${e.message.slice(0, 80)}`);
                    failureCounts[code]++;
                    failures.push({ step: step.order, target, code, detail: e.message.slice(0, 120) });
                }
                continue;
            }

            // ── interactive steps ─────────────────────────────────────────
            if (!["click", "select", "fill", "verify"].includes(action)) continue;

            const t0 = Date.now();
            const res = await resolveSelector(page, target);
            const latency = Date.now() - t0;
            latencies.push(latency);

            if (res.error) {
                const code = classifyFailure(res, null);
                console.log(`    ❌  [${code}] ${res.error}  (${latency}ms)`);
                failureCounts[code]++;
                failures.push({ step: step.order, target: normalized, code, latency, detail: res.error });
                continue;
            }

            resolvedCount++;
            if (res.is_ambiguous) ambiguousCount++;

            // ── Phase 4B.1: verify → if HIDDEN_IN_MENU, activate + retry ──
            let verification = await verifyLocator(res.locator);
            let activated = false;

            if (verification.status === VISIBILITY_STATUS.HIDDEN_IN_MENU) {
                console.log(`    🔧  HIDDEN_IN_MENU — attempting menu activation...`);
                activated = await attemptMenuActivation(page, res.locator);
                if (activated) {
                    const res2 = await resolveSelector(page, target);
                    if (!res2.error) {
                        verification = await verifyLocator(res2.locator);
                        if (verification.status === VISIBILITY_STATUS.VERIFIED) {
                            console.log(`    🟩  Activation SUCCESS — element now visible`);
                        } else {
                            console.log(`    🟡  Activated but element still hidden: ${verification.status}`);
                        }
                    }
                } else {
                    console.log(`    ⚠️   No menu trigger found in ancestors`);
                }
            }

            const uniqueness = 1.0 / res.match_count;
            const finalConfidence = res.confidence * verification.visibility_factor * uniqueness;
            const failCode = classifyFailure(res, verification);

            if (verification.status === VISIBILITY_STATUS.VERIFIED) {
                verifiedCount++;
                const tag = activated ? " [after activation]" : "";
                console.log(`    ✅  ${res.method}${tag} | matches=${res.match_count} | conf=${finalConfidence.toFixed(2)} | ${latency}ms`);
            } else {
                console.log(`    🔴  [${failCode}] | ${res.method} | ${latency}ms`);
                failureCounts[failCode]++;
                failures.push({
                    step: step.order, target: normalized, code: failCode, latency,
                    detail: `method=${res.method} matches=${res.match_count} activated=${activated}`
                });
            }
        }

        await context.close();
    }

    await browser.close();

    // ─── Final Report ──────────────────────────────────────────────────────
    const medianLatency = latencies.length
        ? latencies.sort((a, b) => a - b)[Math.floor(latencies.length / 2)]
        : 0;

    console.log(`\n${"═".repeat(50)}`);
    console.log(`📊  Phase 4A.7 Pilot — Final Metrics`);
    console.log(`${"═".repeat(50)}`);
    console.log(`  Total targets      : ${totalTargets}`);
    console.log(`  Resolution rate    : ${pct(resolvedCount, totalTargets)}`);
    console.log(`  Verified rate      : ${pct(verifiedCount, totalTargets)}`);
    console.log(`  Ambiguity rate     : ${pct(ambiguousCount, totalTargets)}`);
    console.log(`  Median latency     : ${medianLatency}ms`);

    console.log(`\n  Failure taxonomy:`);
    for (const [code, count] of Object.entries(failureCounts)) {
        if (count > 0) console.log(`    ${code.padEnd(18)}: ${count}`);
    }
    if (failures.length) {
        console.log(`\n  Failure traces:`);
        for (const f of failures) {
            console.log(`    Step ${f.step} [${f.code}]  "${f.target}"  — ${f.detail}`);
        }
    }
    console.log(`${"═".repeat(50)}\n`);
}

function pct(n, d) {
    return d === 0 ? "N/A" : `${((n / d) * 100).toFixed(1)}%`;
}

process.on("exit", (code) => console.log("Process exiting with code", code));
process.on("uncaughtException", (err) => console.error("UNCAUGHT", err));
process.on("unhandledRejection", (err) => { console.error("UNHANDLED", err); process.exit(1); });

const workflowFile = "/Users/harshit/Downloads/Projects/job/pilot_workflows.jsonl";
runPilot(workflowFile).catch(err => { console.error("MAIN CATCH", err); process.exit(1); });
