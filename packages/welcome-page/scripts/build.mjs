#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, cpSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const src = resolve(root, "src");
const dist = resolve(root, "dist");
const extDir = resolve(dist, "extension");

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });
mkdirSync(extDir, { recursive: true });

const compiledCss = resolve(dist, ".tmp.welcome.css");
const binDir = resolve(root, "node_modules/.bin");
const cliCandidates = [
  resolve(binDir, "tailwindcss"),
  resolve(binDir, "@tailwindcss/cli"),
];
const cliBin = cliCandidates.find((candidate) => existsSync(candidate));
if (!cliBin) {
  throw new Error(
    "Tailwind CLI binary not found. Run `pnpm install` inside packages/welcome-page first.",
  );
}
execFileSync(
  cliBin,
  [
    "-i", resolve(src, "welcome.css"),
    "-o", compiledCss,
    "--minify",
  ],
  { stdio: "inherit", cwd: root },
);

const css = readFileSync(compiledCss, "utf8").trim();
rmSync(compiledCss, { force: true });

const template = readFileSync(resolve(src, "welcome.template.html"), "utf8");
const html = template.replace("/* POLARIS_INLINE_CSS */", () => css);

writeFileSync(resolve(dist, "welcome.html"), html);
writeFileSync(resolve(extDir, "welcome.html"), html);

const manifest = {
  manifest_version: 3,
  name: "Polaris Welcome",
  version: "1.0.0",
  description: "Polaris new-tab override",
  chrome_url_overrides: { newtab: "welcome.html" },
  icons: { "128": "icon-128.png" },
};
writeFileSync(resolve(extDir, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");

// 128x128 solid-color PNG placeholder so the extension passes Chromium icon checks.
// Buffer generated once and committed as base64 to avoid adding a PNG dep.
const iconBase64 = [
  "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAAdklEQVR42u3SMQEAAAjDMO5fNCDQ",
  "AzCv7Yz6UwEGAAMAAAYAAwABgADAAGAAMAAYAAwABgADAAGAAMAAYAAwABgADAAGAAMAAYAAwABg",
  "ADAAGAAMAAYAAwABgADAAGAAMAAYAAwABgADAAGAAMAAYAAwAH3U+AOlfEb2wAAAAAElFTkSuQmCC",
].join("");
writeFileSync(resolve(extDir, "icon-128.png"), Buffer.from(iconBase64, "base64"));

console.log("welcome-page: built dist/welcome.html and dist/extension/");
