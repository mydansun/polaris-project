/**
 * Playwright smoke tests for the Polaris IDE (Theia).
 *
 * Expects Theia to be running at http://127.0.0.1:3000/.
 * Run locally:  yarn build && yarn start &  →  yarn test
 * Run in Docker: the Dockerfile test stage handles startup.
 */

import { chromium } from 'playwright';

const BASE = process.env.THEIA_TEST_URL || 'http://127.0.0.1:3000';
const TIMEOUT = 20_000;

let browser;
let page;
let failures = 0;

function assert(condition, name) {
    if (condition) {
        console.log(`  PASS  ${name}`);
    } else {
        console.log(`  FAIL  ${name}`);
        failures++;
    }
}

try {
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage();

    // ── 1. HTTP 200 ──────────────────────────────────────────────────
    const response = await page.goto(BASE + '/', {
        waitUntil: 'networkidle',
        timeout: TIMEOUT,
    });
    assert(response?.status() === 200, 'HTTP 200 on /');

    // ── 2. Theia shell loads ─────────────────────────────────────────
    const shell = await page.waitForSelector('#theia-app-shell', {
        timeout: TIMEOUT,
    }).catch(() => null);
    assert(shell !== null, 'Theia app shell rendered');

    // Wait for UI to settle
    await page.waitForTimeout(3000);

    // ── 3. No "Cannot GET /" ─────────────────────────────────────────
    const bodyText = await page.textContent('body');
    assert(!bodyText.includes('Cannot GET'), 'No "Cannot GET /" error');

    // ── 4. No trust dialog ───────────────────────────────────────────
    assert(!bodyText.includes('trust the authors'), 'No workspace trust dialog');

    // ── 5. Custom welcome page ───────────────────────────────────────
    assert(bodyText.includes('Polaris IDE'), 'Welcome shows "Polaris IDE"');
    assert(bodyText.includes('Your workspace is ready'), 'Welcome shows "Your workspace is ready"');

    // ── 6. Explorer sidebar expanded ─────────────────────────────────
    const leftPanel = await page.$('#theia-left-side-panel');
    let sidebarWidth = 0;
    if (leftPanel) {
        const box = await leftPanel.boundingBox();
        sidebarWidth = box?.width ?? 0;
    }
    assert(sidebarWidth > 50, `Explorer sidebar expanded (${sidebarWidth}px)`);

    // ── Summary ──────────────────────────────────────────────────────
    console.log('');
    if (failures > 0) {
        console.log(`FAILED: ${failures} test(s) did not pass`);
        process.exit(1);
    } else {
        console.log('ALL TESTS PASSED');
    }
} catch (err) {
    console.error('Test runner error:', err.message);
    process.exit(1);
} finally {
    await browser?.close();
}
