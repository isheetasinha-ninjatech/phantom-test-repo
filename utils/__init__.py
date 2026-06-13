"""
Ninja Squad AI Utilities
========================

Utility functions for interacting with AI models via the NinjaTech LiteLLM gateway.

Available modules:
    - litellm_client: Core client configuration and authentication
    - chat: Chat completion utilities (Claude, GPT, Gemini)
    - images: Image generation and editing utilities (gpt-image-2, Gemini Image)
    - video: Video generation utilities (Sora 2, Sora 2 Pro)
    - embeddings: Text embedding utilities
    - mcp: MCP tool discovery (146 tools across 4 services)

Quick Start:
    from utils.chat import chat
    response = chat("What is 2+2?")
    print(response)

    from utils.images import generate_image, edit_image
    path = generate_image("A sunset over mountains")
    path = edit_image(
        "Image 1 is a chair. Image 2 is a cat. Put the cat on the chair.",
        references=["chair.png", "cat.png"],
    )

    from utils.video import generate_video
    path = generate_video("A cat playing with yarn")

    from utils.embeddings import embed
    vector = embed("Hello world")

MCP Tools (CLI):
    python -m utils.mcp list          # 146 tools across 4 services
    python -m utils.mcp search hotel  # search by name/description
    python -m utils.mcp groups        # booking_com, flights, linkedin
    python -m utils.mcp info <name>   # detailed tool info

MCP Tools (Python):
    from utils.mcp import MCPClient
    async with MCPClient() as client:
        tools = await client.list_tools()
"""

from utils.litellm_client import get_config
