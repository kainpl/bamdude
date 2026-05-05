#!/usr/bin/env node
/**
 * Set application version across all project files.
 *
 * Supported formats (see temp/release_guide.md for full channel docs):
 *   X.Y.Z                  → stable       (e.g. 0.5.0)
 *   X.Y.Z.W                → stable patch (e.g. 0.5.0.1)
 *   X.Y.ZbN / X.Y.Z.WbN    → beta         (e.g. 0.5.0b1, 0.5.0.2b3)
 *
 * Usage: node scripts/set_version.js 0.5.0b1
 */

const fs = require("fs");
const path = require("path");

const version = process.argv[2];
if (!version) {
  console.error("Usage: node scripts/set_version.js <version>");
  console.error("Examples:");
  console.error("  node scripts/set_version.js 0.5.0       # stable");
  console.error("  node scripts/set_version.js 0.5.0b1     # beta milestone");
  process.exit(1);
}

// Validates the channels listed above. Kept intentionally strict — an
// accidental "0.5.0-beta" or "0.5.0-rc1" would silently slip past upstream's
// pattern and break the Docker-publish channel detection later.
const VERSION_RE = /^\d+\.\d+\.\d+(\.\d+)?(b\d+)?$/;
if (!VERSION_RE.test(version)) {
  console.error(
    `Invalid version format: "${version}". Expected:\n` +
      `  X.Y.Z                  (stable)\n` +
      `  X.Y.Z.W                (stable patch)\n` +
      `  X.Y.ZbN / X.Y.Z.WbN    (beta)`,
  );
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
