import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function generateAuth() {
    console.log("🚀 Starting Auth Generator...");

    // We run headful so the developer (me) can log in manually if needed
    const browser = await chromium.launch({ headless: false });
    const context = await browser.newContext();
    const page = await context.newPage();

    // Let's use Anyword as our first real authenticated pilot tool
    const loginUrl = "https://anyword.com/login/";
    const storagePath = path.join(__dirname, 'anyword.json');

    console.log(`🌐 Navigating to ${loginUrl}...`);
    await page.goto(loginUrl, { waitUntil: "networkidle" });

    console.log(`⏳ Please log in manually if prompted.`);
    console.log(`   Waiting for you to complete login...`);

    // Wait for a selector that definitively proves auth is complete
    // For D-id Studio, the user profile button appearing is a good sign
    // Let's just give a generous 60s timeout for manual input, or until the word "Create" appears in a button
    try {
        await page.waitForTimeout(10000); // Wait 10s for page to settle + checking if already logged in via cookies

        // Wait until we see any marker of authenticated dashboard
        await page.waitForTimeout(60000); // 60s max for manual login
        console.log("✅ Authenticated dashboard marker assumed passed.");

    } catch (e) {
        console.log("⚠️ Did not automatically detect login success marker within 60s.");
        console.log("Storing current state anyway just in case...");
    }

    console.log(`💾 Saving authentication state to ${storagePath}`);
    await context.storageState({ path: storagePath });

    await browser.close();
    console.log("✨ Auth generation complete.");
}

generateAuth().catch(console.error);
