const { ConvexClient } = require("convex/browser");
const client = new ConvexClient("https://majestic-whale-830.convex.cloud");

async function check() {
  try {
    const docs = await client.query("ingest_v2:getPilotDocs", { limit: 10 });
    console.log(`Docs that passed both gates: ${docs.length}\n`);
    docs.forEach((d, i) => {
      console.log(`--- [${i + 1}] Tool: ${d.tool_name} ---`);
      console.log(d.content.substring(0, 400) + "...\n");
    });
  } catch (e) {
    console.error("Failed", e);
  }
}

check();
