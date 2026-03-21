const { ConvexClient } = require("convex/browser");
const client = new ConvexClient("https://industrious-platypus-909.convex.cloud");

async function check() {
    try {
        const scrapedataCount = await client.query("scrapedata:listTools", {});
        console.log(`Phase 1 - Total unique Scrapdata tools available to ingest: ${scrapedataCount.length}`);
    } catch (e) {
        console.error("Failed", e);
    }
}

check();
