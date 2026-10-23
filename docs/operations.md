# Эксплуатация и восстановление

Документ для ручных операций. Все команды выполняются на сервере бота.
Секреты (токен, `.env`) в команды и логи не подставлять.

## Где что лежит

- База: `./data/neuroslop.sqlite3` (контейнер: `/data/neuroslop.sqlite3`).
- Бэкапы: `./backups/neuroslop-<дата>.tar.gz`, хранятся 14 дней.
- Логи: `docker compose logs -f --tail=200`.

## Обновление

```bash
cd ~/neuroslop-bot
./deploy/backup.sh
git pull
docker compose up -d --build
docker compose logs --tail=50
```

Если после обновления бот не стартует — откат:

```bash
git log --oneline -3
git reset --hard <предыдущий-коммит>
docker compose up -d --build
```

Миграции применяются автоматически при старте. Откат кода **не откатывает**
схему БД: старый код совместим с новыми колонками, но после появления новых
данных восстановление старого бэкапа приведёт к их потере.

## Проверка бэкапа

`deploy/backup.sh` уже проверяет копию (`PRAGMA integrity_check` и наличие
ключевых таблиц). Дополнительно можно проверить архив вручную:

```bash
mkdir -p /tmp/nsl-check
tar -xzf backups/neuroslop-<дата>.tar.gz -C /tmp/nsl-check
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/nsl-check/neuroslop.sqlite3")
print(c.execute("PRAGMA integrity_check").fetchone()[0])
print(c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
PY
rm -rf /tmp/nsl-check
```

## Восстановление

```bash
./deploy/restore.sh backups/neuroslop-<дата>.tar.gz
```

Скрипт проверяет архив, останавливает бота, сохраняет текущую базу в
`data/pre-restore-<дата>/` и запускает контейнер. При неудачном запуске
возвращает старую базу.

## Диагностика незавершённых публикаций

`publishing_at` — это резерв заявки на публикацию. Если он остался заполненным,
значит публикация началась и не завершилась штатно. Автоповтор в таком случае
**запрещён**: сообщение могло уже уйти в канал.

1. Посмотреть незавершённые заявки:

```bash
docker compose exec -T bot python - <<'PY'
import sqlite3
c = sqlite3.connect("/data/neuroslop.sqlite3")
for row in c.execute("SELECT id, status, publishing_at FROM submissions WHERE publishing_at IS NOT NULL"):
    print(row)
for row in c.execute("SELECT id, submission_id, status, error FROM queue WHERE status = 'unknown'"):
    print("queue:", row)
PY
```

2. Проверить канал вручную: появился ли пост.

3. Если пост **есть**, но записи в БД нет:
   - перешли этот пост из канала боту и добавь в нужную подборку
     (создастся запись `posts` без привязки к заявке);
   - затем закрой заявку и очередь:

```bash
docker compose exec -T bot python - <<'PY'
import sqlite3
sid = 0  # id заявки
qid = 0  # id queue
c = sqlite3.connect("/data/neuroslop.sqlite3")
c.execute("UPDATE submissions SET publishing_at = NULL, status = 'approved' WHERE id = ?", (sid,))
c.execute("UPDATE queue SET status = 'done' WHERE id = ?", (qid,))
c.commit()
PY
```

4. Если поста **нет**:
   - снять резерв и вернуть заявку в работу:

```bash
docker compose exec -T bot python - <<'PY'
import sqlite3
sid = 0
qid = 0
c = sqlite3.connect("/data/neuroslop.sqlite3")
c.execute("UPDATE submissions SET publishing_at = NULL, status = 'pending' WHERE id = ?", (sid,))
c.execute("DELETE FROM queue WHERE id = ?", (qid,))
c.commit()
PY
```

   После этого заявку можно снова взять из «📥 Заявки».

5. При старте бот сам пишет в лог и админам, если нашёл `processing`-задачи или
   висящие резервы. Если пост уже есть в БД, задача помечается `done`, а заявка
   автоматически переводится в `approved`: резерв снимается, счётчик и номер
   серии корректируются только когда это доказуемо. Если поста нет — задача
   получает `unknown`, резерв сохраняется, автоповтора нет.

## Известные ограничения

- Проверка подписки при ошибке Telegram API считается пройденной (fail-open).
  Изменение поведения требует отдельного согласования.
- Healthcheck у контейнера нет: живость определяется по `docker compose ps` и
  логам.
- Приложение использует одно соединение SQLite; все запросы сериализуются.
  Для текущей нагрузки этого достаточно.

## Тесты

Локально, без production:

```bash
docker build -t neuroslop-test .
docker run --rm -v "$PWD:/src" -w /src neuroslop-test \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Тесты используют временные базы и фиктивный Telegram-бот; реальные сообщения
не отправляются.
