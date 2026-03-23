# 📚 Seat Scraper Bot — Course Seat Tracker for CollegeScheduler

A Discord bot that monitors course section seat availability on [CollegeScheduler](https://collegescheduler.com)-powered university scheduling systems and notifies you when seats open up.

> Originally built for **Texas A&M University** (`tamu.collegescheduler.com`), but adaptable to any institution using the CollegeScheduler SaaS platform.

---

## How It Works

```
CollegeScheduler API
        ↓
  Selenium (authenticated session)
        ↓
  PostgreSQL (Neon or self-hosted)
        ↓
  Discord Bot (notifications + commands)
```

1. **Selenium** authenticates with your university's SSO (Microsoft login + MFA) and scrapes CollegeScheduler's internal API for section data.
2. **PostgreSQL** stores section records and a list of sections you're actively monitoring.
3. Every **10 minutes**, the bot checks for seat changes and sends a Discord notification if anything changed.
4. **Discord commands** let you add/remove tracked sections and query status on demand.

---

## Prerequisites

- Python 3.10+
- Google Chrome + matching [ChromeDriver](https://googlechromelabs.github.io/chrome-for-testing/)
- A PostgreSQL database ([Neon](https://neon.tech) free tier works great)
- A [Discord bot token](https://discord.com/developers/applications)
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
DISCORD_CHANNEL_ID=your_target_channel_id
```

Your Neon host, user, dbname, and password are all available under **Connection Details** on the Neon dashboard.  
`GUILD_ID` is your Discord server ID — enable Developer Mode in Discord settings, then right-click your server icon and select *Copy Server ID*.

> ⚠️ **Never commit your `.env` file.** Add it to `.gitignore`.

---

## Database Schema

Run `schema.sql` against your database to initialize all tables:

```bash
psql "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require" -f schema.sql
```

Or paste the contents directly into the Neon SQL Editor.

The schema creates four tables: `sections`, `monitored_sections`, `scrape_config`, and `channels`. The `channels` table is present in the dump but not actively used by the current bot — you can ignore it.

After running the schema, seed your initial scrape configuration:

```sql
INSERT INTO scrape_config (key, value) VALUES
    ('TERMS',    '["Spring%202026%20-%20College%20Station"]'),
    ('SUBJECTS', '["CSCE", "MATH", "PHYS"]'),
    ('COURSES',  '{"CSCE": [331, 313], "MATH": [308], "PHYS": [207]}');
```

Update these to match the courses you want to monitor. Terms must be URL-encoded (spaces as `%20`).

---

## Installation

```bash
git clone https://github.com/yourusername/classfinder.git
cd classfinder

python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

pip install discord.py selenium psycopg2-binary python-dotenv aiohttp
```

> The `requirements.txt` in this repo is the full Azure deployment manifest and includes packages not needed for local use. Install only what's listed above for a clean local setup.

---

## Running Locally

```bash
python class_finder.py
```

On startup, the bot will:
1. Log into your university's CollegeScheduler via Selenium
2. Begin polling every 10 minutes
3. Broadcast a Discord message with your MFA verification code if one is detected during login

### MFA Note

This project handles Microsoft Authenticator's **number-matching MFA**. When you see a message like:

> 🔐 Verification code detected: **42**

...approve the matching number in your Authenticator app. The bot will continue login automatically once MFA is approved.

---

## Discord Commands

| Command | Description |
|---|---|
| `!track <section_id>` | Start monitoring a section for seat changes |
| `!untrack <section_id>` | Stop monitoring a section |
| `!status` | List all currently tracked sections |
| `!status <section_id>` | Show seat count and last update for a specific section |
| `!config show <KEY>` | Display current scraping config (`TERMS`, `SUBJECTS`, or `COURSES`) |
| `!config add <KEY> <value>` | Add a term, subject, or course to the scrape list |
| `!config remove <KEY> <value>` | Remove an entry from the scrape list |
| `!config replace <KEY> <value>` | Replace an entire config list |

**Config examples:**
```
!config add SUBJECTS ENGL
!config add COURSES CSCE 421
!config show TERMS
!config replace TERMS Fall%202026%20-%20College%20Station
```

> ⚠️ **Anyone in your Discord server can run `!config` commands.** This is an intentional design tradeoff for simplicity. If you're running this in a public server, consider restricting these commands to a specific role.

---

## Hosting Options

### Option A — Local Machine

Best for personal use. Run it on any always-on machine (desktop, Raspberry Pi, etc.).

**Pros:** Free, simple, no cloud setup  
**Cons:** Stops if the machine goes offline; Selenium needs a real or headless Chrome install

### Option B — Cloud VM (recommended for reliability)

Deploy to a low-cost cloud VM (e.g., Azure B1s, AWS t3.micro, Oracle Free Tier).

```bash
# Install Chrome on Ubuntu
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update && apt-get install -y google-chrome-stable

# Run with nohup or a systemd service
nohup python class_finder.py &
```

### Option C — Docker + Azure Functions (original deployment)

The included `requirements.txt` reflects this setup. You can containerize the bot using a `Dockerfile` with Chrome installed:

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    google-chrome-stable wget gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install discord.py selenium psycopg2-binary python-dotenv aiohttp

CMD ["python", "class_finder.py"]
```

Deploy the container to **Azure Container Instances** or **Azure App Service** and set your environment variables in the Azure portal under *Configuration > Application Settings*.

**Pros:** Always-on, scalable, no local Chrome needed  
**Cons:** Azure free tier doesn't cover container hosting long-term; costs ~$5–15/month on the lowest tiers

---

## Adapting for Other CollegeScheduler Schools

The scraper targets `tamu.collegescheduler.com`. To use this at another institution:

1. Find your school's CollegeScheduler subdomain (e.g., `utexas.collegescheduler.com`)
2. Update the URLs in `generate_urls()` and `fetch_all_data()`:
   ```python
   # Replace tamu.collegescheduler.com with your school's domain
   url = f"https://yourschool.collegescheduler.com/api/terms/{term}/..."
   ```
3. Check whether your school uses the same Microsoft SSO login flow. If not, the `login()` function will need to be adapted to your SSO provider's selectors.
4. The API path structure (`/api/terms/{term}/subjects/{subject}/courses/{course}/regblocks`) is standard across CollegeScheduler deployments, but verify it for your institution.

---

## Known Limitations

- **No session recovery.** If Selenium crashes mid-run, the bot stops scraping until the process is restarted. There is currently no `!relogin` command.
- **MFA is manual.** You must approve the MFA prompt on your phone each time the bot restarts. Sessions are not persisted.
- **Traditional F2F only.** The scraper filters out non-F2F sections. Edit `process_json()` to change this behavior.
- **No per-user tracking.** All tracked sections are shared across the Discord channel.
- **Rate limits.** The bot does not enforce Discord command rate limits. Spam protection is not implemented.

---

## License

MIT — free to use, modify, and redistribute. See `LICENSE` for details.

---

## Contributing

Pull requests welcome. If you adapt this for a new university, consider opening a PR to document the changes needed for your school's SSO and CollegeScheduler setup.
