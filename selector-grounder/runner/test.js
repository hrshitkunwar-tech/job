import { chromium } from "playwright";
import fs from "fs";

async function run() {
    console.log("launching");
    const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-setuid-sandbox"] });
    console.log("context");
    const storageState = JSON.parse(fs.readFileSync("../auth/d-id.json"));
    const context = await browser.newContext({ storageState });
    const page = await context.newPage();
    console.log("goto");
    page.on("console", msg => console.log("PAGE LOG:", msg.text()));
    page.on("pageerror", err => console.log("PAGE ERROR:", err));

    try {
        await page.goto("https://studio.d-id.com/", { waitUntil: "networkidle", timeout: 15000 });
        console.log("goto done");
    } catch (e) {
        console.log("goto err", e.message);
    }

    await browser.close();
}

run().catch(console.error);
