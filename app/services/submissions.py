from app import db
from app.utils import post_link


async def duplicates(unique_id, exclude_submission_id=None):
    result = {"post": None, "pending": None}
    if not unique_id:
        return result
    result["post"] = await db.fetchone(
        "SELECT * FROM posts WHERE file_unique_id = ? AND is_deleted = 0 ORDER BY id DESC LIMIT 1",
        (unique_id,),
    )
    if exclude_submission_id is None:
        result["pending"] = await db.fetchone(
            "SELECT * FROM submissions WHERE file_unique_id = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (unique_id,),
        )
    else:
        result["pending"] = await db.fetchone(
            "SELECT * FROM submissions WHERE file_unique_id = ? AND status = 'pending' "
            "AND id != ? ORDER BY id LIMIT 1",
            (unique_id, exclude_submission_id),
        )
    return result


def duplicate_lines(found):
    lines = []
    post = found.get("post")
    if post:
        link = post_link(post["message_id"])
        if link:
            lines.append(f'⚠️ Уже было в канале: <a href="{link}">#{post["submission_id"]}</a>')
        else:
            lines.append("⚠️ Уже было в канале")
    pending = found.get("pending")
    if pending:
        lines.append(f"⚠️ Такая же заявка уже на модерации: #{pending['id']}")
    return lines
