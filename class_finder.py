import asyncio
import json
import os
import re
import time
import requests
import psycopg2
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

import discord
from discord.ext import commands
from discord import app_commands

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Use threads for blocking Selenium operations
executor = ThreadPoolExecutor(max_workers=2)

# ---- DATABASE CONNECTION ----
def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv('NEON_DBNAME'),
        user=os.getenv('NEON_USER'),
        password=os.getenv('NEON_PASSWORD'),
        host=os.getenv('NEON_HOST')
    )

# ---- CONFIG (TERMS only) ----
def fetch_config():
    conn = get_db_connection()
    config = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM scrape_config")
            for key, value in cur.fetchall():
                config[key] = value
    finally:
        conn.close()
    return config


def update_config(key, value):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scrape_config (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value
            """, (key, json.dumps(value)))
            conn.commit()
    finally:
        conn.close()

# ---- CONFIG CACHE ----
_config_cache = {
    "data": None,
    "timestamp": 0
}
CACHE_TTL = 300  # seconds

def fetch_config_cached(force_refresh=False):
    global _config_cache
    if not force_refresh and _config_cache["data"] and (time.time() - _config_cache["timestamp"] < CACHE_TTL):
        return _config_cache["data"]
    config = fetch_config()
    _config_cache["data"] = config
    _config_cache["timestamp"] = time.time()
    return config


def invalidate_config_cache():
    global _config_cache
    _config_cache = {"data": None, "timestamp": 0}

# ---- SEAT CACHE ----
_seat_cache: dict = {}  # {crn: open_seats}

def get_cached_seats(crn: str):
    """Return cached open_seats for a CRN, or None if not cached."""
    return _seat_cache.get(crn)

def get_all_cached_seats() -> dict:
    """Return a shallow copy of the full seat cache."""
    return dict(_seat_cache)

def update_seat_cache(crn: str, open_seats: int):
    _seat_cache[crn] = open_seats

def invalidate_seat_cache(crn: str = None):
    """Evict one CRN or clear the entire cache if crn is None."""
    global _seat_cache
    if crn is None:
        _seat_cache = {}
    else:
        _seat_cache.pop(crn, None)

def init_seat_cache():
    """Populate _seat_cache from sections for tracked CRNs only."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.crn, s.open_seats
                FROM sections s
                JOIN monitored_sections ms ON s.crn = ms.crn
            """)
            rows = cur.fetchall()
        for crn, open_seats in rows:
            update_seat_cache(str(crn), open_seats)
        print(f"[Cache] Initialized seat cache with {len(rows)} entries.")
    except Exception as e:
        print(f"[Cache] Failed to initialize seat cache: {e}")
    finally:
        conn.close()

# ---- API SESSION (requests with Selenium cookies) ----
def _build_requests_session(driver):
    """Extract auth cookies from Selenium into a requests.Session."""
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'])
    session.headers['User-Agent'] = driver.execute_script("return navigator.userAgent")
    return session


async def _refresh_api_session(driver):
    """Refresh bot.api_session with current Selenium cookies (called after each scrape)."""
    bot.api_session = await asyncio.get_running_loop().run_in_executor(
        None, _build_requests_session, driver
    )


def fetch_course_json_http(session, term, subject, course):
    """Hit the CollegeScheduler API using auth cookies (no Selenium navigation)."""
    if str(subject).upper() == 'KINE':
        url = f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/15/regblocks"
    else:
        url = f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/regblocks"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json(), url

# ---- DISCORD: COURSE CHECKING LOOP ----
async def check_courses():
    try:
        conn = get_db_connection()
        print("Checking tracked seats...")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ms.crn, ms.open_seats AS old_seats, ms.discord_uids,
                       s.open_seats AS current_seats, s.subject, s.course, s.term
                FROM monitored_sections ms
                JOIN sections s ON ms.crn = s.crn
            """)
            monitored = cur.fetchall()

        for crn, old_seats, discord_uids, current_seats, subject, course, term in monitored:
            if old_seats != current_seats:
                print(f"[DEBUG] {crn} old:{old_seats} -> new:{current_seats}")
                for uid in (discord_uids or []):
                    try:
                        user = bot.get_user(uid) or await bot.fetch_user(uid)
                        if user:
                            await user.send(
                                f"🚨 Seat change detected!\n"
                                f"**{subject} {course} ({term})**\n"
                                f"CRN: {crn}\n"
                                f"Seats: {old_seats} → {current_seats}"
                            )
                    except Exception as dm_err:
                        print(f"[DM] Failed to notify user {uid}: {dm_err}")

                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE monitored_sections
                        SET open_seats = %s
                        WHERE crn = %s
                    """, (current_seats, crn))
                    conn.commit()

    except Exception as e:
        print(f"Error checking courses: {e}")
    finally:
        conn.close()

# ---- OWNER GUARD ----
def _is_owner(interaction: discord.Interaction) -> bool:
    owner_uid = os.getenv('DISCORD_OWNER_UID')
    if not owner_uid:
        return False
    return interaction.user.id == int(owner_uid)

# ---- SLASH COMMANDS ----
@bot.tree.command(name="track", description="Track a CRN for seat availability alerts")
@app_commands.describe(
    subject="Subject code (e.g. CSCE)",
    course="Course number (e.g. 331)",
    crn="Course Registration Number"
)
async def track(interaction: discord.Interaction, subject: str, course: int, crn: str):
    await interaction.response.defer(thinking=True)
    subject = subject.upper()
    uid = interaction.user.id

    # 1. CRN already in seat cache — it's tracked by at least one user
    if get_cached_seats(crn) is not None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT discord_uids FROM monitored_sections WHERE crn = %s", (crn,))
                row = cur.fetchone()
                if row:
                    uids = row[0] or []
                    if uid in uids:
                        await interaction.followup.send(f"You're already tracking CRN {crn}.", ephemeral=True)
                        return
                    uids.append(uid)
                    cur.execute(
                        "UPDATE monitored_sections SET discord_uids = %s WHERE crn = %s",
                        (json.dumps(uids), crn)
                    )
                    conn.commit()
                    await interaction.followup.send(f"Now tracking CRN {crn} ✅", ephemeral=True)
                    return
        finally:
            conn.close()

    # 2. CRN not in cache — verify via API across all active terms
    config = fetch_config_cached()
    terms = config.get("TERMS", [])
    found_section = None
    found_term = None

    for term_str in terms:
        try:
            data, _ = fetch_course_json_http(bot.api_session, term_str, subject, course)
            for section in data.get("sections", []):
                if section["id"] == crn and section["instructionMode"] == "Traditional Face-to-Face (F2F)":
                    found_section = section
                    found_term = term_str
                    break
            if found_section:
                break
        except Exception:
            continue

    if not found_section:
        await interaction.followup.send(
            f"❌ CRN {crn} not found under {subject} {course} in any active term.",
            ephemeral=True
        )
        return

    # 3. Insert into sections and monitored_sections
    open_seats = found_section["openSeats"]
    instructor = found_section["instructor"][0]["name"] if found_section["instructor"] else None
    times = [
        {"days": m["days"], "start": f"{m['startTime']:04d}", "end": f"{m['endTime']:04d}"}
        for m in found_section["meetings"] if m["meetingType"] == "LEC"
    ]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sections (crn, term, subject, course, open_seats, instructor, times, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (crn) DO UPDATE SET
                    open_seats = EXCLUDED.open_seats,
                    times = EXCLUDED.times,
                    last_updated = EXCLUDED.last_updated
            """, (crn, found_term, subject, course, open_seats, instructor, json.dumps(times)))

            # Fetch or create monitored_sections row, then add uid (no-dup)
            cur.execute("SELECT discord_uids FROM monitored_sections WHERE crn = %s", (crn,))
            ms_row = cur.fetchone()
            if ms_row:
                uids = ms_row[0] or []
                if uid not in uids:
                    uids.append(uid)
                cur.execute(
                    "UPDATE monitored_sections SET discord_uids = %s, open_seats = %s WHERE crn = %s",
                    (json.dumps(uids), open_seats, crn)
                )
            else:
                cur.execute(
                    "INSERT INTO monitored_sections (crn, open_seats, discord_uids) VALUES (%s, %s, %s)",
                    (crn, open_seats, json.dumps([uid]))
                )

            conn.commit()
        update_seat_cache(crn, open_seats)
        await interaction.followup.send(
            f"Now tracking CRN {crn} ({subject} {course}) — {open_seats} seats open ✅",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="untrack", description="Stop tracking a CRN")
@app_commands.describe(crn="Course Registration Number to stop tracking")
async def untrack(interaction: discord.Interaction, crn: str):
    uid = interaction.user.id
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_uids FROM monitored_sections WHERE crn = %s", (crn,))
            row = cur.fetchone()
            if not row:
                await interaction.response.send_message(
                    f"⚠️ CRN {crn} is not being tracked.", ephemeral=True
                )
                return

            uids = row[0] or []
            if uid not in uids:
                await interaction.response.send_message(
                    f"⚠️ You are not tracking CRN {crn}.", ephemeral=True
                )
                return

            uids.remove(uid)
            if uids:
                cur.execute(
                    "UPDATE monitored_sections SET discord_uids = %s WHERE crn = %s",
                    (json.dumps(uids), crn)
                )
            else:
                cur.execute("DELETE FROM monitored_sections WHERE crn = %s", (crn,))
                invalidate_seat_cache(crn)

            conn.commit()
        await interaction.response.send_message(f"🗑️ Stopped tracking CRN {crn}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="status", description="Show all CRNs you are currently tracking")
async def status(interaction: discord.Interaction):
    uid = interaction.user.id
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.crn, s.subject, s.course, s.open_seats, s.term
                FROM monitored_sections ms
                JOIN sections s ON ms.crn = s.crn
                WHERE ms.discord_uids @> %s::jsonb
            """, (json.dumps([uid]),))
            results = cur.fetchall()

        if not results:
            await interaction.response.send_message(
                "You are not tracking any CRNs.", ephemeral=True
            )
            return

        embed = discord.Embed(title="Your Tracked CRNs", color=discord.Color.green())
        for crn, subject, course, open_seats, term in results:
            embed.add_field(
                name=f"{subject} {course} ({term})",
                value=f"CRN: {crn}\nOpen Seats: {open_seats}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="config", description="Manage active terms for scraping (owner only)")
@app_commands.describe(action="Action to perform", term="Term string (required for add/remove)")
@app_commands.choices(action=[
    app_commands.Choice(name="show", value="show"),
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def config_command(interaction: discord.Interaction, action: str, term: str = None):
    if not _is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return

    config = fetch_config()
    terms = config.get("TERMS", [])

    if action == "show":
        await interaction.response.send_message(
            f"📘 Active TERMS:\n```{json.dumps(terms, indent=2)}```", ephemeral=True
        )
    elif action == "add":
        if not term:
            await interaction.response.send_message("❌ Provide a term string.", ephemeral=True)
            return
        if term not in terms:
            terms.append(term)
            update_config("TERMS", terms)
            invalidate_config_cache()
        await interaction.response.send_message(f"✅ Added term: `{term}`", ephemeral=True)
    elif action == "remove":
        if not term or term not in terms:
            await interaction.response.send_message("❌ Term not found.", ephemeral=True)
            return
        terms.remove(term)
        update_config("TERMS", terms)
        invalidate_config_cache()
        await interaction.response.send_message(f"✅ Removed term: `{term}`", ephemeral=True)

# ---- SCRAPING ----
def generate_urls():
    """Derive scrape URLs from currently tracked sections (not from scrape_config)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT s.term, s.subject, s.course
                FROM monitored_sections ms
                JOIN sections s ON ms.crn = s.crn
            """)
            combos = cur.fetchall()
    finally:
        conn.close()

    urls = []
    for term, subject, course in combos:
        if str(subject).upper() == 'KINE':
            urls.append(f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/15/regblocks")
        else:
            urls.append(f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/regblocks")
    return urls


def process_json(json_data):
    """Parse API responses, keeping only tracked CRNs."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT crn FROM monitored_sections")
            tracked_crns = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    normalized = []
    for entry in json_data:
        for section in entry["data"]["sections"]:
            if section["instructionMode"] != "Traditional Face-to-Face (F2F)":
                continue
            if section["id"] not in tracked_crns:
                continue
            normalized.append({
                "crn": section["id"],
                "term": entry["url"].split("/")[5],
                "subject": entry["url"].split("/")[7],
                "course": entry["url"].split("/")[9],
                "open_seats": section["openSeats"],
                "instructor": section["instructor"][0]["name"] if section["instructor"] else None,
                "times": [{
                    "days": meeting["days"],
                    "start": f"{meeting['startTime']:04d}",
                    "end": f"{meeting['endTime']:04d}",
                } for meeting in section["meetings"] if meeting["meetingType"] == "LEC"],
                "last_updated": entry["timestamp"],
            })
    return normalized


# ---- VERIFY CODE BROADCAST ----
async def discord_broadcast_code(bot, code):
    """DM the bot owner the MFA verification code."""
    try:
        owner_uid = os.getenv("DISCORD_OWNER_UID")
        if not owner_uid:
            print("DISCORD_OWNER_UID not set — cannot DM verification code.")
            return
        user = bot.get_user(int(owner_uid)) or await bot.fetch_user(int(owner_uid))
        if user:
            await user.send(f"🔐 Verification code: **{code}**")
            print("Verification code DM'd to owner.")
        else:
            print("Could not find owner user to DM.")
    except Exception as e:
        print(f"Error sending verification DM: {e}")


# ---- LOGIN ----
def login(email, password):
    chrome_options = Options()
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=chrome_options)

    try:
        driver.get('https://tamu.collegescheduler.com/')

        WebDriverWait(driver, 25).until(EC.visibility_of_element_located((By.NAME, "loginfmt")))
        driver.find_element(By.NAME, "loginfmt").send_keys(email + Keys.RETURN)

        WebDriverWait(driver, 25).until(EC.visibility_of_element_located((By.NAME, "passwd")))
        driver.find_element(By.NAME, "passwd").send_keys(password + Keys.RETURN)

        time.sleep(2)
        WebDriverWait(driver, 25).until(
            EC.visibility_of_element_located((By.ID, "idSIButton9"))
        ).click()
        print("Continue button clicked... waiting for verification.")

        try:
            WebDriverWait(driver, 30).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "span.code-text"))
            )
            code_element = driver.find_element(By.CSS_SELECTOR, "span.code-text")
            code = code_element.text.strip()
            if re.fullmatch(r'\d{3}', code):
                print(f"Found verification code: {code}")
                asyncio.run_coroutine_threadsafe(
                    discord_broadcast_code(bot, code), bot.loop
                )
            else:
                print(f"Invalid code format detected: {code}")
        except Exception as wait_err:
            print(f"Verification code not detected: {wait_err}")

        try:
            WebDriverWait(driver, 65).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(., 'Yes, this is my device')]")
                )
            ).click()
        except Exception:
            print("No MFA prompt detected.")

        print("Login flow complete.")
        return driver

    except Exception as e:
        print(f"Login error: {e}")
        driver.quit()
        return None


# ---- DATABASE WRITE ----
def store_data(data):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            skipped = 0
            for entry in data:
                crn = entry["crn"]
                open_seats = entry["open_seats"]
                cached = get_cached_seats(crn)
                update_seat_cache(crn, open_seats)  # always keep cache current
                if cached is not None and cached == open_seats:
                    skipped += 1
                    continue  # skip DB write — seats unchanged
                cur.execute("""
                    INSERT INTO sections (crn, term, subject, course, open_seats, instructor, times, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (crn) DO UPDATE SET
                        open_seats = EXCLUDED.open_seats,
                        times = EXCLUDED.times,
                        last_updated = EXCLUDED.last_updated
                """, (
                    crn,
                    entry["term"],
                    entry["subject"],
                    entry["course"],
                    open_seats,
                    entry["instructor"],
                    json.dumps(entry["times"]),
                    entry["last_updated"],
                ))
        conn.commit()
        print(f"[Cache] {skipped} entries skipped (no seat change).")
    finally:
        conn.close()


def fetch_all_data(driver):
    urls = generate_urls()
    if not urls:
        print("No tracked CRNs — skipping scrape.")
        return []
    data_collected = []
    driver.get("https://tamu.collegescheduler.com/schedule/current")
    time.sleep(10)
    for url in urls:
        try:
            driver.get(url)
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "pre")))
            content = driver.page_source
            start, end = content.find("{"), content.rfind("}") + 1
            json_data = json.loads(content[start:end])
            data_collected.append({
                "url": url,
                "data": json_data,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"Collected data for {url}")
        except Exception as e:
            print(f"Error scraping {url}: {e}")
    return data_collected


def job(driver):
    print("Running scheduled job...")
    all_data = fetch_all_data(driver)
    formatted = process_json(all_data)
    if formatted:
        store_data(formatted)
        print(f"Stored {len(formatted)} records.")
    else:
        print("No valid data found.")
    asyncio.run_coroutine_threadsafe(_refresh_api_session(driver), bot.loop)


async def run_job_loop(driver):
    """Continuously run job(driver) every 2 minutes asynchronously."""
    while True:
        try:
            await asyncio.sleep(30)
            await asyncio.get_running_loop().run_in_executor(executor, job, driver)
            await check_courses()
        except Exception as e:
            print(f"Job loop error: {e}")
        await asyncio.sleep(120)  # 2 minutes


@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    loop = asyncio.get_running_loop()

    # Sync slash commands immediately — before Selenium login
    try:
        guild = discord.Object(id=int(os.getenv('GUILD_ID')))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as sync_err:
        print(f"Failed to sync commands: {sync_err}")

    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")

    if not email or not password:
        print("Missing credentials in .env — skipping login.")
        return

    driver = await loop.run_in_executor(None, lambda: login(email, password))
    if driver:
        print("Login succeeded; driver stored on bot instance.")
        bot.driver = driver
        bot.api_session = await loop.run_in_executor(None, _build_requests_session, driver)
        await loop.run_in_executor(None, init_seat_cache)
        asyncio.create_task(run_job_loop(driver))
    else:
        print("Login failed — driver not started.")


# ---- MAIN ----
if __name__ == "__main__":
    load_dotenv()
    bot.run(os.getenv("DISCORD_TOKEN"))
