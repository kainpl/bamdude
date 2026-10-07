/**
 * Dependency guard of the part-render bundle (spec §5.5, §6.3; consilium N5, R5.2).
 * The Node permission model of our pin does not restrict the network, so what
 * keeps the script off it is that our own bundle never reaches for it. Two
 * checks, both run by the build and by tests:
 *   checkChunk -- the bundler's own module graph: external imports and source
 *                 modules against an allowlist;
 *   checkCode  -- the emitted code's AST: no dynamic import, require,
 *                 getBuiltinModule / binding / dlopen, network or code-loading
 *                 globals -- read directly, as a property of a global object,
 *                 or destructured.
 * This is a regression guard on trusted code, not a sandbox: a computed
 * property name or a chain of aliases can always hide a global from a static scan.
 */
import { parseAst } from 'rolldown/parseAst';

export const ALLOWED_IMPORTS = ['node:crypto', 'node:zlib'];
export const ALLOWED_SOURCES = [
  /[\\/]src[\\/]part-render[\\/][\w-]+\.ts$/,
  /[\\/]src[\\/]lib[\\/]gcodeToolpath\.ts$/,
  /[\\/]src[\\/]lib[\\/]vendor[\\/]toolpathRenderer\.js$/,
];
const FORBIDDEN_GLOBALS = new Set([
  'require', 'fetch', 'WebSocket', 'XMLHttpRequest', 'EventSource', 'Worker', 'SharedWorker',
  'importScripts', 'eval', 'Function', 'WebAssembly',
]);
const FORBIDDEN_MEMBERS = new Set(['getBuiltinModule', 'binding', '_linkedBinding', 'dlopen', 'createRequire', 'fetch']);
/** Objects whose properties ARE the globals: `globalThis.WebSocket` is `WebSocket`. */
const GLOBAL_OBJECTS = new Set(['globalThis', 'global', 'self', 'window']);

export function checkChunk(chunk) {
  const problems = [];
  for (const spec of chunk.imports) if (!ALLOWED_IMPORTS.includes(spec)) problems.push(`import ${spec}`);
  for (const spec of chunk.dynamicImports) problems.push(`dynamic import ${spec}`);
  for (const id of chunk.moduleIds) if (!ALLOWED_SOURCES.some((re) => re.test(id))) problems.push(`module ${id}`);
  return problems;
}

/** Identifiers in reference position only: not a property name or an object key. */
function isReference(parent, key) {
  if (!parent) return true;
  if (parent.type === 'MemberExpression' && key === 'property' && !parent.computed) return false;
  const keyed = parent.type === 'Property' || parent.type === 'MethodDefinition' || parent.type === 'PropertyDefinition';
  return !(keyed && key === 'key' && !parent.computed);
}

/** The name of a non-computed property key: `fetch` and `'fetch'` alike. */
function keyName(property) {
  if (property.computed) return null;
  if (property.key.type === 'Identifier') return property.key.name;
  return typeof property.key.value === 'string' ? property.key.value : null;
}

const forbidden = (name) => FORBIDDEN_GLOBALS.has(name) || FORBIDDEN_MEMBERS.has(name);

export function checkCode(code) {
  const problems = [];
  const visit = (node, parent, key) => {
    if (!node || typeof node.type !== 'string') return;
    if (node.type === 'ImportExpression') problems.push('dynamic import()');
    const declaresSource = node.type === 'ImportDeclaration' || node.type === 'ExportAllDeclaration' || node.type === 'ExportNamedDeclaration';
    if (declaresSource && node.source && !ALLOWED_IMPORTS.includes(node.source.value)) problems.push(`import ${node.source.value}`);
    if (node.type === 'Identifier' && FORBIDDEN_GLOBALS.has(node.name) && isReference(parent, key)) problems.push(`global ${node.name}`);
    if (node.type === 'MemberExpression' && !node.computed && node.property.type === 'Identifier') {
      const name = node.property.name;
      const ofGlobal = node.object.type === 'Identifier' && GLOBAL_OBJECTS.has(node.object.name);
      if (FORBIDDEN_MEMBERS.has(name) || (ofGlobal && FORBIDDEN_GLOBALS.has(name))) problems.push(`member ${name}`);
    }
    if (node.type === 'ObjectPattern') {
      // `const {getBuiltinModule} = process`, `const {fetch: request} = globalThis` (consilium R5.2)
      for (const property of node.properties) {
        const name = property.type === 'Property' ? keyName(property) : null;
        if (name !== null && forbidden(name)) problems.push(`destructured ${name}`);
      }
    }
    for (const [k, v] of Object.entries(node)) {
      if (k === 'type' || k === 'start' || k === 'end' || k === 'range' || k === 'loc') continue;
      if (Array.isArray(v)) for (const c of v) visit(c, node, k);
      else if (v && typeof v === 'object') visit(v, node, k);
    }
  };
  visit(parseAst(code), null, null);
  return [...new Set(problems)];
}
