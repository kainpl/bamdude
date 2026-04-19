#!/usr/bin/env node
/**
 * Set application version across all project files.
 * Usage: node scripts/set_version.js 0.4.0
 */

const fs = require("fs");
const path = require("path");

const version = process.argv[2];
if (!version) {
  console.error("Usage: node scripts/set_version.js <version>");
  console.error("Example: node scripts/set_version.js 0.4.0");
  process.exit(1);
}

if (!/^\d+\.\d+\.\d+(\.\d+)?$/.test(version)) {
  console.error(`Invalid version format: "${version}". Expected: X.Y.Z or X.Y.Z.P`);
  process.exit(1);
}

const root = path.resolve(__dirname, "..");
const files = [
  {
    path: path.join(root, "backend/app/core/config.py"),
    pattern: /^(APP_VERSION\s*=\s*").+(")/m,
    replace: `$1${version}$2`,
  },
  {
    path: path.join(root, "frontend/package.json"),
    pattern: /^(\s*"version"\s*:\s*").+(")/m,
    replace: `$1${version}$2`,
  },
  {
    path: path.join(root, "pyproject.toml"),
    pattern: /^(version\s*=\s*").+(")/m,
    replace: `$1${version}$2`,
  },
];

for (const file of files) {
  const rel = path.relative(root, file.path);
  if (!fs.existsSync(file.path)) {
    console.warn(`  SKIP  ${rel} (not found)`);
    continue;
  }
  const content = fs.readFileSync(file.path, "utf-8");
  const updated = content.replace(file.pattern, file.replace);
  if (content === updated) {
    console.warn(`  SKIP  ${rel} (pattern not matched)`);
    continue;
  }
  fs.writeFileSync(file.path, updated, "utf-8");
  console.log(`  OK    ${rel} → ${version}`);
}

console.log(`\nVersion set to ${version}`);
