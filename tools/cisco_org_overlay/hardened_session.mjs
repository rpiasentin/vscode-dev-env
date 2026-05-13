#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import readline from "node:readline/promises";
import { fileURLToPath } from "node:url";
import process from "node:process";
import { chromium } from "playwright-core";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..", "..");
const outputRoot = path.join(repoRoot, "output", "research", "cisco-org-overlay");
const profileDir = path.join(outputRoot, "browser-profile");
const storageStatePath = path.join(outputRoot, "storage-state.json");
const directoryHeadersPath = path.join(outputRoot, "directory-extra-headers.json");
const manifestPath = path.join(outputRoot, "session-manifest.json");

function buildTargets(profileAlias) {
  return [
    {
      name: "directory",
      url: `https://directory.cisco.com/find-people/profile/${profileAlias}`,
      responseMatch: `/api/directory/v2/profile/${profileAlias}`,
    },
    {
      name: "onesearch",
      url: "https://onesearch.cisco.com/searchpage/v1?queryFilter=org%20chart&taCategory=search",
    },
  ];
}

const headerAllowlist = new Set([
  "accept",
  "accept-language",
  "authorization",
  "cache-control",
  "origin",
  "pragma",
  "priority",
  "referer",
  "sec-ch-ua",
  "sec-ch-ua-mobile",
  "sec-ch-ua-platform",
  "sec-fetch-dest",
  "sec-fetch-mode",
  "sec-fetch-site",
  "user-agent",
  "x-requested-with",
]);

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

function filterHeaders(headers) {
  const filtered = {};
  for (const [key, value] of Object.entries(headers)) {
    const lower = key.toLowerCase();
    if (headerAllowlist.has(lower) && value) {
      filtered[key] = value;
    }
  }
  filtered.Referer = "https://directory.cisco.com/";
  filtered.Origin = "https://directory.cisco.com";
  return filtered;
}

async function saveJson(filePath, data) {
  await ensureDir(path.dirname(filePath));
  await fs.writeFile(filePath, JSON.stringify(data, null, 2));
}

async function main() {
  const rawArgs = process.argv.slice(2);
  const flagArgs = new Set();
  let profileAlias = "crobbins";

  for (let index = 0; index < rawArgs.length; index += 1) {
    const arg = rawArgs[index];
    if (arg === "--profile-alias") {
      profileAlias = rawArgs[index + 1] || profileAlias;
      index += 1;
      continue;
    }
    flagArgs.add(arg);
  }

  const autoMode = flagArgs.has("--auto");
  const headless = flagArgs.has("--headless");
  const targets = buildTargets(profileAlias);

  await ensureDir(outputRoot);
  await ensureDir(profileDir);

  const rl = autoMode
    ? null
    : readline.createInterface({ input: process.stdin, output: process.stdout });
  let capturedDirectoryHeaders = null;
  let lastDirectoryResponse = null;

  const context = await chromium.launchPersistentContext(profileDir, {
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless,
    viewport: { width: 1440, height: 960 },
  });

  context.on("response", async (response) => {
    const url = response.url();
    if (!url.includes("directory-gateway.cisco.com/api/directory/")) {
      return;
    }
    if (response.status() !== 200) {
      return;
    }
    if (!url.includes("/api/directory/v2/profile/")) {
      return;
    }
    capturedDirectoryHeaders = filterHeaders(response.request().headers());
    lastDirectoryResponse = {
      url,
      status: response.status(),
      capturedAt: new Date().toISOString(),
    };
  });

  const openedPages = [];
  for (const target of targets) {
    const page = await context.newPage();
    openedPages.push({ target, page });
    await page.goto(target.url, { waitUntil: "domcontentloaded" });
  }

  if (!autoMode) {
    console.log("");
    console.log("Cisco browser session is open.");
    console.log("Complete any required Cisco authentication in the browser windows.");
    console.log("When the directory page is fully loaded, press Enter here to continue.");
    await rl.question("> ");
  }

  for (const { target, page } of openedPages) {
    if (!target.responseMatch) {
      continue;
    }
    await page.goto(target.url, { waitUntil: "domcontentloaded" });
    await page.waitForResponse(
      (response) => response.url().includes(target.responseMatch) && response.status() === 200,
      { timeout: 90000 }
    );
  }

  await context.storageState({ path: storageStatePath });
  await saveJson(directoryHeadersPath, capturedDirectoryHeaders || {});
  await saveJson(manifestPath, {
    generatedAt: new Date().toISOString(),
    profileDir,
    storageStatePath,
    directoryHeadersPath,
    targets: targets.map((target) => ({
      name: target.name,
      url: target.url,
    })),
    profileAlias,
    lastDirectoryResponse,
    notes: [
      "Local-only persistent browser profile for Cisco internal crawling.",
      "If Directory API calls return 401/403 from the crawler, re-run this helper and refresh the captured headers.",
    ],
  });

  console.log("");
  console.log(`Saved storage state to ${storageStatePath}`);
  console.log(`Saved Directory headers to ${directoryHeadersPath}`);
  console.log(`Saved session manifest to ${manifestPath}`);
  console.log("Browser profile is persistent; you can reuse it for later refresh runs.");

  if (rl) {
    await rl.close();
  }
  await context.close();
}

main().catch(async (error) => {
  console.error(error);
  process.exitCode = 1;
});
