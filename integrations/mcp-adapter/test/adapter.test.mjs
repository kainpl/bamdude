import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import https from 'node:https';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import selfsigned from 'selfsigned';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { configuration, connection, MAX_RESPONSE } from '../src/connection.mjs';

const entry = fileURLToPath(new URL('../src/index.mjs', import.meta.url));
const secret = 'bd_CANARY_never_print_this_key';
const tools = [{ name: 'bamdude_get_capabilities', inputSchema: { type: 'object' }, annotations: { readOnlyHint: true } },
  { name: 'bamdude_get_usage_guide', inputSchema: { type: 'object' }, annotations: { readOnlyHint: true } }];
const deferred = () => Promise.withResolvers();

async function fixture(t, tls) {
  const requests = [];
  const state = { mode: 'ok' };
  const serve = async (request, response) => {
    response.on('error', () => {});
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    let body;
    try { body = JSON.parse(Buffer.concat(chunks).toString()); } catch { /* GET */ }
    requests.push({ path: request.url, method: request.method, key: request.headers['x-api-key'], body });
    if (state.handler && await state.handler(request, response, body)) return;
    if (request.url !== '/mcp') { response.writeHead(404).end(); return; }
    if (request.method === 'GET') { response.writeHead(405).end(); return; }
    if (state.mode === 'revoked') { response.writeHead(401).end(secret); return; }
    if (!body?.id && body?.id !== 0) { response.writeHead(202).end(); return; }
    let result;
    if (body.method === 'initialize') result = {
      protocolVersion: '2025-11-25', capabilities: { tools: {}, resources: {}, prompts: {} }, serverInfo: { name: 'BamDude', version: 'test' },
    };
    else if (body.method === 'tools/list') result = { tools: state.tools ?? tools };
    else if (body.method === 'tools/call') result = {
      content: [{ type: 'text', text: body.params.name === 'bamdude_get_usage_guide' ? 'canonical guide' : 'capabilities' }],
      structuredContent: { data: { diagnostic_contract: state.contract ?? 1, skill_contract: 1, key_marker: request.headers['x-api-key'] === secret ? 'first' : 'second' } },
    };
    else if (body.method === 'resources/list') result = { resources: [{ uri: 'bamdude://guide', name: 'Guide' }] };
    else if (body.method === 'resources/read') result = { contents: [{ uri: 'bamdude://guide', text: 'canonical guide' }] };
    else if (body.method === 'prompts/list') result = { prompts: [{ name: 'bamdude_diagnose' }] };
    else if (body.method === 'prompts/get') result = { messages: [{ role: 'user', content: { type: 'text', text: 'Read capabilities' } }] };
    else { response.writeHead(400).end(); return; }
    response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }));
  };
  const server = tls ? https.createServer(tls, serve) : http.createServer(serve);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  return { url: `${tls ? 'https' : 'http'}://127.0.0.1:${server.address().port}/mcp`, requests, state, server };
}

async function client(t, fixture, key = secret) {
  const transport = new StdioClientTransport({
    command: process.execPath, args: [entry],
    env: { BAMDUDE_MCP_URL: fixture.url, BAMDUDE_MCP_KEY: key }, stderr: 'pipe',
  });
  let stderr = '';
  transport.stderr.on('data', chunk => { stderr += chunk; });
  const client = new Client({ name: 'adapter-test', version: '1' });
  client.onerror = () => {};
  t.after(() => client.close());
  await client.connect(transport, { timeout: 10000 });
  return { client, transport, stderr: () => stderr };
}

test('configuration allows only exact MCP endpoints and explicit TLS trust', () => {
  for (const url of ['http://farm.local/mcp', 'https://farm.local/api', 'https://user:pass@farm.local/mcp',
    'https://farm.local/mcp?key=secret', 'https://farm.local/mcp#secret', 'file:///mcp', 'https://farm.local/mcp/']) {
    assert.throws(() => configuration({ BAMDUDE_MCP_URL: url, BAMDUDE_MCP_KEY: secret }), /invalid_endpoint/);
  }
  for (const url of ['http://localhost/mcp', 'http://127.0.0.1:8000/mcp', 'http://[::1]/mcp', 'https://farm.local/mcp']) {
    assert.equal(configuration({ BAMDUDE_MCP_URL: url, BAMDUDE_MCP_KEY: secret }).endpoint.href, url);
  }
  assert.throws(() => configuration({ BAMDUDE_MCP_URL: 'https://farm.local/mcp', BAMDUDE_MCP_KEY: secret, NODE_TLS_REJECT_UNAUTHORIZED: '0' }), /tls_bypass_forbidden/);
  assert.throws(() => configuration({ BAMDUDE_MCP_URL: 'https://farm.local/mcp', BAMDUDE_MCP_KEY: 'bad\nkey' }), /invalid_key/);
});

test('real stdio host discovers tools, calls guide and reads resource/prompt; two keys stay separate', async t => {
  const farm = await fixture(t);
  const first = await client(t, farm);
  const second = await client(t, farm, 'bd_other_key');
  assert.deepEqual((await first.client.listTools()).tools, tools);
  const result = await first.client.callTool({ name: 'bamdude_get_capabilities' });
  assert.equal(result.structuredContent.data.key_marker, 'first');
  assert.equal((await second.client.callTool({ name: 'bamdude_get_capabilities' })).structuredContent.data.key_marker, 'second');
  assert.equal((await first.client.callTool({ name: 'bamdude_get_usage_guide' })).content[0].text, 'canonical guide');
  assert.equal((await first.client.listResources()).resources[0].uri, 'bamdude://guide');
  assert.equal((await first.client.readResource({ uri: 'bamdude://guide' })).contents[0].text, 'canonical guide');
  assert.equal((await first.client.listPrompts()).prompts[0].name, 'bamdude_diagnose');
  assert.equal((await first.client.getPrompt({ name: 'bamdude_diagnose' })).messages[0].content.text, 'Read capabilities');
  assert.ok(farm.requests.every(request => request.path === '/mcp'));
  assert.equal(first.stderr(), '');
  assert.equal(second.stderr(), '');
});

test('all redirect statuses, even same-origin, are refused without reaching the target', async t => {
  const farm = await fixture(t);
  const upstream = connection(configuration({ BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret }));
  t.after(() => upstream.close());
  for (const status of [300, 301, 302, 303, 304, 305, 307, 308]) {
    farm.state.handler = (_, response) => { response.writeHead(status, { location: '/stolen' }).end(); return true; };
    await assert.rejects(upstream.fetch(farm.url, { method: 'POST', body: '{}' }), /redirect_forbidden/);
  }
  assert.equal(farm.requests.length, 8);
  assert.ok(farm.requests.every(request => request.path === '/mcp'));
});

test('401 OAuth metadata and response-body canaries never trigger discovery or leak', async t => {
  const farm = await fixture(t);
  farm.state.handler = (_, response) => {
    response.writeHead(401, { 'www-authenticate': `Bearer resource_metadata="${farm.url.replace('/mcp', '/oauth')}"` }).end(secret);
    return true;
  };
  await assert.rejects(client(t, farm), error => !String(error).includes(secret));
  assert.equal(farm.requests.length, 1);
  assert.equal(farm.requests[0].path, '/mcp');
});

test('revocation is checked on the next call; hidden/writable tools are not callable', async t => {
  const farm = await fixture(t);
  const host = await client(t, farm);
  farm.state.tools = [...tools, { name: 'do_not_run', inputSchema: { type: 'object' }, annotations: { readOnlyHint: false } }];
  assert.deepEqual((await host.client.listTools()).tools, tools);
  await assert.rejects(host.client.callTool({ name: 'do_not_run' }), /tool_not_available/);
  assert.ok(!farm.requests.some(request => request.body?.params?.name === 'do_not_run'));
  farm.state.mode = 'revoked';
  await assert.rejects(host.client.callTool({ name: 'bamdude_get_usage_guide' }), /unauthorized/);
  assert.ok(!host.stderr().includes(secret));
});

test('self-signed TLS refuses by default, accepts explicit CA, and refuses a changed certificate', async t => {
  const options = { algorithm: 'sha256', extensions: [{ name: 'subjectAltName', altNames: [{ type: 7, ip: '127.0.0.1' }] }] };
  const certificate = await selfsigned.generate([{ name: 'commonName', value: 'adapter-test' }], options);
  const changed = await selfsigned.generate([{ name: 'commonName', value: 'adapter-test-changed' }], options);
  const directory = await mkdtemp(join(tmpdir(), 'bamdude-adapter-ca-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const caFile = join(directory, 'ca.pem');
  await writeFile(caFile, certificate.cert);
  const farm = await fixture(t, { key: certificate.private, cert: certificate.cert });
  const untrusted = connection(configuration({ BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret }));
  t.after(() => untrusted.close());
  await assert.rejects(untrusted.fetch(farm.url, { method: 'POST', body: '{}' }), /network_or_tls_error/);
  const trusted = connection(configuration({ BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret, BAMDUDE_MCP_CA_FILE: caFile }));
  t.after(() => trusted.close());
  assert.equal((await trusted.fetch(farm.url, { method: 'POST', body: '{}' })).status, 202);
  await trusted.close();
  farm.server.setSecureContext({ key: changed.private, cert: changed.cert });
  const mismatch = connection(configuration({ BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret, BAMDUDE_MCP_CA_FILE: caFile }));
  t.after(() => mismatch.close());
  await assert.rejects(mismatch.fetch(farm.url, { method: 'POST', body: '{}' }), /network_or_tls_error/);
});

test('oversized response is bounded and the error does not contain server secrets', async t => {
  const farm = await fixture(t);
  const host = await client(t, farm);
  farm.state.handler = (_, response, body) => {
    if (body?.method !== 'tools/call') return false;
    response.writeHead(200, { 'content-type': 'application/json' }).end(secret + 'x'.repeat(MAX_RESPONSE));
    return true;
  };
  await assert.rejects(host.client.callTool({ name: 'bamdude_get_usage_guide' }), /response_too_large/);
  assert.ok(!host.stderr().includes(secret));
});

test('cancellation aborts the actual HTTP request and frees its slot', async t => {
  const farm = await fixture(t);
  const host = await client(t, farm);
  const entered = deferred(), disconnected = deferred();
  farm.state.handler = (_, response, body) => {
    if (body?.method !== 'tools/call') return false;
    response.once('close', disconnected.resolve);
    entered.resolve();
    return true;
  };
  const abort = new AbortController();
  const call = host.client.callTool({ name: 'bamdude_get_usage_guide' }, undefined, { signal: abort.signal });
  const rejected = assert.rejects(call);
  await entered.promise;
  abort.abort();
  await rejected;
  await disconnected.promise;
  farm.state.handler = undefined;
  assert.equal((await host.client.listTools()).tools.length, 2);
});

test('two in-flight calls are bounded; a third receives busy without another HTTP call', async t => {
  const farm = await fixture(t);
  const host = await client(t, farm);
  const entered = deferred(), responses = [];
  farm.state.handler = (_, response, body) => {
    if (body?.method !== 'tools/call') return false;
    responses.push({ response, id: body.id });
    if (responses.length === 2) entered.resolve();
    return true;
  };
  const calls = [1, 2].map(() => host.client.callTool({ name: 'bamdude_get_usage_guide' }));
  await entered.promise;
  await assert.rejects(host.client.listTools(), /busy/);
  assert.equal(responses.length, 2);
  for (const { response, id } of responses) response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ jsonrpc: '2.0', id, result: { content: [] } }));
  await Promise.all(calls);
});

test('oversized stdio message closes the child, and stdout remains protocol only', async t => {
  const farm = await fixture(t);
  const child = spawn(process.execPath, [entry], { env: { ...process.env, BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret }, stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.stdin.on('error', () => {});
  t.after(() => { if (child.exitCode === null) child.kill(); });
  child.stdin.write('x'.repeat(40000));
  const [code] = await once(child, 'exit');
  assert.notEqual(code, null);
  assert.equal(stdout, '');
  assert.ok(!stderr.includes(secret));
});

test('EOF during an HTTP call closes its socket and exits naturally without a kill', async t => {
  const farm = await fixture(t);
  const entered = deferred(), disconnected = deferred(), initialized = deferred();
  farm.state.handler = (_, response, body) => {
    if (body?.method === 'tools/call' && body.params.name === 'bamdude_get_usage_guide') {
      response.once('close', disconnected.resolve);
      entered.resolve();
      return true;
    }
    return false;
  };
  const child = spawn(process.execPath, [entry], { env: { ...process.env, BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret }, stdio: ['pipe', 'pipe', 'pipe'] });
  let output = '', stderr = '';
  child.stdout.on('data', chunk => { output += chunk; if (output.includes('"id":1')) initialized.resolve(); });
  child.stderr.on('data', chunk => { stderr += chunk; });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  const exited = once(child, 'exit');
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'raw-host', version: '1' } } }) + '\n');
  await initialized.promise;
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'bamdude_get_usage_guide' } }) + '\n');
  await entered.promise;
  child.stdin.end();
  await disconnected.promise;
  const [code, signal] = await exited;
  assert.equal(code, 0);
  assert.equal(signal, null);
  assert.ok(!stderr.includes(secret));
  for (const line of output.trim().split('\n')) assert.equal(JSON.parse(line).jsonrpc, '2.0');
});

test('a slow response reaches the fixed deadline, without retry or leaking its body', async t => {
  const farm = await fixture(t);
  const host = await client(t, farm);
  const disconnected = deferred();
  let attempts = 0;
  farm.state.handler = (_, response, body) => {
    if (body?.method !== 'tools/call') return false;
    attempts++;
    response.once('close', disconnected.resolve);
    return true;
  };
  await assert.rejects(host.client.callTool({ name: 'bamdude_get_usage_guide' }), /cancelled_or_timed_out|connection_failed/);
  await disconnected.promise;
  assert.equal(attempts, 1);
  assert.ok(!host.stderr().includes(secret));
});

test('incompatible contracts are refused before exposing local tools', async t => {
  const farm = await fixture(t);
  farm.state.contract = 2;
  await assert.rejects(client(t, farm));
  assert.ok(!farm.requests.some(request => request.body?.method === 'tools/list'));
});

test('a larger stdio response envelope returns an error and keeps the connection usable', async t => {
  const farm = await fixture(t);
  const child = spawn(process.execPath, [entry], { env: { ...process.env, BAMDUDE_MCP_URL: farm.url, BAMDUDE_MCP_KEY: secret }, stdio: ['pipe', 'pipe', 'pipe'] });
  t.after(() => { child.stdin.end(); if (child.exitCode === null) child.kill(); });
  child.stderr.resume();
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const send = message => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
  const read = async () => JSON.parse((await lines.next()).value);
  send({ id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'large-id-host', version: '1' } } });
  assert.equal((await read()).id, 1);
  send({ method: 'notifications/initialized' });
  farm.state.handler = (_, response, body) => {
    if (body?.method !== 'tools/call') return false;
    const wire = JSON.stringify({ jsonrpc: '2.0', id: body.id, result: { content: [{ type: 'text', text: 'x'.repeat(MAX_RESPONSE - 250) }] } });
    assert.ok(Buffer.byteLength(wire) < MAX_RESPONSE);
    response.writeHead(200, { 'content-type': 'application/json' }).end(wire);
    return true;
  };
  const largeId = 'id'.repeat(8000);
  send({ id: largeId, method: 'tools/call', params: { name: 'bamdude_get_usage_guide' } });
  const error = await read();
  assert.equal(error.id, largeId);
  assert.equal(error.error.message, 'response_too_large');
  send({ id: 3, method: 'tools/list' });
  assert.equal((await read()).result.tools.length, 2);
});
