"""Shared state — set during app startup, used across modules."""

from telethon import TelegramClient

# Set in main.py lifespan, used by bot handlers for quick reply
telethon_client: TelegramClient | None = None
