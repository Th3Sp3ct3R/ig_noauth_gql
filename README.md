# ig_noauth_gql — Instagram profile scraper (no login) with a local dashboard

Scrapes public Instagram profiles through the logged-out web app's GraphQL endpoint — the only
route left after Instagram closed the `web_profile_info` JSON API and the profile HTML page on
2 Sep 2026. Two requests per account, ~4 KB each.

Per account: bio, email, phone, followers, following, likes and dates of the last N posts, full
name, verified/private flags, external link. Everything runs on your own computer.

## Quick start (Windows)

1. Install Python 3 from python.org (tick **Add Python to PATH**).
2. Unzip this folder, e.g. to `C:\ig_noauth_gql`.
3. In PowerShell:
   ```
   cd C:\ig_noauth_gql
   pip install requests
   python dashboard.py
   ```
4. The dashboard opens at `http://127.0.0.1:8765` (open it yourself if it doesn't). Keep the
   PowerShell window open; Ctrl+C there stops the dashboard.

`openpyxl` (Excel output) installs itself on first run. For SOCKS proxies run
`pip install requests[socks]` once.

If `python` prints just `Python` and nothing else, the Microsoft Store stub is intercepting the
command: use `py` instead of `python`, or turn the stub off in *Settings → Apps → Advanced app
settings → App execution aliases* (`python.exe`, `python3.exe`).

## Files

| File | What it is |
|---|---|
| `dashboard.py`, `dashboard.html` | Local web dashboard (stdlib HTTP server + one page). |
| `ig_noauth_scrape.py` | Command-line bulk scraper; also the engine the dashboard uses. |
| `ig_gql.py` | Protocol helper (unchanged from the original package): warm-up, doc-id discovery from Instagram's JS bundles, `resolve`, `profile`, `Blocked`. |
| `usernames.txt` | Seed list of ~1050 well-known public accounts. |
| `EXPLANATION_LONG.md` | Original author's notes on how the endpoint works. |

Files the dashboard creates next to itself:

| File | Contents |
|---|---|
| `dashboard_results.jsonl` | every scraped row — the resume source and the export source |
| `dashboard_found.txt` | usernames harvested from posts |
| `dashboard_proxies.json` | the proxy pool |

## Using the dashboard

The progress band at the top is always visible: counter, rate, per-status totals, the connection
currently in use, and any message (blocked, finished, stopped). **Start / Stop** are in the top bar.

**Run** — paste usernames (one per line, `@` optional), *Load file…* (`usernames.txt` is a good
first list) or *Use harvested usernames* to reuse the names collected from earlier runs' posts.
A **Threads** table at the bottom shows, live, what each thread is doing: which connection it
holds, the account it's scraping right now, and how many it has finished — so you can see at a
glance that thread 2 is on the mobile proxy scraping `natgeo` while thread 3 just switched
connections after a block.
The *Use multithreading* switch turns parallel scraping on or off; when on, *Threads* sets
how many run at once (4 is fine, one per proxy if you have that many) — off pins it to 1, useful
for slow or rate-sensitive proxies. Each thread is assigned its own connection from the Proxies
pool (see below) and switches automatically if that connection gets blocked. *Usernames per IP* =
how many accounts a thread does on one connection before proactively rotating to another, even if
it hasn't been blocked.
*Harvest usernames from posts* collects tagged users, co-authors and @mentions into
`dashboard_found.txt` — the "snowball" way to grow from 1 000 to 10 000+ real accounts.
The log on the right shows every account as it completes.

**Fields** — tick what goes into the results table and the downloads. Every field is scraped
regardless, so you can change the ticks later and re-download. *Last N posts* sets how many posts'
likes and dates are kept (applies to runs started after the change).

**Proxies** — the connection pool.
- *This computer's IP* is always in the list; switch it off if you only want proxies used.
- Each thread claims its own connection and keeps using it until it's blocked, fails, or hits the
  *Usernames per IP* limit. With more usable connections than threads every thread gets a
  different one; with fewer, the least-used connection is shared. A thread that gets rate-limited
  releases its connection and claims a different one immediately — no restart needed.
- *Add proxy*: `socks5h://user:pass@host:port` or `http://user:pass@host:port`, plus an optional
  link that makes the provider switch the exit IP.
- *Load file…*: import many at once, one proxy per line (e.g. `http://138.199.35.215:9002`); a
  missing `http://` is added automatically. Put a rotate-IP link after a comma or tab on the same
  line if you have one. `#` comments and proxies already in the pool are skipped.
- Tick rows (or the header checkbox for all) and *Remove selected* to delete several at once. The
  direct connection can't be removed, only disabled.
- *Test all* tests every connection at once (up to 20 in parallel), with a live `n / total tested`
  counter; *Test selected* appears once you've ticked some rows and tests only those. Useful right
  after importing a large proxy list.
- The **All / Working / Not working / Untested** filter shows only the connections in that state —
  handy for dropping a batch of dead proxies after a Test all. *Select all* selects only the rows
  currently visible under the filter.
- *Test* sends one real request and shows the latency (or the error).
- During a run the enabled connections are used in turn. When one is rate-limited the run moves
  to the next; a blocked connection rests for three hours and then comes back by itself; a
  connection that failed to connect is skipped until it passes a Test again. When nothing is
  available the run stops and tells you when the earliest connection frees up.

**Results** — the latest 60 rows as a table, *Download Excel* (formatted: frozen header,
filters, thousands separators, wrapped bio), *Download CSV* (`;`-separated, opens in columns in a
Russian-locale Excel), *Harvested usernames* (.txt), *Clear all results*.

Runs are resumable: results are written to disk as they arrive, so Stop, a block or closing the
window never loses data. Press Start again with the same list and it continues; rows that ended
as `blocked` or `error` are retried.

### Statuses

| Status | Meaning |
|---|---|
| `ok` | profile scraped |
| `not_found` | username does not exist |
| `no_posts_visible` | account exists but shows no posts to a logged-out viewer (private or empty) — Instagram exposes no id for these, so no profile data |
| `blocked` | request was rate-limited; retried on the next run |
| `error: …` | failed repeatedly (see below); retried on the next run too |

### Retries

A request that fails outright (connection error, timeout, proxy trouble) is retried immediately,
in the same run, on a different connection — up to twice, so an account gets up to three attempts
across up to three connections before it's written as `error`. A `blocked` (429/401) response
always switches connections and requeues the account without counting against that limit. Rows
left as `blocked` or `error` when a run ends are retried automatically the next time you press
Start with the same list.

### Field notes

- **Post count** is not exposed on this route — the column is always empty.
- **Email / phone** come from business-profile fields when Instagram returns them, otherwise
  they are extracted from the bio text. Most accounts keep contacts behind the "Email" button
  rather than in the bio, so the columns are often empty.
- **Likes** show `hidden` for accounts that hide like counts; post 1 is the newest.

## Rate limits

One IP gets roughly 300 requests (about 145 accounts) before Instagram answers 401/429 for
one to several hours. Without a proxy that means ~145 accounts per session. To check whether a
block has lifted without a run, use *Test* on the Proxies page (one request — don't spam it).
Rebooting a router with a dynamic IP, or tethering through a phone, usually gives a fresh address.

For anything beyond a few hundred accounts add a rotating mobile or residential proxy. With a
rotate-IP link the scraper switches IPs every *Usernames per IP* accounts and never stops; the
original author measured ~280 accounts/min with 8 workers on a mobile proxy, so 10 000 accounts
take under an hour.

## Command line (without the dashboard)

```
python ig_noauth_scrape.py usernames.txt out.xlsx                       # bulk scrape to Excel
python ig_noauth_scrape.py usernames.txt out.xlsx --discover found.txt  # also harvest usernames
python ig_noauth_scrape.py usernames.txt out.csv                        # ;-separated CSV instead
python ig_noauth_scrape.py --raw natgeo                                 # dump raw profile JSON
python ig_gql.py both natgeo                                            # one-account protocol check
```

Options: `--workers N` (4), `--per-ip N` (140), `--discover FILE`, `--raw USERNAME`.
Proxy via environment variables: `$env:IG_PROXY='socks5h://…'`, `$env:IG_ROTATE='https://…'`.
Output files are resumable the same way as in the dashboard; `NPOSTS = 5` at the top of the file
sets how many posts are kept. The CLI and the dashboard keep separate result files.

## Changes from the original package

- Local web dashboard with proxy pool, live progress, results preview and Excel/CSV export.
- Bulk scraper writes formatted `.xlsx` (or `;`-CSV) with bio, email, phone, followers,
  following, likes/dates of the last N posts, name, flags, link.
- Username harvesting from post tags, co-authors and caption mentions (`--discover`).
- Raw profile dump (`--raw`), `[n/total]` progress counter, elapsed-time summary.
- Stops on block instead of burning through the list; blocked/error rows are retried on resume.
- Seed list of ~1050 public accounts.
- `ig_gql.py` unchanged.
