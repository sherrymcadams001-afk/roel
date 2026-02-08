#!/usr/bin/env node

const path = require('path');
const puppeteer = require('puppeteer-core');
const fs = require('fs');

async function main() {
  const chromePort = Number(process.env.CHROME_PORT || 9222);
  const outPath = path.resolve(process.cwd(), 'gemini_capture.png');

  console.log('Connecting to Chrome...');
  const versionRes = await fetch(`http://127.0.0.1:${chromePort}/json/version`);
  if (!versionRes.ok) throw new Error(`Failed to fetch /json/version: ${versionRes.status}`);
  const version = await versionRes.json();

  const browser = await puppeteer.connect({
    browserWSEndpoint: version.webSocketDebuggerUrl,
    defaultViewport: null,
    protocolTimeout: 30000,
  });

  try {
    console.log('Fetching targets...');
    const targets = await browser.targets();
    console.log(`Found ${targets.length} targets.`);
    
    // Look for Gemini
    const target = targets.find(t => t.type() === 'page' && String(t.url()).includes('gemini.google.com'));
    
    if (!target) {
        console.log('Gemini tab NOT MATCHED by URL filter.');
        console.log('Available tabs:');
        for (const t of targets) {
            if (t.type() === 'page') console.log(` - ${t.url()}`);
        }
        throw new Error('Gemini tab not found');
    }

    console.log(`Found Gemini target: ${target.url()}`);
    const page = await target.page();
    if (!page) throw new Error('Could not get page object');

    console.log('Bringing to front...');
    await page.bringToFront();
    
    console.log('Taking screenshot...');
    await page.screenshot({ path: outPath });
    console.log(`Screenshot saved to ${outPath}`);

  } finally {
    browser.disconnect();
  }
}

main().catch(err => {
    console.error('ERROR:', err);
    process.exit(1);
});
