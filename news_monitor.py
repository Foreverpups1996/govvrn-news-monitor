#!/usr/bin/env python3
"""
Монитор новостей govvrn.ru -> Telegram.

Работает по расписанию (GitHub Actions) или вручную.

Опции:
  --dry-run          только показать новые новости, ничего не отправлять и не сохранять
  --test             отправить в Telegram тестовое сообщение
  --find-chat        найти chat_id написавших боту пользователей (после /start)
  --send-latest N    отправить N последних новостей (для первого запуска/проверки)
  --pages N          сколько страниц ленты проверять (по умолчанию 2)

Переменные окружения:
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather (обязательна)
  TELEGRAM_CHAT_ID    числовой chat_id получателя (обязательна, кроме --find-chat)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.govvrn.ru"
FEED_URL = f"{BASE}/news/all"
STATE_FILE = Path(__file__).resolve().with_name("state.json")
MAX_SEEN = 800  # сколько ссылок хранить в состоянии, чтобы файл не рос вечно

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Ссылки вида /news/2026/10/15/13210
NEWS_LINK_RE = re.compile(r"^/news/\d{4}/\d{2}/\d{2}/\d+$")
# Дата вида 15.10.2026 14:53 (для новостей не старше суток сайт показывает «4 ч. назад»)
DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\b")


def log(msg: str) -> None:
    print(msg, flush=True)


def make_session() -> requests.Session:
    """Сессия с автоматическими повторами при сетевых сбоях."""
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


# ---------------------------------------------------------------------------
# Парсинг сайта
# ---------------------------------------------------------------------------

def fetch_page(session: requests.Session, page: int, attempts: int = 3) -> str:
    url = FEED_URL if page == 0 else f"{FEED_URL}?page={page}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001 - показываем любую ошибку сети
            last_error = exc
            log(f"  попытка {attempt}/{attempt + attempts - 1} для {url}: {exc}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")


def parse_page(page_html: str) -> list[dict]:
    """Возвращает новости со страницы ленты в порядке от новых к старым."""
    soup = BeautifulSoup(page_html, "html.parser")
    items: list[dict] = []

    for card in soup.select("div.card"):
        link = None
        for a in card.select("a[href]"):
            href = (a.get("href") or "").split("?")[0].rstrip("/")
            if NEWS_LINK_RE.match(href):
                link = a
                break
        if link is None:
            continue

        href = (link.get("href") or "").split("?")[0].rstrip("/")
        title = link.get_text(" ", strip=True)
        if not title:
            strong = link.find("strong")
            title = strong.get_text(" ", strip=True) if strong else ""
        if not title:
            continue

        date = ""
        date_span = card.select_one("span.fs-8.text-secondary")
        if date_span:
            span_text = re.sub(r"\s+", " ", date_span.get_text(" ", strip=True)).strip()
            m = DATE_RE.search(span_text)
            if m:
                date = m.group(0)
            elif span_text and len(span_text) < 40:
                # Сегодняшние новости: «4 ч. 14 мин. назад», «только что» и т.п.
                date = span_text
        if not date:
            m = DATE_RE.search(card.get_text(" ", strip=True))
            if m:
                date = m.group(0)

        items.append(
            {
                "url": BASE + href,
                "title": re.sub(r"\s+", " ", title).strip(),
                "date": date,
            }
        )
    return items


def fetch_news(pages: int = 2) -> list[dict]:
    """Собирает новости со страниц ленты, без дублей, порядок: новые -> старые."""
    session = make_session()
    seen_urls: set[str] = set()
    items: list[dict] = []
    for page in range(pages):
        log(f"Загружаю страницу ленты {page + 1}/{pages}...")
        page_items = parse_page(fetch_page(session, page))
        added = 0
        for item in page_items:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            items.append(item)
            added += 1
        log(f"  найдено новостей: {len(page_items)} (новых для нас: {added})")
        if added == 0:
            break  # дальше только повторы
    if not items:
        raise RuntimeError("На странице новостей не найдено ни одной новости — возможно, изменилась вёрстка сайта.")
    return items


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        return set(data.get("seen", []))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Файл состояния {STATE_FILE.name} повреждён: {exc}")


def save_state(seen: set[str]) -> None:
    ordered = list(seen)[-MAX_SEEN:]
    STATE_FILE.write_text(
        json.dumps({"seen": ordered}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_proxies() -> dict | None:
    """Прокси для Telegram, если задан TELEGRAM_PROXY (например socks5://127.0.0.1:1080)."""
    proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def tg_api(token: str, method: str, payload: dict, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout, proxies=tg_proxies())
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "Нет соединения с api.telegram.org (SSL-обрыв). Обычно это блокировка "
            "Telegram-API вашим провайдером.\n"
            "Варианты: 1) включить VPN; 2) задать прокси в переменной TELEGRAM_PROXY "
            "(например socks5://127.0.0.1:1080); 3) выполнить это через GitHub Actions "
            "(workflow «Найти chat_id») — там блокировки нет.\n"
            f"Техническое описание: {exc}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Не удалось соединиться с api.telegram.org: {exc}") from exc
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"Telegram ответил не JSON (HTTP {resp.status_code}): {resp.text[:300]}")
    if not data.get("ok"):
        desc = data.get("description", "нет описания")
        if "chat not found" in desc or "user is deactivated" in desc:
            raise RuntimeError(
                "Telegram: чат не найден. Контакт должен ОДИН РАЗ написать боту /start, "
                "а в TELEGRAM_CHAT_ID должен быть правильный числовой chat_id "
                "(узнать: py news_monitor.py --find-chat)."
            )
        raise RuntimeError(f"Telegram API ошибка: {desc} (error_code={data.get('error_code')})")
    return data


def send_message(token: str, chat_id: str, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            tg_api(token, "sendMessage", payload)
            return
        except RuntimeError as exc:
            msg = str(exc)
            if "Too Many Requests" in msg or "429" in msg:
                wait = 3 * attempt
                log(f"  Rate limit Telegram, жду {wait} с...")
                time.sleep(wait)
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise RuntimeError(f"Не удалось отправить сообщение: {exc}") from exc
            time.sleep(3 * attempt)
    raise RuntimeError("Не удалось отправить сообщение после нескольких попыток.")


def format_message(item: dict) -> str:
    title = html.escape(item["title"])
    date = html.escape(item["date"]) if item["date"] else "дата не указана"
    if item["date"] and ("назад" in item["date"] or "только что" in item["date"]):
        date = "сегодня, " + date
    link = html.escape(item["url"], quote=True)
    return (
        f"📰 <b>{title}</b>\n"
        f"🕒 {date}\n"
        f"🔗 <a href=\"{link}\">Читать на govvrn.ru</a>"
    )


def find_chats(token: str) -> None:
    """Печатает chat_id всех, кто писал боту (после /start)."""
    log("Ищу пользователей, написавших боту (нужен /start)...")
    data = tg_api(token, "getUpdates", {"timeout": 0})
    updates = data.get("result", [])
    if not updates:
        log(
            "Пока никто не писал боту.\n"
            "  1) Перешлите контакту ссылку на бота и попросите написать ему: /start\n"
            "  2) Повторите --find-chat"
        )
        return
    found = []
    for upd in updates:
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
        if not chat:
            continue
        name = chat.get("title") or " ".join(
            str(chat.get(k, "")) for k in ("first_name", "last_name")
        ).strip()
        found.append((chat.get("id"), chat.get("type"), name))
        log(f"  chat_id={chat.get('id')}  тип={chat.get('type')}  имя={name!r}")
    if found:
        log("\nСкопируйте нужный chat_id в секрет TELEGRAM_CHAT_ID на GitHub.")


# ---------------------------------------------------------------------------
# Основной поток
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Новости govvrn.ru в Telegram")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--test", action="store_true", help="отправить тестовое сообщение")
    parser.add_argument("--find-chat", action="store_true", help="показать chat_id написавших боту")
    parser.add_argument("--send-latest", type=int, default=0, metavar="N",
                        help="отправить N последних новостей при первом запуске")
    parser.add_argument("--pages", type=int, default=2, help="сколько страниц ленты проверять")
    args = parser.parse_args()

    # то же значение можно передать переменной окружения (используется в GitHub Actions)
    if args.send_latest == 0:
        try:
            args.send_latest = int(os.environ.get("SEND_LATEST", "0") or 0)
        except ValueError:
            log("Переменная SEND_LATEST должна быть числом; игнорирую.")
            args.send_latest = 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        log("ОШИБКА: не задана переменная TELEGRAM_BOT_TOKEN.")
        return 1

    if args.find_chat:
        try:
            find_chats(token)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА: {exc}")
            return 1
        return 0

    if not chat_id:
        log(
            "TELEGRAM_CHAT_ID не задана. Чтобы узнать её:\n"
            "  1) Контакт пишет боту: /start\n"
            "  2) Запустите: py news_monitor.py --find-chat\n"
            "     (или через GitHub Actions → workflow «Найти chat_id», если провайдер блокирует Telegram)\n"
            "  3) Скопируйте chat_id в секрет TELEGRAM_CHAT_ID"
        )
        try:
            find_chats(token)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА: {exc}")
        return 1

    if args.test:
        send_message(
            token,
            chat_id,
            "✅ Тестовое сообщение.\nМонитор новостей govvrn.ru настроен и работает.",
        )
        log("Тестовое сообщение отправлено.")
        return 0

    # --- получаем новости с сайта ---
    try:
        items = fetch_news(pages=max(1, args.pages))
    except Exception as exc:  # noqa: BLE001
        log(f"ОШИБКА при загрузке сайта: {exc}")
        return 1
    log(f"Всего новостей на ленте: {len(items)}")

    seen = load_state()
    first_run = not seen

    if first_run and args.send_latest <= 0 and not args.dry_run:
        save_state({i["url"] for i in items})
        log(
            "Первый запуск: все текущие новости помечены как прочитанные, "
            "отправлять ничего не будем (чтобы не забивать чат).\n"
            "Для проверки отправки: py news_monitor.py --send-latest 3"
        )
        return 0

    if args.send_latest > 0:
        # «Догнать» N самых свежих ещё не отправленных новостей (от старых к новым)
        pending = [i for i in items if i["url"] not in seen]
        new_items = list(reversed(pending[: args.send_latest]))
        mark_all_seen = True
    else:
        # Новости на ленте идут от новых к старым; шлём от старых к новым
        new_items = [i for i in reversed(items) if i["url"] not in seen]
        mark_all_seen = False

    if not new_items:
        log("Новых новостей нет.")
        if args.dry_run:
            return 0
        return 0

    log(f"Новых новостей: {len(new_items)}")
    for i in new_items:
        log(f"  [{i['date']}] {i['title']}\n        {i['url']}")

    if args.dry_run:
        log("Режим --dry-run: ничего не отправлено и не сохранено.")
        return 0

    sent = set()
    errors = 0
    for item in new_items:
        try:
            send_message(token, chat_id, format_message(item))
            log(f"  ✔ отправлено: {item['title'][:60]}")
            sent.add(item["url"])
            seen.add(item["url"])
            time.sleep(1)  # не спамим API Telegram
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log(f"  ✖ ошибка отправки: {exc}")

    if mark_all_seen:
        # при разовом «догоне» помечаем всё, чтобы старое не ушло позже
        seen |= {i["url"] for i in items}

    save_state(seen)
    log(f"Готово. Отправлено: {len(sent)}, ошибок: {errors}. Состояние сохранено.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
