import asyncio
import json

from app import db
from app.services import publishing
from app.utils import now_iso

from tests.helpers import FakeBot, seed_submission


def test_caption_formats_title_link_and_tag():
    collection = {"slug": "slovo", "is_ordered": 1, "season": 1}
    caption = publishing.build_caption(
        "Слово Винчестера 1 серия\nhttps://www.instagram.com/p/abc/\n#старый_тег",
        collection,
        1,
        5,
    )
    assert caption.startswith("<b>Слово Винчестера 1 серия</b>")
    assert '<i><a href="https://www.instagram.com/p/abc/">https://www.instagram.com/p/abc/</a></i>' in caption
    assert "#слoво" not in caption
    assert "#slovo · S1E5" in caption
    assert "#старый_тег" in caption


def test_caption_does_not_bold_url_first_line():
    caption = publishing.build_caption("https://example.com/video\nОбычный текст", None, None, None)
    assert "<b>" not in caption
    assert '<i><a href="https://example.com/video">' in caption


def test_caption_without_text_is_tag_only():
    collection = {"slug": "folder", "is_ordered": 0, "season": 1}
    assert publishing.build_caption(None, collection, None, None) == "#folder"


def test_link_submission_uses_title_on_skip():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            await db.execute(
                "UPDATE submissions SET source_url = ?, source_meta = ? WHERE id = ?",
                (
                    "https://www.youtube.com/watch?v=abcDEF12345",
                    json.dumps({"title": "Название из источника"}),
                    submission_id,
                ),
            )
            bot = FakeBot()
            await publishing.publish_submission(bot, submission_id, caption_text=None)
            post = await db.fetchone("SELECT * FROM posts WHERE submission_id = ?", (submission_id,))
            assert post["caption"].startswith("<b>Название из источника</b>")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_file_submission_without_caption_keeps_old_behavior():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            bot = FakeBot()
            await publishing.publish_submission(bot, submission_id, caption_text=None)
            post = await db.fetchone("SELECT * FROM posts WHERE submission_id = ?", (submission_id,))
            assert post["caption"] == ""
        finally:
            await db.close()

    asyncio.run(scenario())


def test_refresh_keyboard_restores_buttons_for_unordered_post():
    async def scenario():
        await db.init()
        try:
            bot = FakeBot()
            post = {
                "channel_id": -1001111111111,
                "message_id": 42,
                "collection_id": None,
                "season": None,
                "episode": None,
            }
            await publishing.refresh_keyboard(bot, post)
            assert bot.markups
            keyboard = bot.markups[-1][2]
            urls = [button.url for row in keyboard.inline_keyboard for button in row if button.url]
            assert any("start=submit" in url for url in urls)
            assert any("start=collections" in url for url in urls)
        finally:
            await db.close()

    asyncio.run(scenario())
