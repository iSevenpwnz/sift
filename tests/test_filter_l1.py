from src.app.processors.filter_l1 import should_process


class TestL1Filter:
    def test_passes_meeting(self):
        assert should_process({"content": "зустріч завтра о 14:00", "content_type": "text"})

    def test_passes_deadline(self):
        assert should_process({"content": "дедлайн до п'ятниці по PR review", "content_type": "text"})

    def test_passes_task(self):
        assert should_process({"content": "треба заревʼюїти PR #748", "content_type": "text"})

    def test_passes_mention(self):
        assert should_process({"content": "@sanerok подивись це", "content_type": "text"})

    def test_passes_time(self):
        assert should_process({"content": "давай о 15:30", "content_type": "text"})

    def test_skips_lol(self):
        assert not should_process({"content": "лол", "content_type": "text"})

    def test_skips_short(self):
        assert not should_process({"content": "ок", "content_type": "text"})

    def test_skips_sticker(self):
        assert not should_process({"content": "sticker", "content_type": "sticker"})

    def test_skips_bare_link(self):
        assert not should_process({"content": "https://example.com/path", "content_type": "text"})

    def test_passes_long_message(self):
        assert should_process({
            "content": "Привіт, я думаю нам треба обговорити архітектуру нового сервісу",
            "content_type": "text",
        })

    def test_skips_empty(self):
        assert not should_process({"content": "", "content_type": "text"})

    def test_photo_with_caption(self):
        assert should_process({"content": "зустріч о 14:00 в офісі", "content_type": "photo"})
