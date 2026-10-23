from app.handlers import admin, user
from app.utils import format_local_day, format_local_short


def test_plural_forms():
    assert user._plural(1, "серия", "серии", "серий") == "серия"
    assert user._plural(3, "серия", "серии", "серий") == "серии"
    assert user._plural(11, "серия", "серии", "серий") == "серий"
    assert user._plural(21, "пост", "поста", "постов") == "пост"


def test_plain_title():
    assert user._plain_title("<b>Заголовок</b>\nhttps://example.com\n#tag") == "Заголовок"
    assert user._plain_title("https://example.com/x") is None
    assert user._plain_title("#тег\nтекст") is None
    assert user._plain_title(None) is None


def test_my_line_scheduled_and_approved():
    scheduled = {
        "id": 7,
        "status": "scheduled",
        "created_at": "2026-10-24 10:00:00",
        "publish_at": "2026-10-25 15:00:00",
        "sched_season": 1,
        "sched_episode": 5,
    }
    text, link = user._my_line(scheduled)
    assert text == "#7 · 🕓 25.10 18:00 · S1E5"
    assert link is None

    approved = {
        "id": 6,
        "status": "approved",
        "created_at": "2026-10-24 10:00:00",
        "post_published_at": "2026-10-24 12:00:00",
        "post_season": 1,
        "post_episode": 4,
        "post_message_id": 100,
    }
    text, link = user._my_line(approved)
    assert text == "#6 · ✅ 24.10 · S1E4"
    assert link == "https://t.me/testchan/100"


def test_render_collection_series_and_folder():
    series = {"name": "Слово", "slug": "slovo", "is_ordered": 1, "season": 1}
    rows = [{
        "message_id": 10,
        "media_type": "video",
        "caption": "cap",
        "season": 1,
        "episode": 1,
        "published_at": "2026-10-24 12:00:00",
        "username": "user",
        "first_name": "User",
    }]
    text = user._render_collection(series, rows, 1, 0)
    assert "📺 <b>Слово</b>" in text
    assert "1 серия" in text
    assert "S1E1 · 24.10 · @user" in text
    assert 'href="https://t.me/testchan/10"' in text

    folder = {"name": "Приколы", "slug": "prikol", "is_ordered": 0, "season": 1}
    rows = [{
        "message_id": 11,
        "media_type": "video",
        "caption": "<b>Смешное видео</b>\nhttps://example.com/x",
        "season": None,
        "episode": None,
        "published_at": "2026-10-24 12:00:00",
        "username": None,
        "first_name": None,
    }]
    text = user._render_collection(folder, rows, 1, 0)
    assert "📁 <b>Приколы</b>" in text
    assert "1 пост" in text
    assert "🎬 «Смешное видео»" in text
    assert "из канала" in text


def test_date_helpers():
    assert format_local_day("2026-10-24 12:00:00") == "24.10"
    assert format_local_short("2026-10-24 12:00:00") == "24.10 15:00"


def test_admin_helpers():
    assert admin._admin_plain_title("<b>Смешное</b>\nhttps://x") == "Смешное"
    assert admin._admin_plain_title("https://x") is None
    assert admin._admin_author({"username": "user", "first_name": "User"}) == "@user"
    assert admin._admin_author({"username": None, "first_name": "User"}) == "User"
    assert admin._admin_author({"username": None, "first_name": None}) == "из канала"
    assert admin._admin_plural(1, "серия", "серии", "серий") == "серия"
