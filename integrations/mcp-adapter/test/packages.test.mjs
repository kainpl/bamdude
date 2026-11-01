import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, readFile, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import http from 'node:http';
import { once } from 'node:events';
import { unzipSync } from 'fflate';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { MANIFEST_SCHEMAS } from '@anthropic-ai/mcpb';
import { buildPackages } from '../../ai-plugins/build.mjs';

test('build products are deterministic, self-contained and share the canonical skill', async t => {
  const previousKey = process.env.BAMDUDE_MCP_KEY;
  process.env.BAMDUDE_MCP_KEY = 'bd_CANARY_never_ship_this_secret';
  t.after(() => { if (previousKey === undefined) delete process.env.BAMDUDE_MCP_KEY; else process.env.BAMDUDE_MCP_KEY = previousKey; });
  const root = await mkdtemp(join(tmpdir(), 'bamdude-ai-build-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const first = await buildPackages(join(root, 'first'));
  const second = await buildPackages(join(root, 'second'));
  assert.deepEqual(first, second);
  const canonical = await readFile(new URL('../../agent-skills/bamdude-diagnostics/SKILL.md', import.meta.url));
  const guide = await readFile(new URL('../../agent-skills/bamdude-diagnostics/references/diagnostics.md', import.meta.url));
  for (const [name, digest] of Object.entries(first.sha256)) {
    const bytes = await readFile(join(root, 'first', name));
    assert.equal(createHash('sha256').update(bytes).digest('hex'), digest);
    const files = unzipSync(bytes);
    assert.ok(Object.keys(files).every(path => !path.startsWith('/') && !path.includes('..') && !path.includes('\\')));
    assert.deepEqual(Buffer.from(files['skills/bamdude-diagnostics/SKILL.md']), canonical);
    assert.deepEqual(Buffer.from(files['skills/bamdude-diagnostics/references/diagnostics.md']), guide);
    assert.ok(!files['mcp.json'], 'portable secrets must not be invented');
    for (const contents of Object.values(files)) assert.ok(!Buffer.from(contents).toString('utf8').includes('CANARY_never_ship_this_secret'));
    if (name.endsWith('.mcpb')) {
      const manifest = MANIFEST_SCHEMAS['0.3'].parse(JSON.parse(Buffer.from(files['manifest.json'])));
      assert.equal(manifest.user_config.ca_file.default, '');
    }
    if (name === 'bamdude-claude-code.zip') {
      const manifest = JSON.parse(Buffer.from(files['.claude-plugin/plugin.json']));
      assert.equal(manifest.userConfig.ca_file.default, '');
    }
    if (files['server/index.cjs']) {
      const notices = JSON.parse(Buffer.from(files['THIRD_PARTY_NOTICES.json']));
      assert.ok(notices.some(item => item.name === '@modelcontextprotocol/sdk'));
      assert.ok(notices.every(item => item.text && item.license));
      assert.ok(files['source/integrations/ai-plugins/templates/claude-code.json']);
      assert.ok(files['source/integrations/mcp-adapter/package-lock.json']);
      await mkdir(join(root, 'standalone'), { recursive: true });
      await writeFile(join(root, 'standalone/index.cjs'), files['server/index.cjs']);
    }
  }
  // A bundled child outside node_modules must actually speak MCP, not just parse.
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    if (request.method === 'GET') { response.writeHead(405).end(); return; }
    const message = JSON.parse(Buffer.concat(chunks));
    if (!('id' in message)) { response.writeHead(202).end(); return; }
    const result = message.method === 'initialize'
      ? { protocolVersion: '2025-11-25', capabilities: { tools: {} }, serverInfo: { name: 'bundle-test', version: '1' } }
      : message.method === 'tools/list'
        ? { tools: [{ name: 'bamdude_get_capabilities', inputSchema: { type: 'object' }, outputSchema: { type: 'object', properties: { data: { type: 'object' } }, required: ['data'] }, annotations: { readOnlyHint: true } }] }
        : { content: [], structuredContent: { data: { diagnostic_contract: 1, skill_contract: 1 } } };
    response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const client = new Client({ name: 'bundle-host', version: '1' });
  t.after(() => client.close());
  await client.connect(new StdioClientTransport({ command: process.execPath, args: [join(root, 'standalone/index.cjs')],
    env: { BAMDUDE_MCP_URL: `http://127.0.0.1:${server.address().port}/mcp`, BAMDUDE_MCP_KEY: 'bd_CANARY_never_ship_this_secret', BAMDUDE_MCP_CA_FILE: '${user_config.ca_file}' }, stderr: 'pipe' }), { timeout: 10000 });
  assert.equal((await client.listTools()).tools.length, 1);
  assert.equal((await client.callTool({ name: 'bamdude_get_capabilities' })).structuredContent.data.diagnostic_contract, 1);
});
