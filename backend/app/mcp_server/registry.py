"""SDK handlers for the base module, using the canonical packaged guidance."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from backend.app.mcp_server.contracts import DiagnosticResult, EmptyInput, GuideInput
from backend.app.mcp_server.limits import (
    CALLS_PER_MINUTE_PER_KEY,
    CONCURRENT_CALLS_PER_KEY,
    CONCURRENT_CALLS_PER_PROCESS,
)
from backend.app.services.queue_sources import current_epoch

GUIDE_PATH = (
    Path(__file__).resolve().parents[3]
    / "integrations"
    / "agent-skills"
    / "bamdude-diagnostics"
    / "references"
    / "diagnostics.md"
)


def usage_guide() -> str:
    with GUIDE_PATH.open("rb") as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise ValueError("guide_asset_too_large")
    return content.decode("utf-8")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]


TOOLS = (
    Tool("bamdude_get_capabilities", "Read the available BamDude modules, contracts and limits.", EmptyInput),
    Tool("bamdude_get_usage_guide", "Read BamDude's read-only diagnostic and connection guidance.", GuideInput),
)


def create_server():
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    from backend.app.core.config import APP_VERSION as app_version

    def access(ctx):
        return ctx.request.scope["bamdude_diagnostic_access"]

    def catalog(ctx):
        access(ctx)  # every SDK handler must have passed the request-local gate
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_model.model_json_schema(),
                output_schema=DiagnosticResult.model_json_schema(),
                annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False),
            )
            for tool in TOOLS
        ]

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=catalog(ctx))

    async def discover(ctx, params):
        return types.DiscoverResult(
            supported_versions=["2026-07-28"],
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(), resources=types.ResourcesCapability(), prompts=types.PromptsCapability()
            ),
            instructions="Available tools: " + ", ".join(tool.name for tool in catalog(ctx)),
        )

    async def call_tool(ctx, params):
        granted = access(ctx)
        scope = ctx.request.scope
        request_id = scope["bamdude_request_id"]

        def error(code):
            return types.CallToolResult(
                is_error=True,
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "code": code,
                                "request_id": request_id,
                                "message": code.replace("_", " "),
                                "retryable": code in {"busy", "state_changed"},
                            }
                        ),
                    )
                ],
            )

        tool = next((item for item in TOOLS if item.name == params.name), None)
        if tool is None:
            return error("not_found")
        try:
            tool.input_model.model_validate(params.arguments or {})
        except ValidationError:
            return error("invalid_arguments")
        if granted.epoch != current_epoch():
            return error("state_changed")
        if tool.name == "bamdude_get_capabilities":
            data = {
                "bamdude_version": app_version,
                "diagnostic_contract": 1,
                "skill_contract": 1,
                "enabled_modules": ["base"],
                "tool_names": [item.name for item in TOOLS],
                "auth": "api_key",
                "transport": "streamable_http",
                "timezone": "UTC",
                "limits": {
                    "page_default": 50,
                    "page_max": 100,
                    "response_bytes": 131072,
                    "concurrent_calls_per_key": CONCURRENT_CALLS_PER_KEY,
                    "concurrent_calls_per_process": CONCURRENT_CALLS_PER_PROCESS,
                    "calls_per_minute_per_key": CALLS_PER_MINUTE_PER_KEY,
                },
                "limitations": ["read_only", "diagnostics_module_not_installed"],
            }
        else:
            try:
                data = {"guide": usage_guide()}
            except Exception:
                return error("source_unavailable")
        result = DiagnosticResult(request_id=request_id, data=data)
        payload = result.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_dump_json())], structured_content=payload
        )

    async def resources(ctx, params):
        access(ctx)
        return types.ListResourcesResult(
            resources=[types.Resource(uri="bamdude://guide", name="BamDude usage guide", mime_type="text/markdown")]
        )

    async def read_resource(ctx, params):
        access(ctx)
        if str(params.uri) != "bamdude://guide":
            raise MCPError(-32602, "not_found")
        try:
            guide = usage_guide()
        except Exception:
            raise MCPError(-32603, "source_unavailable") from None
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, mime_type="text/markdown", text=guide)]
        )

    async def prompts(ctx, params):
        access(ctx)
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="bamdude_diagnose", description="Read the capabilities and diagnostic guidance first."
                )
            ]
        )

    async def get_prompt(ctx, params):
        access(ctx)
        if params.name != "bamdude_diagnose":
            raise MCPError(-32602, "not_found")
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text="Read BamDude capabilities and its usage guide before investigating. The base module alone cannot diagnose a farm.",
                    ),
                )
            ]
        )

    server = Server(
        "BamDude",
        version=app_version,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=resources,
        on_read_resource=read_resource,
        on_list_prompts=prompts,
        on_get_prompt=get_prompt,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)
    return server
