"""Apify actor: Vavada esports odds via public Altenar widget API.

Implementation notes
--------------------
* Vavada's SPA renders esports from the unauthenticated Altenar widget API.
* Endpoint: https://sb2frontend-altenar2.biahosted.com/api/WidgetESports/GetESportsEvents
* Returns events, competitors, championships (leagues), categories (games), markets and odds.
* The actor paginates through available pages and pushes match-winner records.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from apify import Actor
import httpx

from .normalise import normalise_game

API_BASE = "https://sb2frontend-altenar2.biahosted.com/api"
EVENTS_URL = f"{API_BASE}/WidgetESports/GetESportsEvents"

DEFAULT_PARAMS = {
    "culture": "en-GB",
    "timezoneOffset": 240,
    "integration": "vavada",
    "deviceType": 1,
    "numFormat": "en-GB",
    "countryCode": "AG",
    "sportId": 145,
    "period": 0,
    "eventCount": 100,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def index_by_id(items: list[dict], key: str = "id") -> dict[Any, dict]:
    return {item[key]: item for item in items if key in item}


def select_match_winner_market(event_market_ids: list[int], markets_index: dict[int, dict]) -> dict | None:
    for mid in event_market_ids:
        m = markets_index.get(mid, {})
        name = (m.get("name") or "").lower()
        if "match" in name and "winner" in name:
            return m
    # Fallback: first market that has two oddIds
    for mid in event_market_ids:
        m = markets_index.get(mid, {})
        odd_ids = m.get("oddIds") or []
        if len(odd_ids) >= 2:
            return m
    return None


def build_schema_records(data: dict) -> list[dict[str, Any]]:
    """Convert Vavada Altenar API response into schema-locked records."""
    competitors = index_by_id(data.get("competitors", []))
    champs = index_by_id(data.get("champs", []))
    categories = index_by_id(data.get("categories", []))
    markets_index = index_by_id(data.get("markets", []))
    odds_index = index_by_id(data.get("odds", []), key="id")

    records: list[dict] = []
    for event in data.get("events", []):
        comp_ids = event.get("competitorIds", [])
        if len(comp_ids) < 2:
            continue
        comp_a = competitors.get(comp_ids[0], {})
        comp_b = competitors.get(comp_ids[1], {})
        team1 = comp_a.get("name", "").strip()
        team2 = comp_b.get("name", "").strip()
        if not team1 or not team2:
            continue

        champ = champs.get(event.get("champId"), {})
        category = categories.get(event.get("catId"), {})
        tournament_name = champ.get("name", "")
        game_raw = category.get("name", "Unknown")
        game = normalise_game(game_raw)

        market = select_match_winner_market(event.get("marketIds", []), markets_index)
        price_team1 = price_team2 = price_draw = None
        if market:
            odd_ids = market.get("oddIds", [])
            odds = [odds_index.get(oid) for oid in odd_ids if oid in odds_index]
            odd_a = next((o for o in odds if o.get("competitorId") == comp_ids[0]), None)
            odd_b = next((o for o in odds if o.get("competitorId") == comp_ids[1]), None)
            if odd_a and odd_b:
                price_team1 = odd_a["price"]
                price_team2 = odd_b["price"]

        status = event.get("status")
        is_live = status is not None and status != 0

        records.append({
            "bookmaker": "vavada",
            "game_raw": game_raw,
            "game": game,
            "tournament_name": tournament_name,
            "team1": team1,
            "team2": team2,
            "match_start_time": event.get("startDate"),
            "match_url": f"https://vavada.com/en/sports#/event/{event.get('id')}",
            "market_name": "Match Winner",
            "price_team1": price_team1,
            "price_team2": price_team2,
            "price_draw": price_draw,
            "scraped_at": now_iso(),
            "is_live": bool(is_live),
            "event_id": str(event.get("code", event.get("id"))),
        })

    return records


async def fetch_page(client: httpx.AsyncClient, page: int) -> dict | None:
    params = {**DEFAULT_PARAMS, "page": page}
    try:
        resp = await client.get(EVENTS_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        Actor.log.warning(f"API page {page} failed: {exc}")
        return None


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_pages = int(input_data.get("maxPages") or 0)
        proxy_url = input_data.get("proxyUrl") or None
        hub_names = [h.strip() for h in (input_data.get("hubNames") or [])]
        hub_canonical = {normalise_game(h).lower() for h in hub_names if h}

        async with httpx.AsyncClient(proxy=proxy_url or None, follow_redirects=True) as client:
            page = 1
            total = 0
            seen_ids: set[str] = set()
            while True:
                data = await fetch_page(client, page)
                if not data:
                    break

                records = build_schema_records(data)
                Actor.log.info(f"Page {page}: {len(records)} records")

                for rec in records:
                    if rec["event_id"] in seen_ids:
                        continue
                    seen_ids.add(rec["event_id"])
                    if hub_canonical and rec["game"] and rec["game"].lower() not in hub_canonical:
                        continue
                    # Only emit records that have match-winner odds
                    if rec.get("price_team1") is None or rec.get("price_team2") is None:
                        continue
                    await actor.push_data(rec)
                    total += 1

                page_count = data.get("pageCount", 0)
                if page_count and page >= page_count:
                    break
                if max_pages and page >= max_pages:
                    break
                page += 1

        Actor.log.info(f"Finished; pushed {total} events total")


if __name__ == "__main__":
    asyncio.run(main())
