#!/usr/bin/env python3
"""
Монитор новостей Воронежской области -> Telegram.

Источники (по приоритету):
  1) сайт govvrn.ru — полный поток новостей (напрямую или через бесплатные
     российские прокси, если сайт блокирует зарубежные дата-центры);
  2) официальный telegram-канал @govvrn36 (Правительство Воронежской области) —
     если сайт недоступен. Канал отстаёт от сайта в среднем на ~70 минут и
     публикует ~20% новостей, поэтому используется только как резерв.

Работает по расписанию (GitHub Actions) или вручную.

Опции:
  --dry-run          только показать новые новости, ничего не отправлять и не сохранять
  --test             отправить в Telegram тестовое сообщение
  --find-chat        найти chat_id написавших боту пользователей (после /start)
  --send-latest N    отправить N последних новостей (для первого запуска/проверки)
  --pages N          сколько страниц ленты сайта проверять (по умолчанию 2)
  --source S         auto | site | telegram — принудительно выбрать источник (по умолчанию auto)
  --no-proxies       не искать бесплатные прокси (быстро проверить, доступен ли сайт напрямую)

Переменные окружения:
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather (обязательна)
  TELEGRAM_CHAT_ID    числовой chat_id получателя (обязательна, кроме --find-chat)
  TELEGRAM_PROXY      прокси для Telegram, если он блокируется (например socks5://127.0.0.1:1080)
  SEND_LATEST         то же, что --send-latest
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.govvrn.ru"
FEED_URL = f"{BASE}/news/all"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

CHANNEL = "govvrn36"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"

MAX_SEEN = 800  # сколько ключей хранить в состоянии, чтобы файл не рос вечно

MSK = timezone(timedelta(hours=3))

# Жёсткие таймауты: если сайт недоступен, мы должны быстро перейти к следующему
# варианту (прокси / telegram-канал), а не висеть до таймаута задания.
SITE_CONNECT_TIMEOUT = 8
SITE_READ_TIMEOUT = 25
# Сколько секунд в сумме разрешено потратить на перебор бесплатных прокси
PROXY_BUDGET_SECONDS = 100
# Сколько прокси пробовать за один запуск
PROXY_LIMIT = 10

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
NEWS_ANY_RE = re.compile(r"/news/\d{4}/\d{2}/\d{2}/\d+")
NEWS_ABS_RE = re.compile(r"https?://(?:www\.)?govvrn\.ru(/news/\d{4}/\d{2}/\d{2}/\d+)")
# Дата вида 15.10.2026 14:53 (для новостей не старше суток сайт показывает «4 ч. назад»)
DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\b")
# Относительная дата сайта: «4 ч. 14 мин. назад», «12 мин. назад», «только что»
REL_DATE_RE = re.compile(r"(\d+)\s*ч\.(?:\s*(\d+)\s*мин\.)?\s*назад|(\d+)\s*мин\.\s*назад|только что")


def log(msg: str) -> None:
    print(msg, flush=True)


def make_session() -> requests.Session:
    """Сессия с одним автоматическим повтором при сетевых сбоях."""
    session = requests.Session()
    retries = Retry(
        total=1,
        backoff_factor=0.5,
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

        url = BASE + href
        items.append(
            {
                "key": url,
                "alt": None,
                "url": url,
                "title": re.sub(r"\s+", " ", title).strip(),
                "date": date,
                "source": "site",
            }
        )

    return items


def _looks_like_feed(text: str) -> bool:
    """Проверяем, что нам вернули именно ленту новостей (защита от мусора и «заглушек» прокси)."""
    return len(NEWS_ANY_RE.findall(text)) >= 5


def discover_proxies() -> list[str]:
    """Список бесплатных российских прокси из публичных источников."""
    session = make_session()
    sources = (
        ("socks5h", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=5000&country=RU"),
        ("http", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=RU"),
        ("socks5h", "https://www.proxy-list.download/api/v1/get?type=socks5&country=RU"),
    )
    found: list[str] = []
    for scheme, url in sources:
        try:
            resp = session.get(url, timeout=(6, 15))
            if resp.status_code != 200:
                continue
            for line in resp.text.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$", line):
                    continue
                proxy = f"{scheme}://{line}"
                if proxy not in found:
                    found.append(proxy)
        except Exception as exc:  # noqa: BLE001
            log(f"  источник прокси недоступен ({url}): {exc}")
    return found[:PROXY_LIMIT]


def fetch_site_htmls(pages: int, cached_proxy: str | None, use_proxies: bool) -> tuple[list[str], str | None]:
    """Пытается получить страницы ленты сайта: напрямую -> сохранённый прокси -> новые прокси.

    Возвращает (список html-страниц, использованный_прокси_или_None).
    """
    session = make_session()
    deadline = time.monotonic() + PROXY_BUDGET_SECONDS

    def try_proxy(proxy: str | None) -> list[str] | None:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        htmls: list[str] = []
        for page in range(pages):
            url = FEED_URL if page == 0 else f"{FEED_URL}?page={page}"
            try:
                resp = session.get(
                    url,
                    timeout=(SITE_CONNECT_TIMEOUT, SITE_READ_TIMEOUT),
                    proxies=proxies,
                )
                resp.raise_for_status()
                text = resp.text
            except Exception as exc:  # noqa: BLE001
                if page == 0:
                    log(f"    страница 1 не загрузилась: {exc}")
                    return None
                log(f"    страница {page + 1} не загрузилась ({exc}) — беру то, что есть")
                break
            if page == 0 and not _looks_like_feed(text):
                log("    ответ не похож на ленту новостей — пропускаю")
                return None
            htmls.append(text)
        return htmls or None

    log("Пробую сайт напрямую...")
    htmls = try_proxy(None)
    if htmls:
        return htmls, None

    if cached_proxy:
        log(f"Пробую сохранённый прокси {cached_proxy}...")
        htmls = try_proxy(cached_proxy)
        if htmls:
            return htmls, cached_proxy

    if not use_proxies:
        return [], None

    log("Ищу бесплатные российские прокси...")
    candidates = discover_proxies()
    log(f"  найдено кандидатов: {len(candidates)}")
    for proxy in candidates:
        if time.monotonic() > deadline:
            log("  время на перебор прокси вышло.")
            break
        log(f"  пробую {proxy}...")
        htmls = try_proxy(proxy)
        if htmls:
            log(f"  прокси подошёл: {proxy}")
            return htmls, proxy

    return [], None


def fetch_site(pages: int, cached_proxy: str | None, use_proxies: bool = True) -> tuple[list[dict], str | None]:
    """Новости с сайта (могут быть получены через прокси)."""
    htmls, proxy = fetch_site_htmls(max(1, pages), cached_proxy, use_proxies)
    if not htmls:
        raise RuntimeError("Сайт govvrn.ru недоступен (ни напрямую, ни через бесплатные прокси).")

    seen_urls: set[str] = set()
    items: list[dict] = []
    for idx, page_html in enumerate(htmls):
        page_items = parse_page(page_html)
        added = 0
        for item in page_items:
            if item["key"] in seen_urls:
                continue
            seen_urls.add(item["key"])
            items.append(item)
            added += 1
        log(f"  страница {idx + 1}: новостей {len(page_items)}, новых для нас {added}")
        if added == 0:
            break

    if not items:
        raise RuntimeError("Лента новостей загрузилась, но новостей не найдено — возможно, изменилась вёрстка.")
    return items, proxy


# ---------------------------------------------------------------------------
# Резервный источник: официальный telegram-канал @govvrn36
# ---------------------------------------------------------------------------

def fetch_channel() -> list[dict]:
    """Свежие посты официального канала региона (веб-версия t.me/s/<channel>)."""
    session = make_session()
    try:
        resp = session.get(CHANNEL_URL, timeout=(10, 30))
        resp.raise_for_status()
        page_html = resp.text
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось открыть {CHANNEL_URL}: {exc}") from exc

    soup = BeautifulSoup(page_html, "html.parser")
    items: list[dict] = []
    for msg in soup.select("div.tgme_widget_message"):
        data_post = (msg.get("data-post") or "").strip()
        if "/" not in data_post:
            continue
        post_id = data_post.split("/")[-1]
        if not post_id.isdigit():
            continue

        date = ""
        time_el = msg.select_one("time[datetime]")
        if time_el is not None and time_el.get("datetime"):
            try:
                dt = datetime.fromisoformat(time_el["datetime"]).astimezone(MSK)
                date = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:  # noqa: BLE001
                date = ""

        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el is not None else ""
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip()
        if not text:
            continue  # медиа без подписи — отправлять нечего

        title = text.split("\n")[0].strip()
        if len(title) > 300:
            title = title[:297].rstrip() + "…"

        url = f"https://t.me/{CHANNEL}/{post_id}"
        alt = None
        m = NEWS_ABS_RE.search(text)
        if m:
            # в посте часто есть ссылка на страницу сайта — используем её для дедупликации
            alt = BASE + m.group(1).rstrip("/")

        items.append(
            {
                "key": f"tg:{post_id}",
                "alt": alt,
                "url": url,
                "title": re.sub(r"\s+", " ", title).strip(),
                "date": date,
                "source": "telegram",
                "order": int(post_id),
            }
        )

    if not items:
        raise RuntimeError(
            f"В канале @{CHANNEL} не найдено ни одного поста — возможно, изменилась страница preview."
        )

    items.sort(key=lambda i: i["order"], reverse=True)  # от новых к старым
    return items


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

def load_state() -> tuple[set[str], str | None]:
    """Возвращает (множество уже отправленных ключей, сохранённый рабочий прокси)."""
    if not STATE_FILE.exists():
        return set(), None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Файл состояния {STATE_FILE.name} повреждён: {exc}") from exc
    proxy = (data.get("proxy") or "").strip() or None
    return set(data.get("seen", [])), proxy


def save_state(seen: set[str], proxy: str | None) -> None:
    payload: dict = {"seen": list(seen)[-MAX_SEEN:]}
    if proxy:
        payload["proxy"] = proxy
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
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
    if item.get("source") == "telegram":
        label = "Открыть в Telegram"
    else:
        label = "Читать на govvrn.ru"
    return (
        f"📰 <b>{title}</b>\n"
        f"🕒 {date}\n"
        f"🔗 <a href=\"{link}\">{label}</a>"
    )


def item_keys(item: dict) -> set[str]:
    keys = {item["key"]}
    if item.get("alt"):
        keys.add(item["alt"])
    return keys


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

def collect_items(args, cached_proxy: str | None) -> tuple[list[dict], str | None]:
    """Собирает новости: сначала сайт, при недоступности — telegram-канал."""
    source = args.source
    if source in ("auto", "site"):
        try:
            items, proxy = fetch_site(max(1, args.pages), cached_proxy, use_proxies=not args.no_proxies)
            log(f"Источник: сайт govvrn.ru{f' (через прокси {proxy})' if proxy else ' (напрямую)'}.")
            return items, proxy
        except Exception as exc:  # noqa: BLE001
            log(f"Сайт недоступен: {exc}")
            if source == "site":
                raise

    log("Переключаюсь на резервный источник — telegram-канал @govvrn36.")
    items = fetch_channel()
    log(f"Источник: канал @{CHANNEL}.")
    return items, cached_proxy


def main() -> int:
    parser = argparse.ArgumentParser(description="Новости Воронежской области в Telegram")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--test", action="store_true", help="отправить тестовое сообщение")
    parser.add_argument("--find-chat", action="store_true", help="показать chat_id написавших боту")
    parser.add_argument("--send-latest", type=int, default=0, metavar="N",
                        help="отправить N последних новостей при первом запуске")
    parser.add_argument("--pages", type=int, default=2, help="сколько страниц ленты сайта проверять")
    parser.add_argument("--source", choices=("auto", "site", "telegram"), default="auto",
                        help="источник: auto (сайт, иначе канал), site, telegram")
    parser.add_argument("--no-proxies", action="store_true", help="не перебирать бесплатные прокси")
    args = parser.parse_args()

    # то же значение можно передать переменной окружения (используется в GitHub Actions)
    if args.send_latest == 0:
        try:
            args.send_latest = int(os.environ.get("SEND_LATEST", "0") or 0)
        except ValueError:
            log("Переменная SEND_LATEST должна быть числом; игнорирую.")
            args.send_latest = 0

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

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
            "✅ Тестовое сообщение.\nМонитор новостей Воронежской области настроен и работает.",
        )
        log("Тестовое сообщение отправлено.")
        return 0

    seen, cached_proxy = load_state()

    # --- получаем новости ---
    try:
        items, used_proxy = collect_items(args, cached_proxy)
    except Exception as exc:  # noqa: BLE001
        log(f"ОШИБКА при получении новостей: {exc}")
        return 1
    log(f"Всего новостей получено: {len(items)}")

    first_run = not seen

    if first_run and args.send_latest <= 0 and not args.dry_run:
        save_state({k for i in items for k in item_keys(i)}, used_proxy)
        log(
            "Первый запуск: все текущие новости помечены как прочитанные, "
            "отправлять ничего не будем (чтобы не забивать чат).\n"
            "Для проверки отправки: py news_monitor.py --send-latest 3"
        )
        return 0

    if args.send_latest > 0:
        # «Догнать» N самых свежих ещё не отправленных новостей (от старых к новым)
        pending = [i for i in items if not (item_keys(i) & seen)]
        new_items = list(reversed(pending[: args.send_latest]))
        mark_all_seen = True
    else:
        # Новости идут от новых к старым; шлём от старых к новым
        new_items = [i for i in reversed(items) if not (item_keys(i) & seen)]
        mark_all_seen = False

    if not new_items:
        log("Новых новостей нет.")
        if not args.dry_run:
            save_state(seen, used_proxy)
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
            sent |= item_keys(item)
            seen |= item_keys(item)
            time.sleep(1)  # не спамим API Telegram
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log(f"  ✖ ошибка отправки: {exc}")

    if mark_all_seen:
        # при разовом «догоне» помечаем всё, чтобы старое не ушло позже
        seen |= {k for i in items for k in item_keys(i)}

    save_state(seen, used_proxy)
    log(f"Готово. Отправлено: {len(sent)}, ошибок: {errors}. Состояние сохранено.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
