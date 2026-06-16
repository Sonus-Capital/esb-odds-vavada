# Vavada esports odds scraper

Vavada esports odds via the public Altenar widget API.

## Source API

- `https://sb2frontend-altenar2.biahosted.com/api/WidgetESports/GetESportsEvents`
- Integrator: `vavada`, sportId `145`.

## Flow

1. Fetch paginated event list (events, competitors, champs/leagues, categories/games, markets, odds).
2. Select the `Match winner` market for each event.
3. Map odds to competitors by `competitorId`.

## Inputs

- `hubNames`: array of game names to limit scraping (e.g. `["Dota 2"]`).
- `maxPages`: integer page limit (0 = all pages).
- `proxyUrl`: optional proxy URL.
