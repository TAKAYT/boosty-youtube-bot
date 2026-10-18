import asyncio
import logging
import sqlite3
import yt_dlp
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8764943717:AAGiNmkBaIr2bz41soxW4UeHYG2jZv5Zz0E"
GROUP_ID = "-5283586638"

YOUTUBE_CHANNEL_URL = "https://www.youtube.com/channel/UCVIQOnICNIBcLshYisdechQ/videos"
BOOSTY_BLOG_NAME = "darktaka"
BOOSTY_POSTS_API = f"https://api.boosty.to/v1/blog/{BOOSTY_BLOG_NAME}/post/?limit=3"
BOOSTY_BLOG_API = f"https://api.boosty.to/v1/blog/{BOOSTY_BLOG_NAME}/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Защита от одновременного запуска и дублей
yt_lock = asyncio.Lock()
boosty_lock = asyncio.Lock()


# ================= БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect('bot_database.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS posted_videos
                       (
                           video_id
                           TEXT
                           PRIMARY
                           KEY
                       )
                       ''')
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS posted_boosty
                       (
                           post_id
                           TEXT
                           PRIMARY
                           KEY
                       )
                       ''')
        conn.commit()


def is_item_posted(table: str, item_id: str) -> bool:
    column = 'video_id' if table == 'posted_videos' else 'post_id'
    with sqlite3.connect('bot_database.db') as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (str(item_id),))
        return bool(cursor.fetchone())


def mark_item_posted(table: str, item_id: str):
    column = 'video_id' if table == 'posted_videos' else 'post_id'
    with sqlite3.connect('bot_database.db') as conn:
        cursor = conn.cursor()
        cursor.execute(f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)", (str(item_id),))
        conn.commit()


def format_duration(seconds) -> str:
    if not seconds:
        return "Неизвестно"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ================= ГЛУБОКИЙ ПОИСК ФОТО BOOSTY =================
def extract_boosty_image(post_data: dict) -> str | None:
    """Ищет любую картинку или превью видео во всех блоках поста Boosty"""
    # 1. Проверяем приоритетные блоки (тизер и основной контент)
    blocks = (post_data.get('teaser') or []) + (post_data.get('data') or [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        # Для видео-блоков сначала берем превью (чтобы не схватить ссылку на сам видеоплеер)
        for key in ('preview', 'previewUrl', 'url', ' rendition'):
            val = block.get(key)
            if isinstance(val, str) and val.startswith('http'):
                if any(ext in val.lower() for ext in
                       ('.jpg', '.jpeg', '.png', '.webp', 'images.boosty.to', 'ivideon.com')):
                    return val

    # 2. Если в стандартных полях нет — рекурсивно сканируем весь JSON поста на наличие картинок
    found_urls = []

    def DeepScan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                # Игнорируем аватарки самого блога, чтобы брать только фото из поста
                if k in ('avatarUrl', 'coverUrl', 'user'):
                    continue
                if isinstance(v, str) and v.startswith('http'):
                    if any(x in v.lower() for x in ('images.boosty.to', '.jpg', '.png', '.jpeg', '.webp')):
                        found_urls.append(v)
                else:
                    DeepScan(v)
        elif isinstance(obj, list):
            for item in obj:
                DeepScan(item)

    DeepScan(blocks)
    return found_urls[0] if found_urls else None


# ================= ОТПРАВКА В TELEGRAM (СКАЧИВАНИЕ ФОТО) =================
async def send_tg_photo(photo_url: str, caption: str) -> bool:
    """Скачивает картинку с обходом защиты Boosty/YouTube и загружает в Telegram"""
    tg_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    timeout = aiohttp.ClientTimeout(total=35)
    connector = aiohttp.TCPConnector(ssl=False)

    # Заголовки нужны, чтобы Boosty разрешил скачать картинку
    dl_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://boosty.to/"
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(photo_url, headers=dl_headers) as img_resp:
                if img_resp.status != 200:
                    logging.warning(f"⚠️ Сервер не отдал картинку (Код {img_resp.status}): {photo_url}")
                    return False
                image_bytes = await img_resp.read()

            form = aiohttp.FormData()
            form.add_field('chat_id', GROUP_ID)
            form.add_field('caption', caption)
            form.add_field('parse_mode', 'HTML')
            form.add_field('photo', image_bytes, filename='preview.jpg', content_type='image/jpeg')

            async with session.post(tg_api_url, data=form) as resp:
                if resp.status == 200:
                    return True
                else:
                    err_text = await resp.text()
                    logging.error(f"❌ Ошибка Telegram при отправке фото: {err_text}")
                    return False
    except Exception as e:
        logging.warning(f"⚠️ Ошибка при скачивании/отправке фото ({photo_url}): {e}")
        return False


async def send_tg_text(text: str) -> bool:
    tg_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(ssl=False)

    payload = {
        "chat_id": GROUP_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(tg_api_url, json=payload) as resp:
                return resp.status == 200
    except Exception as e:
        logging.error(f"❌ Ошибка отправки текста в Telegram: {e}")
        return False


# ================= ПАРСИНГ YOUTUBE =================
async def check_youtube():
    async with yt_lock:
        logging.info("🔍 YouTube: Проверка новых роликов...")
        ydl_opts = {
            'playlist_items': '1',
            'extract_flat': False,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 15
        }

        def fetch_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(YOUTUBE_CHANNEL_URL, download=False)

        try:
            info = await asyncio.to_thread(fetch_info)
            entries = info.get('entries', [])

            if entries:
                video = entries[0]
                video_id = video.get('id')

                if video_id and not is_item_posted('posted_videos', video_id):
                    title = video.get('title', 'Без названия')
                    uploader = video.get('uploader', 'Неизвестен')
                    duration = format_duration(video.get('duration'))
                    video_url = video.get('webpage_url', f"https://youtu.be/{video_id}")

                    text = (
                        f"🎬 <b>{title}</b>\n\n"
                        f"👤 Автор: {uploader}\n"
                        f"⏱ Длительность: {duration}\n\n"
                        f"🔗 Смотреть: {video_url}"
                    )

                    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
                    hq_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

                    sent = await send_tg_photo(maxres_url, text)
                    if not sent:
                        logging.info("🔄 YouTube: Переключаюсь на стандартное HD превью...")
                        sent = await send_tg_photo(hq_url, text)

                    if sent:
                        mark_item_posted('posted_videos', video_id)
                        logging.info(f"✅ YouTube: Опубликован новый ролик — {title}")
                else:
                    logging.info("💤 YouTube: Новых роликов нет (уже в базе).")
        except Exception as e:
            logging.error(f"❌ Ошибка при проверке YouTube: {e}")


# ================= ПАРСИНГ BOOSTY =================
async def check_boosty():
    async with boosty_lock:
        logging.info("🔍 Boosty: Проверка новых постов...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://boosty.to/{BOOSTY_BLOG_NAME}"
        }

        timeout = aiohttp.ClientTimeout(total=25)
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
                sub_text = ""
                try:
                    async with session.get(BOOSTY_BLOG_API) as stat_resp:
                        if stat_resp.status == 200:
                            blog_data = await stat_resp.json()
                            subscribers = blog_data.get('count', {}).get('subscribers', 0)
                            if subscribers > 0:
                                sub_text = f"👥 Нас уже <b>{subscribers}</b>! Присоединяйся к платным подписчикам и поддержи проект.\n\n"
                except Exception as e:
                    logging.warning(f"⚠️ Boosty: Статистика подписчиков временно недоступна ({e})")

                try:
                    async with session.get(BOOSTY_POSTS_API) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            posts = data.get('data', [])

                            if posts:
                                latest_post = posts[0]
                                post_id = latest_post.get('id')

                                if post_id and not is_item_posted('posted_boosty', post_id):
                                    title = latest_post.get('title') or "Новый эксклюзивный выпуск!"
                                    post_url = f"https://boosty.to/{BOOSTY_BLOG_NAME}/posts/{post_id}"

                                    # Ищем любое фото или превью в посте через глубокий сканер
                                    preview_url = extract_boosty_image(latest_post)
                                    if preview_url:
                                        logging.info(f"🖼 Boosty: Найдено изображение поста -> {preview_url}")
                                    else:
                                        logging.warning(
                                            "⚠️ Boosty: В посте вообще нет доступных картинок, отправляю текстом.")

                                    text = (
                                        f"🔥 <b>Новый ролик на Boosty!</b>\n"
                                        f"🎬 <b>{title}</b>\n\n"
                                        f"🔗 <b>Смотреть пост:</b> {post_url}\n\n"
                                        f"💎 <b>Уровни подписки для доступа:</b>\n"
                                        f"• <a href='https://boosty.to/darktaka/purchase/3988840?ssource=DIRECT&share=subscription_link'>Базовый уровень — 250 ₽</a>\n"
                                        f"• <a href='https://boosty.to/darktaka/purchase/3988841?ssource=DIRECT&share=subscription_link'>Основной уровень — 500 ₽</a>\n"
                                        f"• <a href='https://boosty.to/darktaka/purchase/3979820?ssource=DIRECT&share=subscription_link'>Дополнительный уровень — 1000 ₽</a>\n\n"
                                        f"{sub_text}"
                                        f"🎯 <b>Цель:</b> Стабильный выпуск роликов на бусти, оплата аренды, монтажёра, а также повышение количества глав в роликах на бусти с 5 -> 10 глав в ролике!"
                                    )

                                    sent = False
                                    if preview_url:
                                        sent = await send_tg_photo(preview_url, text)
                                    if not sent:
                                        sent = await send_tg_text(text)

                                    if sent:
                                        mark_item_posted('posted_boosty', post_id)
                                        logging.info(f"✅ Boosty: Опубликован новый пост — {title}")
                                else:
                                    logging.info("💤 Boosty: Новых постов нет (уже в базе).")
                        else:
                            logging.error(f"❌ Ошибка доступа к API Boosty. Код ответа: {resp.status}")
                except Exception as e:
                    logging.error(f"❌ Ошибка при загрузке постов Boosty: {e}")

        except Exception as e:
            logging.error(f"❌ Критическая ошибка сессии Boosty: {e}")


# ================= АВТОНОМНЫЙ ЗАПУСК 24/7 =================
async def main():
    init_db()
    logging.info("💾 База данных подключена и готова.")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_youtube, 'interval', minutes=15, max_instances=1)
    scheduler.add_job(check_boosty, 'interval', minutes=15, max_instances=1)
    scheduler.start()

    await check_youtube()
    await asyncio.sleep(2)
    await check_boosty()

    logging.info("🤖 Бот перешел в неубиваемый автономный режим 24/7!")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Бот остановлен вручную.")