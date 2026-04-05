# YouTube_TikTok_grup_tg_bot

### Telegram бот для скачивания YouTube Shorts и TikTok в чаты и каналы

<img alt="demo.gif" src="demo.gif" width="512"/>


---
### Используемый стек технологий

- **Python 3.11+**
    + aiogram (3.15)
    + yt-dlp
    + uv
- **Docker + Docker Compose**

---

### Как запустить проект

#### 0. Получите токен телеграм бота

Создайте бота через [@BotFather](https://t.me/BotFather)

#### 1. Склонируйте репозиторий

Выполните команды:

```bash
git clone https://github.com/akarmain/YouTube_TikTok_grup_tg_bot.git
cd YouTube_TikTok_grup_tg_bot
```

#### 2. Создайте файл `.env`

Скопируйте файл `example.env` в `.env`:

```bash
cp example.env .env
```

Укажите в `.env`:

- `TG_MAIN_BOT_TOKEN` - токен бота
- `TG_ADMIN_ID` - Telegram ID администратора (например, `912185600`)
- `TG_MAX_VIDEO_SIZE_MB` - лимит размера видео для проверки до скачивания (по умолчанию `49`)
- `TG_FFMPEG_THREADS` - число потоков `ffmpeg` для одной задачи перекодирования (по умолчанию `1`)
- `TG_FFMPEG_MAX_JOBS` - максимальное число одновременных задач `ffmpeg` (по умолчанию `1`)

#### 3.1. Для большей стабильности настройте YouTube cookies локально

```bash
cp bot/youTube/cookies.example.txt bot/youTube/cookies.txt
```

Заполните `bot/youTube/cookies.txt` своими cookies локально и не добавляйте этот файл в git.
[Подробнее](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)

#### 3.2. (Опционально) TikTok cookies

```bash
cp bot/tiktok/cookies.example.txt bot/tiktok/cookies.txt
```

Если у TikTok есть ограничения по региону/возрасту/частоте, локальные cookies могут повысить стабильность.

#### 3.3. Как включить работу в канале (включая закрытый ТГК)

1. Добавьте бота администратором в канал.
2. Выдайте право на публикацию сообщений (`Post messages`).
3. Публикуйте в канале пост с ссылкой YouTube/TikTok: бот отправит видеофайл в тот же канал.

#### 4. Установите зависимости через uv (локальный запуск)

```bash
uv venv
source .venv/bin/activate
uv pip sync requirements.txt
```

#### 5. Запуск локально

```bash
uv run python run.py
```

#### 6. Запустите проект через Docker Compose

Соберите и запустите контейнеры:

```bash
docker-compose up --build
```

---
### Поведение загрузки

- Поддерживаются ссылки YouTube (`youtube.com`, `youtu.be`) и TikTok (`tiktok.com`, `vm/vt.tiktok.com`).
- Бот обрабатывает ссылки в личных/групповых чатах и в `channel_post` (постах каналов).
- Ссылка может быть единственным текстом сообщения или частью текста/подписи.
- Бот выбирает лучшее доступное качество с приоритетом не ниже `420p` (если такой формат есть).
- Видео отправляется в Telegram как `video` (streaming), без подписи от бота.
- Для стабильного воспроизведения в Telegram Desktop (включая macOS) видео нормализуется в `H.264 + AAC`, `yuv420p`, `30fps`, `+faststart`.
- Для защиты сервера от перегрузки интенсивные задачи `ffmpeg` ограничены по числу потоков и количеству одновременных запусков.
- По команде `/send_db` администратор получает в личные сообщения JSON с пользователями, которые запускали `/start`.
- По команде `/cmd` администратор получает список доступных административных команд.
- Повторные ссылки отправляются мгновенно через сохраненный `file_id` (без повторного скачивания), ключи нормализуются по ID видео.
- Перед скачиванием бот проверяет размер выбранного формата и не загружает видео, если оно больше лимита `TG_MAX_VIDEO_SIZE_MB`.

---
### Безопасность GitHub

- Никогда не коммитьте `.env`, `bot/youTube/cookies.txt`, `bot/tiktok/cookies.txt`, `bot/database/*.log`, `bot/database/users_videos.json`.
- Перед `git push` проверьте изменения:

```bash
git status --short
git diff --name-only
```
- Если токен/куки уже утекли в историю git, отзовите (rotate) секреты и очистите историю репозитория перед публикацией.

---

_Спасибо, что читаете мой код!_
