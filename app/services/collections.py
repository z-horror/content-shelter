from app import db
from app.utils import now_iso, slugify


async def unique_slug(name: str) -> str:
    base = slugify(name)
    candidate = base
    counter = 2
    while await db.fetchval("SELECT 1 FROM collections WHERE slug = ?", (candidate,)):
        candidate = f"{base[:36]}_{counter}"
        counter += 1
    return candidate


async def create_collection(name: str, is_ordered: bool) -> int:
    position = (await db.fetchval("SELECT MAX(position) FROM collections", default=0) or 0) + 1
    slug = await unique_slug(name)
    cur = await db.execute(
        "INSERT INTO collections (name, slug, is_ordered, position, created_at) VALUES (?, ?, ?, ?, ?)",
        (name.strip()[:80], slug, 1 if is_ordered else 0, position, now_iso()),
    )
    return cur.lastrowid


async def next_episode(collection_id: int, season: int) -> int:
    current = await db.fetchval(
        "SELECT MAX(episode) FROM posts WHERE collection_id = ? AND season = ?",
        (collection_id, season),
        0,
    )
    return int(current or 0) + 1
