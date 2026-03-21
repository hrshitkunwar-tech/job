const { ConvexClient } = require("convex/browser");
const client = new ConvexClient("https://majestic-whale-830.convex.cloud");

async function check() {
    try {
        const docs = await client.query("ingest_v2:getPilotDocs", { limit: 5 });
        docs.forEach(d => console.log(`\n--- ${d.tool_name} ---\n${d.content.substring(0, 500)}...\n`));
    } catch (e) {
        console.error("Failed", e);
    }
}

check();
