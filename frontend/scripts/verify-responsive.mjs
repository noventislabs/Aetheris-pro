/**
 * Real browser verification of the terminal at the target viewport widths.
 *
 * Opt-in and deliberately outside CI: it needs a running backend *and* a
 * running frontend, and CI must not depend on a venue being reachable. Run it
 * by hand:
 *
 *   # terminal 1
 *   cd backend && .venv/Scripts/python.exe -m uvicorn aetheris.main:app --port 8000
 *   # terminal 2
 *   cd frontend && pnpm build && pnpm start
 *   # terminal 3
 *   cd frontend && node scripts/verify-responsive.mjs
 *
 * What it actually checks, per page per width:
 *   - the page renders (no blank body)
 *   - no horizontal page overflow
 *   - no element overflows the viewport horizontally
 *   - no console errors and no unhandled page errors
 *   - key content is present
 *
 * Screenshots land in scripts/screenshots/ so a human can look at the result
 * rather than trusting a pass/fail line.
 */

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, "screenshots");

const BASE = process.env.TERMINAL_URL ?? "http://127.0.0.1:3000";

/** The widths the terminal is designed against. */
const VIEWPORTS = [
  { name: "1920", width: 1920, height: 1080 },
  { name: "1440", width: 1440, height: 900 },
  { name: "1366", width: 1366, height: 768 },
  { name: "820", width: 820, height: 1180 },
  { name: "390", width: 390, height: 844 },
];

const PAGES = [
  { path: "/markets", expect: ["AETHERIS", "OHLCV"] },
  { path: "/scanner", expect: ["AETHERIS", "Scanner"] },
  { path: "/backtest", expect: ["AETHERIS", "HISTORICAL SIMULATION"] },
];

let failures = 0;

function report(ok, label, detail = "") {
  if (!ok) failures += 1;
  const mark = ok ? "PASS" : "FAIL";
  console.log(`  [${mark}] ${label}${detail ? ` — ${detail}` : ""}`);
}

const browser = await chromium.launch();
await mkdir(SHOTS, { recursive: true });

try {
  for (const viewport of VIEWPORTS) {
    console.log(`\n=== ${viewport.width}x${viewport.height} ===`);
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });

    for (const target of PAGES) {
      const page = await context.newPage();
      const consoleErrors = [];
      const pageErrors = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) => pageErrors.push(String(error)));

      await page.goto(`${BASE}${target.path}`, { waitUntil: "networkidle" });
      // Data arrives from the backend after hydration; give the panels a beat.
      await page.waitForTimeout(2500);

      const label = `${target.path} @ ${viewport.name}`;

      const metrics = await page.evaluate(() => {
        const doc = document.documentElement;
        // An element inside a horizontally scrollable or clipping ancestor
        // legitimately extends past the viewport -- the nav scrolls on
        // purpose. Only unclipped overflow can push the page sideways.
        const isClipped = (el) => {
          for (let node = el.parentElement; node; node = node.parentElement) {
            const overflowX = getComputedStyle(node).overflowX;
            if (overflowX === "auto" || overflowX === "scroll" || overflowX === "hidden") {
              return true;
            }
          }
          return false;
        };
        const overflowing = [...document.querySelectorAll("*")]
          .filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.right > window.innerWidth + 1 && !isClipped(el);
          })
          .slice(0, 5)
          .map((el) => `${el.tagName.toLowerCase()}.${el.className || "(no class)"}`);
        return {
          scrollWidth: doc.scrollWidth,
          innerWidth: window.innerWidth,
          bodyText: (document.body.innerText || "").length,
          overflowing,
        };
      });

      report(metrics.bodyText > 200, `${label}: rendered`, `${metrics.bodyText} chars`);
      report(
        metrics.scrollWidth <= metrics.innerWidth + 1,
        `${label}: no page overflow`,
        `scrollWidth ${metrics.scrollWidth} vs viewport ${metrics.innerWidth}`,
      );
      report(
        metrics.overflowing.length === 0,
        `${label}: no element overflows`,
        metrics.overflowing.join(", "),
      );

      const text = await page.evaluate(() => document.body.innerText);
      for (const needle of target.expect) {
        report(text.includes(needle), `${label}: contains "${needle}"`);
      }

      report(consoleErrors.length === 0, `${label}: no console errors`, consoleErrors[0] ?? "");
      report(pageErrors.length === 0, `${label}: no page errors`, pageErrors[0] ?? "");

      await page.screenshot({
        path: join(SHOTS, `${target.path.replace(/\//g, "")}-${viewport.name}.png`),
        fullPage: false,
      });
      await page.close();
    }
    await context.close();
  }
} finally {
  await browser.close();
}

console.log(
  failures === 0
    ? "\nAll viewport checks passed."
    : `\n${failures} viewport check(s) FAILED.`,
);
process.exit(failures === 0 ? 0 : 1);
