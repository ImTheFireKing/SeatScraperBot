# 📚 Seat Scraper Bot — Course Seat Tracker for CollegeScheduler

A Discord bot that monitors course section seat availability on [CollegeScheduler](https://collegescheduler.com)-powered university scheduling systems and notifies you via DM when seats open up.

> Originally built for **Texas A&M University** (`tamu.collegescheduler.com`), but adaptable to any institution using the CollegeScheduler SaaS platform.

---

## How It Works

```
CollegeScheduler API
        ↓
  Selenium (authenticated session)
        ↓
  In-memory seat cache
        ↓  (only on change)
  PostgreSQL (Neon or self-hosted)
        ↓
  Discord DM (per-user notifications)
```

1. **Selenium** authenticates with your university's SSO (Microsoft/Duo login + MFA) and maintains an authenticated session for scraping.
2. **Per-user tracking** — each Discord user independently tracks CRNs they care about. The bot only scrapes courses that have at least one active watcher.
3. Every **2 minutes**, the bot checks tracked CRNs for seat changes. An **in-memory seat cache** skips database writes when nothing changed, keeping DB load low.
4. **Discord DMs** are sent directly to each user watching a CRN when its seat count changes.
5. **Slash commands** let any server member track/untrack their own CRNs and check their personal status.

---

## Prerequisites

- Python 3.10+
- Google Chrome + matching [ChromeDriver](https://googlechromelabs.github.io/chrome-for-testing/)
- A PostgreSQL database ([Neon](https://neon.tech) free tier works great)
- A [Discord bot token](https://discord.com/developers/applications) with the `applications.commands` scope
- A university account on a CollegeScheduler-powered platform

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```env
# University SSO credentials
EMAIL=yournetid@university.edu
PASSWORD=your_sso_password

# PostgreSQL (Neon or self-hosted)
NEON_DBNAME=your_db_name
NEON_USER=your_db_user
NEON_PASSWORD=your_db_password
NEON_HOST=your_db_host

# Discord
DISCORD_TOKEN=your_discord_bot_token
GUILD_ID=your_server_id
DISCORD_OWNER_UID=your_discord_user_id
```

- `GUILD_ID` — right-click your server icon in Discord → *Copy Server ID* (requires Developer Mode in Discord settings)
- `DISCORD_OWNER_UID` — right-click your own name in Discord → *Copy User ID* (requires Developer Mode)

> ⚠️ **Never commit your `.env` file.** Add it to `.gitignore`.

---

## Database Setup

### 1. Initialize the schema

Run `schema.sql` against your database to create the base tables:

```bash
psql "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" -f schema.sql
```

Or paste the contents directly into the Neon SQL Editor.

### 2. Apply migrations

Two migration files must be applied in order after the base schema:

```bash
# Renames section_id → crn
psql "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" -f migration_crn.sql

# Adds per-user discord_uids tracking; removes channel_id and active columns
psql "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" -f migration_v2.sql
```

### 3. Seed the active term

The bot only needs a list of active terms — courses and subjects are now auto-managed as users track CRNs:

```sql
INSERT INTO scrape_config (key, value) VALUES
    ('TERMS', '["Spring%202026%20-%20College%20Station"]');
```

Terms must be URL-encoded (spaces as `%20`). You can find the exact term string in your school's CollegeScheduler URL after navigating to a course.

---

## Installation

```bash
git clone https://github.com/yourusername/seatscraper.git
cd seatscraper

python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

---

## Discord Bot Setup

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. Create a bot and copy its token into `.env` as `DISCORD_TOKEN`
2. Under **OAuth2 → URL Generator**, check both scopes:
   - `bot`
   - `applications.commands` ← required for slash commands
3. Under **Bot Permissions**, check `Send Messages` (needed for DMs)
4. Use the generated URL to invite the bot to your server

> If you previously invited the bot without `applications.commands`, re-invite it using the new URL — Discord will add the scope without removing the bot.

---

## Running

```bash
python class_finder.py
```

On startup the bot will:
1. Register slash commands to your server instantly
2. Log into CollegeScheduler via Selenium (this takes 30–90 seconds due to MFA)
3. Load tracked CRN seat counts into the in-memory cache
4. Begin the 2-minute scrape loop

### MFA

During login, if a Duo number-matching verification code is detected, the bot DMs it directly to your Discord account (the `DISCORD_OWNER_UID`):

> 🔐 Verification code: **260**

Approve the matching number in the Duo Mobile app. The bot continues automatically once MFA is approved.

---

## Slash Commands

All command responses are **ephemeral** — only visible to the person who ran the command.

| Command | Who can use | Description |
|---|---|---|
| `/track <subject> <course> <crn>` | Anyone | Start receiving seat alerts for a CRN |
| `/untrack <crn>` | Anyone | Stop receiving alerts for a CRN |
| `/status` | Anyone | Show all CRNs you are personally tracking |
| `/config <action> [term]` | Owner only | Manage active terms for scraping |

### `/track` details

```
/track subject:CSCE course:331 crn:12345
```

- Checks the in-memory cache first (instant if the CRN is already watched by someone)
- If not cached, hits the CollegeScheduler API to verify the CRN exists under that course
- On success: adds you to the watchers list and begins including this CRN in scrape cycles
- Invalid subject/course/CRN combinations return an error

### `/untrack` details

```
/untrack crn:12345
```

- Removes you from the watchers list for that CRN
- If you were the last watcher, the CRN is fully removed from tracking and the cache

### `/status` details

```
/status
```

Returns an embed showing every CRN you are currently watching with its current open seat count.

### `/config` details (owner only)

Manages the list of active terms used when verifying new CRNs via `/track`. SUBJECTS and COURSES no longer need to be configured manually — they are derived automatically from what users track.

```
/config action:show
/config action:add  term:Fall%202026%20-%20College%20Station
/config action:remove term:Spring%202026%20-%20College%20Station
```

---

## Seat Alerts

When a tracked CRN's seat count changes, **every user watching that CRN** receives a Discord DM:

```
🚨 Seat change detected!
**CSCE 331 (Spring%202026%20-%20College%20Station)**
CRN: 12345
Seats: 0 → 1
```

---

## Hosting Options

### Option A — Local Machine

Best for personal use. Run it on any always-on machine (desktop, Raspberry Pi, etc.).

**Pros:** Free, simple, no cloud setup  
**Cons:** Stops if the machine goes offline; Selenium needs Chrome installed

### Option B — Cloud VM (recommended for reliability)

Deploy to a low-cost cloud VM (e.g., Azure B1s, AWS t3.micro, Oracle Free Tier).

```bash
# Install Chrome on Ubuntu
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update && apt-get install -y google-chrome-stable

# Run as a background process
nohup python class_finder.py &
```

### Option C — Docker

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    google-chrome-stable wget gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install -r requirements.txt

CMD ["python", "class_finder.py"]
```

---

## Adapting for Other CollegeScheduler Schools

The scraper targets `tamu.collegescheduler.com`. To use this at another institution:

1. Find your school's CollegeScheduler subdomain (e.g., `utexas.collegescheduler.com`)
2. Update all URL strings in `generate_urls()` and `fetch_course_json_http()` to use your school's domain
3. Check whether your school uses the same Microsoft SSO + Duo login flow. If not, `login()` will need its element selectors updated to match your SSO provider's page
4. The API path structure (`/api/terms/{term}/subjects/{subject}/courses/{course}/regblocks`) is standard across CollegeScheduler deployments, but verify it for your institution

---

## Known Limitations

- **No session recovery.** If Selenium crashes mid-run, the scrape loop stops until the process is restarted. There is no auto-relogin.
- **MFA is manual.** You must approve the MFA prompt on your phone each time the bot restarts. Sessions are not persisted between runs.
- **Traditional F2F only.** The scraper filters out non-face-to-face sections. Edit the `process_json()` filter to change this.
- **Single Selenium instance.** The `/track` CRN verification uses a `requests` session derived from the Selenium cookies, so it does not block the scrape loop. However, if the Selenium session expires between bot restarts, `/track` verification calls will fail until the bot is restarted.

---

## License

MIT — free to use, modify, and redistribute.

---

## Contributing

Pull requests welcome. If you adapt this for a new university, consider opening a PR to document the changes needed for your school's SSO and CollegeScheduler setup.
