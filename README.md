# Powder Desk

A personal snow dashboard for Logan, plus phone alerts when big snow is coming.

- **Front and center:** 7-day snow for Beaver Mountain, Tony Grove and Franklin Basin (the middle of five weather models plus the NWS forecasters' own numbers), with the Logan valley forecast alongside.
- **Avalanche:** the current Utah Avalanche Center rating for the Logan zone, its bottom line and a link to the full forecast.
- **Forecasters vs. the numbers:** the NWS Salt Lake City forecast discussion and the latest Wasatch Weather Weenies post, next to a neutral read written by Claude from the same data.
- **Where it's dumping:** 18 resorts and backcountry zones within ~3.5 hours, ranked by the next 72 hours or 7 days, with avalanche danger on backcountry spots.
- **Alerts:** push notifications through the free ntfy app. Quiet unless something crosses a threshold.

Everything runs free on GitHub: a scheduled Action fetches the data every 2 hours, saves `docs/data.json`, and GitHub Pages serves the page.

## Setup (about 15 minutes)

1. **Make a repo.** Create a new GitHub repository (public, so Pages is free) and upload everything in this folder, keeping the folder structure. The `.github` folder is hidden on Mac; press Cmd+Shift+. in Finder to see it.
2. **Add your email.** In `config.json`, replace `you@example.com`. The NWS asks every app to identify itself with a contact.
3. **Set up alerts.**
   - Install **ntfy** on your phone (iOS or Android, free).
   - Tap **+** and subscribe to a topic with a long, unguessable name, like `powderdesk-mr-7k2q9x`. Anyone who knows the name can read it, so don't use something obvious.
   - In the repo: **Settings > Secrets and variables > Actions > New repository secret**. Name `NTFY_TOPIC`, value is your topic name.
4. **(Optional) Turn on Claude's read.** Add a second secret, `ANTHROPIC_API_KEY`, with a key from console.anthropic.com. It uses Claude Haiku at most about four times a day, which costs well under a dollar a month. Without it, the page shows a plain consensus summary instead.
5. **Let the Action save data.** **Settings > Actions > General > Workflow permissions:** choose **Read and write permissions** and save.
6. **Turn on the page.** **Settings > Pages:** Source **Deploy from a branch**, branch **main**, folder **/docs**. Save. GitHub shows your URL after a minute or two.
7. **Make the alert tap open the page.** **Settings > Secrets and variables > Actions > Variables > New repository variable**. Name `DASHBOARD_URL`, value is your Pages URL.
8. **Run it once.** **Actions > Update forecast > Run workflow.** When it finishes, reload the page. Until the first run, the page shows clearly labeled sample data.

## Tuning

All in `config.json`:

| Setting | What it does | Default |
|---|---|---|
| `alerts.local_72h_in` | Alert when a home-range spot's middle forecast for the next 72 h reaches this | 8 in. |
| `alerts.local_day_in` | ...or any single day in the next 7 reaches this | 6 in. |
| `alerts.local_7d_in` | ...or the 7-day total reaches this | 14 in. |
| `alerts.regional_72h_in` | One combined alert when anywhere else hits this in 72 h | 14 in. |
| `alerts.avy_danger_level` | Alert when Logan zone danger reaches this level (4 = High). Avalanche warnings always alert. | 4 |
| `alerts.cooldown_hours` | Won't repeat the same alert sooner than this, unless the forecast grows by `regrow_factor` | 36 h, 1.5x |
| `alerts.nws_events` | NWS alerts that trigger a push | Winter Storm Watch/Warning, Blizzard Warning, Winter Weather Advisory |
| `snow_ratio` | Snow-to-water ratio for model snow. Open-Meteo's raw snowfall assumes about 7:1, which runs light for Utah powder. 10 is a middle-of-the-road default; try 12 if model totals keep coming in low against what Beaver reports. | 10 |
| `spots` | Add, remove or move spots. `group: "local"` puts a spot in the hero switcher and the local alert. `elev_ft` is the elevation the models are adjusted to. | |

## How the data is gathered

| Source | Used for | Notes |
|---|---|---|
| NWS (api.weather.gov) | Logan 7-day forecast, NWS snow grid at local spots, active alerts, Salt Lake City forecast discussion | Free, no key |
| Open-Meteo | GFS, NBM, ECMWF, ICON and GEM model snow for every spot | Free for personal use, no key |
| avalanche.org | Danger rating for every spot's zone (UAC and Bridger-Teton) | Free, no key. Ratings expire on schedule and show as "No rating" once expired. |
| Utah Avalanche Center | The Logan zone's full bottom line text | Best effort from UAC's JSON feed; if it ever changes, the page falls back to the standard travel advice for the rating |
| Wasatch Weather Weenies | Latest post title, a short teaser and link | Blogger feed |
| OpenSnow | Linked only | No public data feed |

If a source fails, the rest of the page still updates and a note at the bottom says which source missed.

## Off-season

Leave it running. With no snow in the forecast nothing alerts, and the UAC section shows "No rating" until rated forecasts start in early winter. To pause it entirely: **Actions > Update forecast > ... > Disable workflow**.
