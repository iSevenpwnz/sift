import re

PASS_PATTERNS = [
    re.compile(r"\d{1,2}[:.]\d{2}"),
    re.compile(r"(зустріч|call|meeting|sync|standup|daily)", re.IGNORECASE),
    re.compile(r"(дедлайн|deadline|до п.ятниці|до кінця|до завтра|urgent)", re.IGNORECASE),
    re.compile(r"(задача|task|TODO|треба|потрібно|need to|please)", re.IGNORECASE),
    re.compile(r"@\w+"),
]

SKIP_PATTERNS = [
    re.compile(r"^(лол|хаха|gg|nice|ахах|lmao|lol)\s*$", re.IGNORECASE),
    re.compile(r"^https?://\S+$"),
]

SKIP_CONTENT_TYPES = frozenset({"sticker", "animation", "video_note"})

MIN_TEXT_LENGTH = 10


def should_process(message: dict) -> bool:
    """L1 filter: returns True if message should go to AI (L2)."""
    content_type = message.get("content_type", "text")
    if content_type in SKIP_CONTENT_TYPES:
        return False

    content = message.get("content", "").strip()
    if not content:
        return False

    # Check skip patterns first
    for pattern in SKIP_PATTERNS:
        if pattern.search(content):
            return False

    # Short messages without numbers are likely noise
    if len(content) < MIN_TEXT_LENGTH and not re.search(r"\d", content):
        return False

    # Check pass patterns — if any match, definitely process
    for pattern in PASS_PATTERNS:
        if pattern.search(content):
            return True

    # Default: process if message is long enough (might contain useful info)
    return len(content) >= 30
