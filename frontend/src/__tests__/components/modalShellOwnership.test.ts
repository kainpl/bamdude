/**
 * Every modal renders through `components/Modal.tsx`. A hand-rolled full-screen
 * overlay is how modals came to close on a click outside, each file copying the
 * last; this scan is what stops the next copy.
 *
 * Three rules, checked on every JSX opening tag in `src/**\/*.tsx`:
 * 1. `fixed inset-0` with a painted ground (any `bg-*` but `bg-transparent`,
 *    or `backdrop-blur`) is a modal → must be <Modal>.
 * 2. an `inset-0` overlay with a pointer handler is a click-outside → only a
 *    menu, popover, drawer, loading or viewer overlay may have one, and it says
 *    so on the line above: `// not-a-modal: menu` (or `{/* not-a-modal: menu *\/}`).
 * 3. a `fixed` element at z ≥ 50 in a file that does not portal into `body` is
 *    inside `#root`, which the stack marks inert while a modal is open, and
 *    `#root` opens no stacking context — so it paints over (or ties with) the
 *    modal layer while being dead. Below 50 or portalled, never both ways.
 *
 * Migration finished 2026-09 — there is no allowlist; a new offender is a new bug.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const SRC = join(process.cwd(), 'src');
const SHELL = 'components/Modal.tsx';
const MARKER = /(\/\/|\{\/\*)\s*not-a-modal:\s*(menu|popover|drawer|loading|viewer)\b/;
/** `z-50`, `z-[55]`, `z-[100]` — the modal layer and above. Not `z-[49]`, not `z-40`.
 *  The bracket form is delimited by its own `]`, never by `\b`: `]` and whatever
 *  follows it are both non-word, so a trailing `\b` there can never hold. */
const AT_MODAL_LAYER = /\bz-(?:50\b|\[(?:[5-9]\d|[1-9]\d{2,})\])/;

function walk(dir: string, out: string[] = []): string[] {
  for (const d of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, d.name);
    if (d.isDirectory()) {
      if (d.name !== '__tests__') walk(p, out);
    } else if (d.name.endsWith('.tsx')) {
      out.push(p);
    }
  }
  return out;
}

interface Tag {
  line: number;
  text: string;
  lineBefore: string;
}

/** Every JSX opening tag, from `<Tag` to its own `>`, honouring braces and strings. */
function openingTags(src: string): Tag[] {
  const lines = src.split('\n');
  const tags: Tag[] = [];
  const re = /<([A-Za-z][\w.]*)\b/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src))) {
    let i = m.index + m[0].length;
    let depth = 0;
    let str: string | null = null;
    while (i < src.length) {
      const c = src[i];
      if (str) {
        if (c === str && src[i - 1] !== '\\') str = null;
      } else if (c === '"' || c === "'" || c === '`') {
        str = c;
      } else if (c === '{') {
        depth += 1;
      } else if (c === '}') {
        depth -= 1;
      } else if (c === '>' && depth === 0) {
        break;
      }
      i += 1;
    }
    const text = src.slice(m.index, i + 1);
    if (text.length > 4000) continue;
    const line = src.slice(0, m.index).split('\n').length;
    tags.push({ line, text, lineBefore: lines[line - 2] ?? '' });
  }
  return tags;
}

function offenders(file: string, src: string): string[] {
  const out: string[] = [];
  for (const t of openingTags(src)) {
    // The shell's own tag: a header/children prop may carry a popover's click-catcher, which is that popover's to mark, not the shell's.
    if (/^<Modal\b/.test(t.text)) continue;
    if (!/className=/.test(t.text) || !/\binset-0\b/.test(t.text)) continue;
    if (MARKER.test(t.lineBefore)) continue;
    const fullScreen = /\bfixed\b/.test(t.text);
    // Any painted ground makes a full-screen overlay a modal — `bg-black/50`,
    // but also an opaque themed ground like the fullscreen camera's
    // `bg-bambu-dark-secondary`. A menu's click-catcher paints nothing.
    const ground = /\bbg-(?!transparent\b)|\bbackdrop-blur/.test(t.text);
    // A sibling backdrop is always the dimming kind.
    const dimming = /\bbg-black\b|\bbackdrop-blur/.test(t.text);
    const handler = /\bon(Click|MouseDown|PointerDown|TouchStart)=/.test(t.text);
    if (fullScreen && ground) {
      out.push(`${file}:${t.line} — hand-rolled modal overlay; render <Modal> from components/Modal.tsx`);
    } else if (handler && (fullScreen || (dimming && /\babsolute\b/.test(t.text)))) {
      out.push(
        `${file}:${t.line} — click-outside on an overlay; put "// not-a-modal: menu|popover|drawer|loading|viewer" on the line above, or use <Modal variant="lightbox">`,
      );
    }
  }
  return out;
}

/** The `className` attribute's own text — the string literal, or whatever the expression spells out. */
function classNameOf(tag: string): string {
  const m = /className=/.exec(tag);
  if (!m) return '';
  const at = m.index + m[0].length;
  const opener = tag[at];
  if (opener === '"' || opener === "'") {
    const end = tag.indexOf(opener, at + 1);
    return end === -1 ? tag.slice(at + 1) : tag.slice(at + 1, end);
  }
  if (opener !== '{') return '';
  let depth = 0;
  for (let i = at; i < tag.length; i += 1) {
    if (tag[i] === '{') depth += 1;
    else if (tag[i] === '}') {
      depth -= 1;
      if (depth === 0) return tag.slice(at + 1, i);
    }
  }
  return tag.slice(at + 1);
}

/**
 * Rule 3. A file with `createPortal(` is skipped whole: a body-portalled
 * subtree is outside `#root`, stays live under a modal and may legitimately
 * sit above the modal layer (a menu opened from a dialog). A `fixed` element
 * that is NOT portalled never may — `#root` is inert while a modal is open.
 * `fixed` is read from the `className`, so a `position: 'fixed'` style on a
 * portalled panel is not mistaken for one.
 *
 * Two accepted limits: the exemption is per FILE, not per element — a file with
 * any `createPortal(` in it is skipped whole, so an in-tree `fixed z-50` living
 * beside a portal in the same file is unguarded, and a file protected today
 * stays protected only until someone adds a portal to it — and `fixed` is read
 * from the `className` only, so a panel that goes `fixed` through an inline
 * `style={{ position: 'fixed' }}` is invisible to the rule.
 */
function overlaysAtModalLayer(file: string, src: string): string[] {
  if (/createPortal\(/.test(src)) return [];
  const out: string[] = [];
  for (const t of openingTags(src)) {
    const cls = classNameOf(t.text);
    if (!/\bfixed\b/.test(cls) || !AT_MODAL_LAYER.test(cls)) continue;
    out.push(
      `${file}:${t.line} — fixed overlay inside #root at or above the modal layer (z = 50 + stack position): lower it below 50 or portal it into body`,
    );
  }
  return out;
}

describe('modal shell ownership', () => {
  const files = walk(SRC)
    .map((p) => relative(SRC, p).split(sep).join('/'))
    .filter((f) => f !== SHELL);
  const sources = new Map(files.map((f) => [f, readFileSync(join(SRC, f), 'utf8')]));
  const byFile = new Map(files.map((f) => [f, offenders(f, sources.get(f) ?? '')]));
  const aboveByFile = new Map(files.map((f) => [f, overlaysAtModalLayer(f, sources.get(f) ?? '')]));

  it('every full-screen overlay outside the shell is a <Modal>, or is marked as not a modal', () => {
    const bad = files.flatMap((f) => byFile.get(f) ?? []);
    expect(bad).toEqual([]);
  });

  it('every fixed overlay inside #root sits below the modal layer', () => {
    const bad = files.flatMap((f) => aboveByFile.get(f) ?? []);
    expect(bad).toEqual([]);
  });
});
