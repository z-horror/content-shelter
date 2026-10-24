import asyncio
import types

from aiogram.methods import CopyMessage

from app import db
from app.utils import now_iso


def copy_method():
    return CopyMessage(chat_id=1, from_chat_id=1, message_id=1)


class FakeBot:
    def __init__(self, copy_errors=None, delay=0.0, send_error=None):
        self.copy_errors = list(copy_errors or [])
        self.delay = delay
        self.copy_calls = 0
        self.deleted = []
        self.messages = []
        self.sent = []
        self.markups = []
        self.send_error = send_error

    async def copy_message(self, **kwargs):
        self.copy_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.copy_errors:
            error = self.copy_errors.pop(0)
            if error is not None:
                raise error
        return types.SimpleNamespace(message_id=1000 + self.copy_calls)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def edit_message_reply_markup(self, **kwargs):
        self.markups.append((kwargs.get("chat_id"), kwargs.get("message_id"), kwargs.get("reply_markup")))
        return None

    async def edit_message_caption(self, **kwargs):
        return None

    async def edit_message_text(self, **kwargs):
        return None

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def send_video(self, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(("video", kwargs.get("caption")))
        return types.SimpleNamespace(
            message_id=2000 + len(self.sent),
            video=types.SimpleNamespace(file_id="video-file-id", file_unique_id="video-unique-id"),
        )

    async def send_document(self, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(("document", kwargs.get("caption")))
        return types.SimpleNamespace(
            message_id=3000 + len(self.sent),
            document=types.SimpleNamespace(file_id="doc-file-id", file_unique_id="doc-unique-id"),
        )


async def seed_user(user_id=1, username="user", first_name="User"):
    await db.execute(
        "INSERT OR IGNORE INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, now_iso()),
    )


async def seed_submission(submission_id=1, user_id=1, status="pending"):
    await seed_user(user_id)
    await db.execute(
        "INSERT INTO submissions (id, user_id, source_chat_id, source_message_id, media_type, file_id, "
        "file_unique_id, status, admin_chat_id, admin_message_id, card_caption, created_at) "
        "VALUES (?, ?, ?, ?, 'video', 'file-id', 'unique-id', ?, -111, ?, 'card', ?)",
        (submission_id, user_id, user_id, 100 + submission_id, status, 50 + submission_id, now_iso()),
    )
    return submission_id


async def seed_collection(ordered=True, name="Тест"):
    cur = await db.execute(
        "INSERT INTO collections (name, slug, is_ordered, position, created_at) VALUES (?, ?, ?, 0, ?)",
        (name, f"test_{int(ordered)}", 1 if ordered else 0, now_iso()),
    )
    return cur.lastrowid


async def submission_row(submission_id=1):
    return await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
