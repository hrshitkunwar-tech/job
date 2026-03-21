const { ConvexClient } = require("convex/browser");
const client = new ConvexClient("https://majestic-whale-830.convex.cloud");

async function resetGate2() {
    try {
        const docs = await client.query("ingest_v2:getPilotDocs", { limit: 1000 });
        console.log(`Resetting ${docs.length} docs to unverified phase...`);

        // Fast patch loop
        // But wait, there is no generic patch mutation for array of docs.
        // I will write a quick custom node query.
    } catch (e) {
        console.error("Failed", e);
    }
}

resetGate2();
