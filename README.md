# Now Showing

A small read-only web page for the people who use your media server: what was **just added** and what is
**coming up**, built from Sonarr and Radarr. No login, no database, no accounts. Click a tile for the plot,
episode details and links to IMDb, Letterboxd, TVDB and TMDB.

- **Just added** (last 14 days): imports from Sonarr/Radarr history. Episodes are grouped per show, quality
  upgrades of things you already had are left out, and a movie in two Radarrs (e.g. HD and 4K) shows once.
- **Coming up** (next 14 days): monitored episodes that have not downloaded yet, and movies on their
  digital or disc release date.

The server polls the *arrs every 15 minutes and serves the page from memory. API keys stay on the server;
posters load from TMDB/TVDB. Python standard library only.

## Run

```sh
cp .env.example .env      # fill in URLs and API keys
python3 app.py            # http://localhost:8077
```

With Docker:

```sh
cp .env.example docker/.env   # fill in
docker compose -f docker/docker-compose.yml up -d --build
```

## Settings

All settings are environment variables. See `.env.example`.

| Variable | Default | |
|---|---|---|
| `SONARR_URL`, `SONARR_KEY` | — | One pair per instance. Extra instances: `SONARR_<NAME>_URL/_KEY` |
| `RADARR_URL`, `RADARR_KEY` | — | Same, `RADARR_<NAME>_URL/_KEY` |
| `TZ` | `Australia/Melbourne` | Display timezone |
| `DAYS_AHEAD`, `DAYS_BACK` | `14`, `14` | Window for each tab |
| `REFRESH_MINUTES` | `15` | Poll interval |
| `SITE_TITLE` | `Now Showing` | |
| `REQUEST_URL` | — | Adds a "Request something" link (e.g. to Seerr) |
| `PORT` | `8077` | Listen port |

`/healthz` returns 200 after a successful refresh, 503 otherwise.

The API keys have full access to Sonarr/Radarr. Do not expose them: keep `.env` out of git and run this on a
network you control.
