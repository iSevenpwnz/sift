"""One-time script to generate Telethon StringSession.

Run locally (NOT on Railway):
    uv run python scripts/generate_session.py

Copy the output string to Railway env var TELETHON_SESSION.
"""

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(os.environ.get("TELEGRAM_API_ID", input("Enter TELEGRAM_API_ID: ")))
    api_hash = os.environ.get("TELEGRAM_API_HASH", input("Enter TELEGRAM_API_HASH: "))

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\n=== Your TELETHON_SESSION string ===\n")
        print(client.session.save())
        print("\nCopy the string above to your .env / Railway env vars.")


if __name__ == "__main__":
    asyncio.run(main())
