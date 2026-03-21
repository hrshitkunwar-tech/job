// ─── Normalizer ────────────────────────────────────────────────────────────
export function normalizeTargetText(text) {
    if (!text) return "";
    let clean = text.trim().replace(/^["']|["']$/g, '');
    const fillers = /\b(button|link|tab|checkbox|menuitem|or option|in the main menu|dropdown|icon|text)\b/gi;
    clean = clean.replace(fillers, '').replace(/\s+/g, ' ').trim();
    return clean || text.trim();
}

// ─── Resolver (tiered) ──────────────────────────────────────────────────────
export async function resolveSelector(page, originalTarget) {
    const results = {};
    const cleanText = normalizeTargetText(originalTarget);

    if (!cleanText) return { error: "Empty target text", confidence: 0 };
    console.log(`    🔍 Resolving: original='${originalTarget}' | normalized='${cleanText}'`);

    const evaluateLocator = async (loc, method, baseConfidence) => {
        try {
            const count = await loc.count();
            if (count > 0) {
                if (count === 1) return { method, baseConfidence, locator: loc.first(), count };
                else results[method] = { baseConfidence, locator: loc.first(), count };
            }
        } catch (e) { }
        return null;
    };

    // TIER 1 — Exact visible text
    let res = await evaluateLocator(page.getByText(cleanText, { exact: true }), "text_exact", 0.95);
    if (res) return calculateFinalScore(res);

    // TIER 2 — Role + Text
    for (const role of ["button", "link", "menuitem", "tab", "checkbox", "textbox"]) {
        const safeText = cleanText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        res = await evaluateLocator(page.getByRole(role, { name: new RegExp(safeText, "i") }), `role_${role}`, 0.85);
        if (res) return calculateFinalScore(res);
    }

    // TIER 3 — Fuzzy partial text
    res = await evaluateLocator(page.getByText(cleanText, { exact: false }), "fuzzy_text", 0.60);
    if (res) return calculateFinalScore(res);

    // Multi-match fallbacks
    if (results["text_exact"]) return calculateFinalScore({ ...results["text_exact"], method: "text_exact", isMulti: true });
    for (const key in results) {
        if (key.startsWith("role_")) return calculateFinalScore({ ...results[key], method: key, isMulti: true });
    }
    if (results["fuzzy_text"]) return calculateFinalScore({ ...results["fuzzy_text"], method: "fuzzy_text", isMulti: true });

    return { error: "Target not found on page", confidence: 0.0, is_ambiguous: false, match_count: 0 };
}

function calculateFinalScore(matchRes) {
    let conf = matchRes.baseConfidence;
    let isAmbiguous = false;
    if (matchRes.count >= 2 && matchRes.count <= 3) { conf -= 0.15; isAmbiguous = true; }
    else if (matchRes.count > 3) { conf -= 0.30; isAmbiguous = true; }
    return {
        method: matchRes.method + (matchRes.isMulti ? "_multi" : ""),
        confidence: Math.max(0, conf),
        locator: matchRes.locator,
        match_count: matchRes.count,
        is_ambiguous: isAmbiguous
    };
}

// ─── Refined NOT_VISIBLE taxonomy ──────────────────────────────────────────
export const VISIBILITY_STATUS = {
    VERIFIED: "verified",
    HIDDEN_IN_MENU: "HIDDEN_IN_MENU",   // in DOM, display:none/visibility:hidden — likely behind a trigger
    OFFSCREEN: "OFFSCREEN",        // in DOM, outside viewport
    DISABLED: "DISABLED",         // visible but disabled
    DETACHED: "DETACHED",         // element removed mid-check
    UNVERIFIED: "UNVERIFIED",       // catch-all fallback
};

export async function verifyLocator(locator) {
    try {
        // Give React/SPA time to paint, but don't wait full 5s if it's just hidden
        await locator.waitFor({ state: "attached", timeout: 3000 });

        const visible = await locator.isVisible({ timeout: 1000 });
        const enabled = await locator.isEnabled({ timeout: 1000 });

        if (visible && enabled) {
            return { status: VISIBILITY_STATUS.VERIFIED, visibility_factor: 1.0 };
        }
        if (visible && !enabled) {
            return { status: VISIBILITY_STATUS.DISABLED, visibility_factor: 0.0 };
        }

        // Element is hidden — try to classify WHY
        const hiddenReason = await locator.evaluate(el => {
            const s = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") {
                return "hidden_style";
            }
            if (rect.width === 0 && rect.height === 0) {
                return "zero_size";
            }
            if (rect.bottom < 0 || rect.top > window.innerHeight ||
                rect.right < 0 || rect.left > window.innerWidth) {
                return "offscreen";
            }
            return "other";
        }).catch(() => "detached");

        if (hiddenReason === "detached") {
            return { status: VISIBILITY_STATUS.DETACHED, visibility_factor: 0.0 };
        }
        if (hiddenReason === "offscreen") {
            return { status: VISIBILITY_STATUS.OFFSCREEN, visibility_factor: 0.0 };
        }
        // hidden_style / zero_size → likely inside a collapsed menu
        return { status: VISIBILITY_STATUS.HIDDEN_IN_MENU, visibility_factor: 0.0 };

    } catch (e) {
        if (e.message?.includes("detached")) {
            return { status: VISIBILITY_STATUS.DETACHED, visibility_factor: 0.0 };
        }
        return { status: VISIBILITY_STATUS.UNVERIFIED, visibility_factor: 0.0 };
    }
}

// ─── Phase 4B.1 — Menu Activation Layer ───────────────────────────────────
/**
 * When an element is HIDDEN_IN_MENU, walk its ancestors looking for
 * a known menu trigger (button, [aria-expanded], [aria-haspopup], nav).
 * Click the nearest candidate, wait for the DOM to settle, then return.
 *
 * Returns true if a trigger was found and clicked, false otherwise.
 */
export async function attemptMenuActivation(page, locator) {
    try {
        const triggered = await locator.evaluate(el => {
            // Walk up the DOM tree looking for a trigger
            const triggerSelectors = [
                '[aria-haspopup]',
                '[aria-expanded]',
                'button',
                '[role="button"]',
                'summary',           // <details> disclosure
                'a[href="#"]',       // common SPA nav pattern
            ];

            let node = el.parentElement;
            while (node && node !== document.body) {
                for (const sel of triggerSelectors) {
                    if (node.matches(sel)) {
                        node.click();
                        return true;
                    }
                    // Also look for a sibling trigger at same level
                    const sibling = node.querySelector(sel);
                    if (sibling && sibling !== el) {
                        sibling.click();
                        return true;
                    }
                }
                node = node.parentElement;
            }
            return false;
        });

        if (triggered) {
            // Small settle buffer for the menu animation / React re-render
            await page.waitForTimeout(400);
            return true;
        }
    } catch (e) { /* element gone mid-walk */ }
    return false;
}
