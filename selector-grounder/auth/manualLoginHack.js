import { chromium } from "playwright";
import * as fs from "fs";
import path from "path";
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function manualAuthRoutine() {
    const browser = await chromium.launch({
        headless: false,
        channel: "chrome", // Prevents Auth0/Google from realizing it is a bot
        args: ['--disable-blink-features=AutomationControlled']
    });
    const context = await browser.newContext();
    const page = await context.newPage();

    console.log("Navigating to Github Login...");
    await page.goto("https://github.com/login");

    console.log("⚠️ Paused. You must interact with the browser window to log in.");
    console.log("When you are fully logged in and can see your dashboard, close the browser window.");

    // Pause script indefinitely until user closes the window
    try {
        await page.waitForEvent('close', { timeout: 0 }); // timeout 0 means wait forever
    } catch (e) { }

    console.log("Saving storageState...");
    await context.storageState({ path: path.join(__dirname, 'github.json') });

    console.log("Done.");
    await browser.close();
}

manualAuthRoutine().catch(console.error);
