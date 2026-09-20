#!/usr/bin/env node
/**
 * Set application version across all project files.
 *
 * Supported formats (see temp/release_guide.md for full channel docs):
 *   X.Y.Z                  → stable       (e.g. 0.5.0)
 *   X.Y.Z.W                → stable patch (e.g. 0.5.0.1)
 *   X.Y.ZbN / X.Y.Z.WbN    → beta         (e.g. 0.5.0b1, 0.5.0.2b3)
 *   X.Y.ZaN / X.Y.Z.WaN    → alpha        (e.g. 0.5.6a1) — a LOCAL marker for
 *                            builds that are not published: the tag-publish
 *                            workflow classifies stable and beta only and
 *                            refuses an alpha tag outright, which is the
 *                            intended answer (an alpha is not a channel).
 *
 * Touches backend/app/core/config.py, frontend/package.json, pyproject.toml
 * and frontend/package-lock.json. A file that is missing, or whose version
 * field this script can no longer find, is a hard failure — the 0.6.0 release
 * shipped a lockfile still reading 0.5.6b1 because a silent gap looks exactly
 * like a clean run.
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

// Validates the shapes listed above. Kept intentionally strict — an
// accidental "0.5.0-beta" or "0.5.0-rc1" would silently slip past upstream's
// pattern and break the Docker-publish channel detection later.
const VERSION_RE = /^\d+\.\d+\.\d+(\.\d+)?([ab]\d+)?$/;
if (!VERSION_RE.test(version)) {
  console.error(
    `Invalid version format: "${version}". Expected:\n` +
      `  X.Y.Z                  (stable)\n` +
      `  X.Y.Z.W                (stable patch)\n` +
      `  X.Y.ZbN / X.Y.Z.WbN    (beta)\n` +
      `  X.Y.ZaN / X.Y.Z.WaN    (alpha — local builds, never tagged)`,
  );
  process.exit(1);
}

const root = path.resolve(__dirname, "..");
const files = [
  {
    path: path.join(root, "backend/app/core/config.py"),
    pattern: /^(APP_VERSION\s*=\s*").+(")/m,
  },
  {
    path: path.join(root, "frontend/package.json"),
    pattern: /^(\s*"version"\s*:\s*").+(")/m,
  },
  {
    path: path.join(root, "pyproject.toml"),
    pattern: /^(version\s*=\s*").+(")/m,
  },
  {
    // npm keeps TWO fields in step with package.json: the document's own
    // "version" and the root entry `packages[""]`. Both sit above the first
    // dependency, so the first two matches are the right two — and `verify`
    // below is what proves it rather than trusting the count.
    //
    // Deliberately a surgical edit and not a JSON round-trip: the lockfile is
    // CRLF in a Windows checkout, and `JSON.stringify` would rewrite all 8k
    // lines to LF — burying the one real change in a whole-file diff.
    path: path.join(root, "frontend/package-lock.json"),
    pattern: /^(\s*"version"\s*:\s*").+(")/gm,
    limit: 2,
    verify: (text) => {
      const lock = JSON.parse(text);
      const rootPkg = lock.packages && lock.packages[""];
      if (lock.version !== version || !rootPkg || rootPkg.version !== version) {
        throw new Error(
          `version is "${lock.version}" and packages[""] is ` +
            `"${rootPkg && rootPkg.version}" — expected both to be "${version}"`,
        );
      }
    },
  },
];

// Every pattern above captures the text on either side of the value, so one
// replacer serves them all — and counting the matches is what lets an entry
// ask for exactly two of them. A global pattern still walks the whole file
// (the lockfile has a `"version"` for every dependency), so what is reported
// back is how many were REWRITTEN, not how many were seen.
function rewrite(file, content) {
  const limit = file.limit || 1;
  let seen = 0;
  const updated = content.replace(file.pattern, (match, before, after) =>
    ++seen <= limit ? `${before}${version}${after}` : match,
  );
  return { updated, applied: Math.min(seen, limit), limit };
}

let failed = false;

for (const file of files) {
  const rel = path.relative(root, file.path);
  if (!fs.existsSync(file.path)) {
    console.error(`  FAIL  ${rel} (not found)`);
    failed = true;
    continue;
  }
  const content = fs.readFileSync(file.path, "utf-8");
  const { updated, applied, limit } = rewrite(file, content);
  if (applied !== limit) {
    console.error(`  FAIL  ${rel} (rewrote the version field ${applied}×, expected ${limit})`);
    failed = true;
    continue;
  }
  if (file.verify) {
    try {
      file.verify(updated);
    } catch (err) {
      console.error(`  FAIL  ${rel} (${err.message})`);
      failed = true;
      continue;
    }
  }
  if (content === updated) {
    console.log(`  OK    ${rel} → ${version} (already)`);
    continue;
  }
  fs.writeFileSync(file.path, updated, "utf-8");
  console.log(`  OK    ${rel} → ${version}`);
}

if (failed) {
  console.error(`\nVersion NOT set — fix the files above and run again.`);
  process.exit(1);
}

console.log(`\nVersion set to ${version}`);
