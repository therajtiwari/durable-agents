"""Ask an OpenAI-compatible provider which models it actually serves.

Providers rotate and decommission model names constantly, and most
return a bare 404 for an unknown one — indistinguishable from a wrong
URL. This asks the /models endpoint directly so you can pick a name
that exists right now.

Usage:
    $env:GROQ_API_KEY = "gsk_..."
    python examples/list_models.py
"""

import asyncio
import os
import sys

import httpx

BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")


async def main() -> None:
    if not API_KEY:
        print("Set GROQ_API_KEY (or OPENAI_API_KEY) first.")
        sys.exit(1)

    async with httpx.AsyncClient(
        base_url=BASE_URL.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30.0,
    ) as client:
        response = await client.get("models")
        print(f"GET {response.request.url} -> {response.status_code}")
        if response.is_error:
            print(response.text)
            sys.exit(1)

        models = sorted(m["id"] for m in response.json().get("data", []))
        print(f"\n{len(models)} models available:\n")
        for name in models:
            print(f"  {name}")


if __name__ == "__main__":
    asyncio.run(main())
