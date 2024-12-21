# YouTube_grup_bot

### Telegram бот для скачивания YouTube shorts в группах

<img alt="demo.gif" src="demo.gif" width="512"/>


---

### Используемый стек технологий

- **Python 3.11+**
    + aiogram (3.15)
    + yt-dlp
- **Docker + Docker Compose**

---

### Как запустить проект

#### 0. Получите токен телеграм бота

Создайте бота через [@BotFather](https://t.me/BotFather)

#### 1. Склонируйте репозиторий

Выполните команды:

```bash
git clone https://github.com/akarmain/YouTube_grup_bot
cd YouTube_grup_bot
```

#### 2. Создайте файл `.env`

Скопируйте файл `example.env` в `.env`:

```bash
cp example.env .env
```

#### 3. Запустите проект через Docker Compose

Соберите и запустите контейнеры:

```bash
docker-compose up --build
```

---

_Спасибо, что читаете мой код!_

