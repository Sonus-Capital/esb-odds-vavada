"""Apify actor: Vavada esports odds via public SPA DOM scraping.

Implementation notes
--------------------
* Vavada renders esports through a widget on https://vavada.com/en/sports#/esports.
* Hub buttons are visible (e.g., Counter-Strike 23, Dota2 16, Valorant 24).
* Event card text contains:
  "{event_id} {DD/MM} • {HH:MM} {league} • {game} {LIVE?} {teamA} {teamB} {odds...}".
* The actor clicks each hub, extracts leaf event-card texts, and parses them.
* Vavada blocks some datacenter IPs via Qrator; optional proxyUrl input is supported.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from apify import Actor
from playwright.async_api import async_playwright, Page

from .normalise import normalise_game

START_URL = "https://vavada.com/en/sports#/esports"

GAME_LABELS = [
    "Counter-Strike",
    "League of Legends",
    "Starcraft 2",
    "Rainbow Six",
    "Valorant",
    "Fortnite",
    "Overwatch",
    "Dota2",
    "Crossfire",
]

ODD_RE = re.compile(r"\d+\.\d+")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_game(remainder: str) -> tuple[str, str]:
    for label in GAME_LABELS:
        if remainder.lower().startswith(label.lower()):
            return label, remainder[len(label):].strip()
    tokens = remainder.split()
    return (tokens[0] if tokens else "Unknown", " ".join(tokens[1:]))


def parse_event_text(text: str) -> dict[str, Any] | None:
    """Parse a Vavada event card text into structured fields."""
    parts = [p.strip() for p in text.split("•")]
    if len(parts) < 3:
        return None

    id_date = parts[0].split()
    if len(id_date) < 2:
        return None
    event_id = id_date[0]
    date_str = id_date[1]  # DD/MM

    rest1_tokens = parts[1].split()
    if not rest1_tokens or not TIME_RE.match(rest1_tokens[0]):
        return None
    time_str = rest1_tokens[0]
    league = " ".join(rest1_tokens[1:])

    game, remainder = extract_game(parts[2].strip())

    is_live = False
    rem = remainder
    if rem.upper().startswith("LIVE"):
        is_live = True
        rem = rem[4:].strip()

    # Split remainder keeping decimal odds as tokens
    split_parts = ODD_RE.split(rem)
    odds = [float(o) for o in ODD_RE.findall(rem)]
    if not odds or len(split_parts) < 2:
        return None

    team_a = split_parts[0].strip()
    team_b = split_parts[-1].strip()

    # Parse start time (year inferred)
    start_time: str | None = None
    try:
        today = datetime.now(timezone.utc)
        dt = datetime.strptime(f"{date_str}/{today.year} {time_str}", "%d/%m/%Y %H:%M")
        dt = dt.replace(tzinfo=timezone.utc)
        if dt < today - timedelta(days=1):
            dt = dt.replace(year=today.year + 1)
        start_time = dt.isoformat()
    except Exception:
        pass

    markets = []
    if len(odds) >= 2:
        markets = [
            {"market_id": "match_winner", "outcome_id": "H", "team": team_a, "odds": odds[0]},
            {"market_id": "match_winner", "outcome_id": "A", "team": team_b, "odds": odds[-1]},
        ]

    return {
        "event_id": event_id,
        "brand": "vavada",
        "sport": "Esports",
        "game": normalise_game(game),
        "league": league,
        "team_a": team_a,
        "team_b": team_b,
        "is_live": is_live,
        "start_time": start_time,
        "markets": markets,
        "raw_text": text,
        "scraped_at": now_iso(),
    }


async def accept_cookies(page: Page) -> None:
    for selector in ["button:has-text('Accept')", "button:has-text('I agree')", "button:has-text('Agree')"]:
        with suppress(Exception):
            await page.locator(selector).first.click(timeout=3000)
            await asyncio.sleep(0.5)


async def safe_goto(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        Actor.log.warning(f"Navigation to {url} ended with {exc}; continuing anyway")
    with suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=20000)


async def extract_hub_labels(page: Page) -> list[str]:
    labels = await page.eval_on_selector_all(
        "*",
        """elements => {
            const re = /^(Counter-Strike|League of Legends|Starcraft 2|Rainbow Six|Valorant|Fortnite|Overwatch|Dota2|Crossfire)\\s+\\d+$/i;
            const seen = new Set();
            return Array.from(elements)
                .map(el => el.innerText.trim())
                .filter(t => re.test(t))
                .filter(t => { if (seen.has(t)) return false; seen.add(t); return true; });
        }""",
    )
    return labels


async def extract_event_texts(page: Page) -> list[str]:
    texts = await page.eval_on_selector_all(
        "*",
        """elements => {
            const re = /^\\d+\\s+\\d{2}\\/\\d{2}\\b/;
            const seen = new Set();
            const results = [];
            for (const el of elements) {
                if (el.children.length === 0 || el.children.length === 1 && el.children[0].nodeType === 3) {
                    const t = el.innerText.trim();
                    if (t && re.test(t) && !seen.has(t)) {
                        seen.add(t);
                        results.push(t);
                    }
                }
            }
            return results;
        }""",
    )
    return texts


async def click_hub(page: Page, label: str) -> bool:
    """Click a hub by its visible label. Returns True if no error."""
    try:
        # label is e.g. "Counter-Strike 23"; click element matching the full text
        loc = page.get_by_text(re.compile(re.escape(label)), exact=False)
        await loc.first.click(timeout=5000)
        return True
    except Exception as exc:
        Actor.log.warning(f"Could not click hub {label}: {exc}")
        return False


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        hub_names = [h.strip() for h in (input_data.get("hubNames") or [])]
        proxy_url = input_data.get("proxyUrl") or None
        headless = not bool(input_data.get("headful"))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            }
            if proxy_url:
                ctx_kwargs["proxy"] = {"server": proxy_url}
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            await safe_goto(page, START_URL)
            await accept_cookies(page)

            # wait for the SPA to render hubs
            for _ in range(30):
                await asyncio.sleep(1)
                hubs = await extract_hub_labels(page)
                if hubs:
                    break

            if not hubs:
                Actor.log.warning("No hubs found; page may be blocked")

            if hub_names:
                hubs = [h for h in hubs if any(hn.lower() in h.lower() for hn in hub_names)]

            Actor.log.info(f"Discovered hubs: {hubs}")

            seen_ids: set[str] = set()
            total = 0

            for hub in hubs:
                try:
                    ok = await click_hub(page, " ".join(hub.split()[:-1]))  # strip count for click matching
                    if not ok:
                        continue
                    await asyncio.sleep(5)
                    with suppress(Exception):
                        await page.wait_for_load_state("networkidle", timeout=15000)

                    texts = await extract_event_texts(page)
                    Actor.log.info(f"Hub {hub}: {len(texts)} raw event texts")
                    for txt in texts:
                        rec = parse_event_text(txt)
                        if not rec or rec["event_id"] in seen_ids:
                            continue
                        seen_ids.add(rec["event_id"])
                        await actor.push_data(rec)
                        total += 1
                except Exception as exc:
                    Actor.log.exception(f"Hub {hub} failed: {exc}")

            await context.close()
            await browser.close()
            Actor.log.info(f"Finished; pushed {total} events total")


if __name__ == "__main__":
    asyncio.run(main())
