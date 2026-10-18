import asyncio
import logging
import sqlite3
import aiohttp
import xml.etree.ElementTree as ET

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8764943717:AAGiNmkBaIr2bz41soxW4UeHYG2jZv5Zz0E"
GROUP_ID = "@PoteranSusheter"

# RSS лента канала YouTube (работает без блокировок и куки)
YOUTUBE_CHANNEL_ID = "UCVIQOnICNIBcLshYisdechQ"
YOUTUBE_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

BOOSTY_BLOG_NAME = "darktaka"
BOOSTY_POSTS_API = f"https://api.boosty.to/v1/blog/{BOOSTY_BLOG_NAME}/post/?limit=3"
BOOSTY_BLOG_API = f"https://api.boosty.to/v1/blog/{BOOSTY_BLOG_NAME}/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


# ================= БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect('bot_database.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posted_videos (
                video_id TEXT PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posted_boosty (
                post_id TEXT PRIMARY KEY
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


# ================= ПОИСК КАРТИНОК BOOSTY =================
def extract_boosty_image(post_data: dict) -> str | None:
    blocks = (post_data.get('teaser') or []) + (post_data.get('data') or [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for key in ('preview', 'previewUrl', 'url'):
            val = block.get(key)
            if isinstance(val, str) and val.startswith('http'):
                if any(ext in val.lower() for ext in ('.jpg', '.jpeg', '.png', '.webp', 'images.boosty.to')):
                    return val

    found_urls = []
    def DeepScan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
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


# ================= ОТПРАВКА В TELEGRAM =================
async def send_tg_photo(photo_url: str, caption: str) -> bool:
    tg_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    timeout = aiohttp.ClientTimeout(total=35)
    connector = aiohttp.TCPConnector(ssl=False)

    dl_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://boosty.to/"
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(photo_url, headers=dl_headers) as img_resp:
                if img_resp.status != 200:
                    return False
                image_bytes = await img_resp.read()

            form = aiohttp.FormData()
            form.add_field('chat_id', GROUP_ID)
            form.add_field('caption', caption)
            form.add_field('parse_mode', 'HTML')
            form.add_field('photo', image_bytes, filename='preview.jpg', content_type='image/jpeg')

            async with session.post(tg_api_url, data=form) as resp:
                return resp.status == 200
    except Exception as e:
        logging.warning(f"⚠️ Ошибка отправки фото в Telegram: {e}")
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


# ================= ПАРСИНГ YOUTUBE (ЧЕРЕЗ RSS) =================
async def check_youtube():
    logging.info("🔍 YouTube: Проверка новых роликов через RSS...")
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(YOUTUBE_RSS_URL) as resp:
                if resp.status == 200:
                    xml_data = await resp.text()
                    root = ET.fromstring(xml_data)
                    ns = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

                    entry = root.find('atom:entry', ns)
                    if entry is not None:
                        video_id = entry.find('yt:videoId', ns).text
                        title = entry.find('atom:title', ns).text
                        uploader = root.find('atom:title', ns).text
                        video_url = f"https://youtu.be/{video_id}"

                        if not is_item_posted('posted_videos', video_id):
                            text = (
                                f"🎬 <b>{title}</b>\n\n"
                                f"👤 Автор: {uploader}\n\n"
                                f"🔗 Смотреть: {video_url}"
                            )

                            maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
                            hq_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

                            sent = await send_tg_photo(maxres_url, text)
                            if not sent:
                                sent = await send_tg_photo(hq_url, text)

                            if sent:
                                mark_item_posted('posted_videos', video_id)
                                logging.info(f"✅ YouTube: Опубликован новый ролик — {title}")
                        else:
                            logging.info("💤 YouTube: Новых роликов нет (уже в базе).")
                else:
                    logging.error(f"❌ Ошибка получения RSS YouTube. Код: {resp.status}")
    except Exception as e:
        logging.error(f"❌ Ошибка при проверке YouTube через RSS: {e}")


# ================= ПАРСИНГ BOOSTY =================
async def check_boosty():
    logging.info("🔍 Boosty: Проверка новых постов...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
                logging.warning(f"⚠️ Ошибка получения количества подписчиков Boosty: {e}")

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

                            preview_url = extract_boosty_image(latest_post)
                            if preview_url:
                                logging.info(f"🖼 Boosty: Найдено изображение поста -> {preview_url}")
                            else:
                                logging.warning("⚠️ Boosty: В посте нет доступных картинок, отправляю текстом.")

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
                    logging.error(f"❌ Ошибка доступа к API Boosty. Код: {resp.status}")
    except Exception as e:
        logging.error(f"❌ Критическая ошибка при работе с Boosty: {e}")


# ================= ОДНОКРАТНЫЙ ЗАПУСК ДЛЯ GITHUB ACTIONS =================
async def main():
    init_db()
    logging.info("💾 База данных подключена.")

    await check_youtube()
    await asyncio.sleep(2)
    await check_boosty()

    logging.info("✅ Проверка успешно завершена.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Бот остановлен.")
