# Vavada esports odds scraper

Public Vavada esports odds via DOM scraping.

## Flow

1. Open `https://vavada.com/en/sports#/esports`.
2. Wait for the widget SPA to render hub buttons (Counter-Strike, Dota2, etc.).
3. Click each hub, extract visible event-card texts.
4. Parse event texts for id, league, game, teams, and decimal odds.

## Inputs

- `hubNames`: array of hub game names to limit scraping (e.g. `["Counter-Strike"]`).
- `proxyUrl`: optional proxy to bypass Qrator IP blocks.
- `headful`: debug mode.
