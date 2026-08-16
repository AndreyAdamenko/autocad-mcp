# Copyright 2026 JIALE LIU
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A local, stdio-only MCP bridge for a running AutoCAD 2026 instance."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from .acad_core import HANDLERS, TOOLS, com_error, logger, result


server = Server("autocad-mcp")

# AutoCAD ActiveX is a single STA automation server.  Interleaving two drawing
# mutations can corrupt the implicit active document/selection state, so tools
# execute one at a time even if the MCP client sends concurrent requests.
COM_LOCK = asyncio.Lock()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    handler = HANDLERS.get(name)
    if handler is None:
        payload: dict[str, Any] = result(False, f"Unknown tool: {name}")
    else:
        async with COM_LOCK:
            try:
                payload = handler(arguments or {})
            except KeyError as exc:
                # str(KeyError) is the repr of the missing key, so on its own it
                # reads as a bare quoted word with no hint of what went wrong.
                payload = result(False, f"Missing required argument: {exc}")
            except (RuntimeError, ValueError) as exc:
                payload = result(False, str(exc))
            except Exception as exc:
                payload = com_error(exc)

    image_base64 = payload.pop("_image_png_base64", None) if isinstance(payload, dict) else None
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    ]
    if image_base64:
        content.append(ImageContent(type="image", data=image_base64, mimeType="image/png"))
    return content


async def main() -> None:
    logger.info("autocad-mcp starting with %d tools", len(TOOLS))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    """Console-script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
