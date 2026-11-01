/** SDK-owned stdio/HTTP bridge. It never reads farm files, REST or device APIs. */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema, ListToolsRequestSchema, ListResourcesRequestSchema, ReadResourceRequestSchema,
  ListPromptsRequestSchema, GetPromptRequestSchema, McpError, ErrorCode,
} from '@modelcontextprotocol/sdk/types.js';
import { configuration, connection, ConnectionError, requestContext, MAX_REQUEST, MAX_RESPONSE, DEADLINE_MS } from './connection.mjs';

const identity = { name: 'bamdude-local-connector', version: '0.1.0' };
let remote, upstream, local, stopping;
const safeCode = error => error instanceof ConnectionError ? error.code : 'connection_failed';
function stop(code = 0) {
  if (stopping) return stopping;
  process.exitCode = code;
  stopping = Promise.resolve().then(async () => {
    await Promise.allSettled([upstream?.close(), remote?.close(), local?.close()]);
    process.stdin.destroy();
  });
  return stopping;
}

async function main() {
  if (process.argv.length > 2) throw new ConnectionError('configuration_must_use_client_settings');
  const config = configuration(process.env);
  upstream = connection(config);
  remote = new Client(identity);
  // No authProvider: a 401 is a refusal, never an OAuth discovery attempt.
  const http = new StreamableHTTPClientTransport(config.endpoint, {
    fetch: upstream.fetch, reconnectionOptions: { maxRetries: 0 },
  });
  remote.onerror = () => {}; // SDK exceptions can contain remote text; never log them.
  await remote.connect(http, { timeout: DEADLINE_MS });
  const capabilityResult = await remote.callTool(
    { name: 'bamdude_get_capabilities', arguments: {} }, undefined, { timeout: DEADLINE_MS },
  );
  const capabilities = capabilityResult.structuredContent?.data;
  if (capabilityResult.isError || capabilities?.diagnostic_contract !== 1 || capabilities?.skill_contract !== 1) {
    throw new ConnectionError('incompatible_contract');
  }
  local = new Server(identity, { capabilities: { tools: {}, resources: {}, prompts: {} } });
  let active = 0;
  const run = handler => async (request, extra) => {
    if (active >= 2) throw new McpError(ErrorCode.InternalError, 'busy');
    active++;
    try {
      return await requestContext.run({ signal: extra.signal }, () => handler(request, {
        signal: extra.signal, timeout: DEADLINE_MS,
      }));
    } catch (error) {
      // Only our own finite error codes may cross the boundary, not str(remote exception).
      throw new McpError(ErrorCode.InternalError, safeCode(error));
    } finally { active--; }
  };
  const tools = async options => {
    const result = await remote.listTools(undefined, options);
    return { ...result, tools: result.tools.filter(tool => tool.annotations?.readOnlyHint === true) };
  };
  local.setRequestHandler(ListToolsRequestSchema, run((_, options) => tools(options)));
  local.setRequestHandler(CallToolRequestSchema, run(async (request, options) => {
    // Refresh discovery, not credentials or permission caches, before a direct call.
    if (!(await tools(options)).tools.some(tool => tool.name === request.params.name)) {
      throw new ConnectionError('tool_not_available');
    }
    return remote.callTool(request.params, undefined, options);
  }));
  local.setRequestHandler(ListResourcesRequestSchema, run((_, options) => remote.listResources(undefined, options)));
  local.setRequestHandler(ReadResourceRequestSchema, run((request, options) => remote.readResource(request.params, options)));
  local.setRequestHandler(ListPromptsRequestSchema, run((_, options) => remote.listPrompts(undefined, options)));
  local.setRequestHandler(GetPromptRequestSchema, run((request, options) => remote.getPrompt(request.params, options)));
  const stdio = new StdioServerTransport(process.stdin, process.stdout, { maxBufferSize: MAX_REQUEST });
  await local.connect(stdio);
  const receive = stdio.onmessage;
  const send = stdio.send.bind(stdio);
  const pending = new Set();
  stdio.onmessage = message => {
    if ('method' in message && 'id' in message) {
      if (pending.size >= 16 || pending.has(message.id)) { void stop(1); return; }
      pending.add(message.id);
    }
    receive(message);
  };
  stdio.send = async message => {
    if (Buffer.byteLength(JSON.stringify(message)) > MAX_RESPONSE) {
      // The host's request ID can be longer than the SDK's upstream ID.
      // Report envelope overflow as one bounded error, preserving the connection.
      if (!('method' in message) && 'id' in message) {
        message = { jsonrpc: '2.0', id: message.id, error: { code: ErrorCode.InternalError, message: 'response_too_large' } };
      } else { void stop(1); return; }
    }
    if (process.stdout.writableLength > 2 * MAX_RESPONSE) {
      void stop(1); return;
    }
    if (!('method' in message) && 'id' in message) pending.delete(message.id);
    await send(message);
  };
  local.onerror = () => { process.stderr.write('BamDude connector: invalid_protocol\n'); };
  const closed = stdio.onclose;
  stdio.onclose = () => { closed?.(); void stop(); };
}

process.stdin.once('end', () => { void stop(); });
process.stdin.once('error', () => { void stop(1); });
process.stdout.once('error', () => { void stop(1); });
process.once('SIGINT', () => { void stop(); });
process.once('SIGTERM', () => { void stop(); });
main().catch(async error => {
  process.stderr.write(`BamDude connector: ${safeCode(error)}\n`);
  await stop(1);
});
