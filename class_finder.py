import asyncio
import json
import os
import re
import time
import psycopg2
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

import discord
from discord.ext import commands, tasks

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

def fetch_config():
    """Fetch SCRAPE_CONFIG from the database."""
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
    """Update or insert configuration key/value in the database."""
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
CACHE_TTL = 300  # seconds (5 minutes)

def fetch_config_cached(force_refresh=False):
    """Fetch configuration with simple in-memory caching."""
    global _config_cache

    # Check cache age
    if not force_refresh and _config_cache["data"] and (time.time() - _config_cache["timestamp"] < CACHE_TTL):
        return _config_cache["data"]

    # Otherwise, fetch from DB
    config = fetch_config()
    _config_cache["data"] = config
    _config_cache["timestamp"] = time.time()
    return config


def invalidate_config_cache():
    """Clear config cache after manual updates."""
    global _config_cache
    _config_cache = {"data": None, "timestamp": 0}

# ---- DISCORD: COURSE CHECKING LOOP ----
async def check_courses():
    try:
        conn = get_db_connection()
        print("Updating seats")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ms.section_id, ms.open_seats AS old_seats,
                       s.open_seats AS current_seats, s.subject, s.course, s.term
                FROM monitored_sections ms
                JOIN sections s ON ms.section_id = s.section_id
                WHERE ms.active = TRUE
            """)
            monitored_sections = cur.fetchall()

        for section in monitored_sections:
            section_id, old_seats, current_seats, subject, course, term = section
            if old_seats != current_seats:
                print(f"[DEBUG] {section_id} old:{old_seats} -> new:{current_seats}")
                channel = bot.get_channel(int(os.getenv('DISCORD_CHANNEL_ID')))
                if channel:
                    await channel.send(
                        f"🚨 Seat change detected!\n"
                        f"**{subject} {course} ({term})**\n"
                        f"Section ID: {section_id}\n"
                        f"Seats: {old_seats} → {current_seats}"
                    )

                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE monitored_sections
                        SET open_seats = %s
                        WHERE section_id = %s
                    """, (current_seats, section_id))
                    conn.commit()

    except Exception as e:
        print(f"Error checking courses: {e}")
    finally:
        conn.close()


# ---- DISCORD COMMANDS ----
@bot.command()
async def track(ctx, section_id: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT section_id FROM sections WHERE section_id = %s", (section_id,))
            if not cur.fetchone():
                await ctx.send("Section not found ❌")
                return

            cur.execute("""
                INSERT INTO monitored_sections (section_id, channel_id, open_seats)
                VALUES (%s, %s, (SELECT open_seats FROM sections WHERE section_id = %s))
                ON CONFLICT (section_id) DO NOTHING
            """, (section_id, ctx.channel.id, section_id))
            conn.commit()
            await ctx.send(f"Now tracking section {section_id} ✅")
    except Exception as e:
        await ctx.send(f"Error: {e}")
    finally:
        conn.close()

@bot.command()
async def untrack(ctx, section_id: str):
    """Permanently stop tracking a section (delete from database)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Delete the section from tracked sections
            cur.execute("""
                DELETE FROM monitored_sections
                WHERE section_id = %s
                RETURNING section_id
            """, (section_id,))
            deleted = cur.fetchone()
            conn.commit()

            if deleted:
                await ctx.send(f"🗑️ Deleted tracking record for section {section_id}.")
            else:
                await ctx.send(f"⚠️ No section found with ID {section_id}.")
    except Exception as e:
        await ctx.send(f"Error removing section: {e}")
    finally:
        conn.close()


@bot.command()
async def status(ctx, section_id: str = None):
    """Check current section status — or show all tracked."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if section_id:
                # Single course lookup
                cur.execute("""
                    SELECT subject, course, term, open_seats, last_updated 
                    FROM sections 
                    WHERE section_id = %s
                """, (section_id,))
                result = cur.fetchone()
                if result:
                    subject, course, term, open_seats, last_updated = result
                    embed = discord.Embed(
                        title=f"{subject} {course} Status",
                        description=f"**Section:** {section_id}",
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="Open Seats", value=open_seats)
                    embed.add_field(name="Last Updated", value=last_updated.strftime("%m/%d %H:%M"))
                    embed.add_field(name="Term", value=term)
                    await ctx.send(embed=embed)
                else:
                    await ctx.send("Section not found ❌")
            else:
                # Show all tracked sections
                cur.execute("""
                    SELECT s.section_id, s.subject, s.course, s.open_seats, s.term
                    FROM monitored_sections ms
                    JOIN sections s ON ms.section_id = s.section_id
                    WHERE ms.active = TRUE
                """)
                results = cur.fetchall()
                if not results:
                    await ctx.send("No active tracked sections found ❌")
                    return

                embed = discord.Embed(
                    title="Active Tracked Sections",
                    color=discord.Color.green()
                )
                for section_id, subject, course, open_seats, term in results:
                    embed.add_field(
                        name=f"{subject} {course} ({term})",
                        value=f"Section ID: {section_id}\nOpen Seats: {open_seats}",
                        inline=False
                    )
                await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error fetching status: {str(e)}")
    finally:
        conn.close()

@bot.command(name="config")
async def config_scrape(ctx, action: str, key: str, *values):
    """Adjust scraping configuration dynamically via DB.
    Examples:
      !config add SUBJECTS ENGL
      !config remove COURSES CSCE 213
      !config replace TERMS Fall%202026%20-%20College%20Station
      !config show SUBJECTS
    """
    key = key.upper()
    valid_keys = ["TERMS", "SUBJECTS", "COURSES"]

    if key not in valid_keys:
        await ctx.send("Invalid key. Use TERMS, SUBJECTS, or COURSES.")
        return

    try:
        config = fetch_config()

        if action.lower() == "add":
            if key == "COURSES":
                subj, *nums = values
                nums = [int(n) for n in nums]
                config["COURSES"].setdefault(subj, []).extend(nums)
            else:
                config[key].extend(values)

        elif action.lower() == "remove":
            if key == "COURSES":
                subj, *nums = values
                if subj in config["COURSES"]:
                    config["COURSES"][subj] = [
                        n for n in config["COURSES"][subj] if n not in map(int, nums)
                    ]
            else:
                config[key] = [v for v in config[key] if v not in values]

        elif action.lower() == "replace":
            if key == "COURSES":
                subj, *nums = values
                config["COURSES"][subj] = [int(n) for n in nums]
            else:
                config[key] = list(values)

        elif action.lower() == "show":
            await ctx.send(f"📘 Current {key}: {json.dumps(config[key], indent=2)}")
            return

        else:
            await ctx.send("Unknown action. Use add, remove, replace, or show.")
            return

        # Save updated config to DB
        update_config(key, config[key])
        invalidate_config_cache()
        await ctx.send(f"✅ Updated {key} successfully.")

    except Exception as e:
        await ctx.send(f"Error updating config: {e}")

def wake_event_loop(loop):
    """Force the asyncio event loop in another thread to get processor time."""
    loop.call_soon_threadsafe(lambda: None)
    time.sleep(0.05)  # micro-yield from main thread

def generate_urls():
    """Generate URLs using cached config (auto-refresh every 5 min)."""
    config = fetch_config_cached()
    urls = []
    for term in config["TERMS"]:
        for subject in config["SUBJECTS"]:
            for course in config["COURSES"].get(subject, []):
                if subject == 'KINE':
                    url = f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/15/regblocks"
                else:
                    url = f"https://tamu.collegescheduler.com/api/terms/{term}/subjects/{subject}/courses/{course}/regblocks"
                urls.append(url)
    return urls


def process_json(json_data):
    normalized = []
    for entry in json_data:
        for section in entry["data"]["sections"]:
            if section["instructionMode"] != "Traditional Face-to-Face (F2F)":
                continue
            normalized.append({
                "section_id": section["id"],
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
    """Send verification code to Discord asynchronously."""
    try:
        channel = bot.get_channel(int(os.getenv("DISCORD_CHANNEL_ID")))
        if channel:
            await channel.send(f"🔐 Verification code detected: **{code}**")
            print("Verification message sent to Discord.")
        else:
            print("Discord channel not found.")
    except Exception as e:
        print(f"Error sending Discord broadcast: {e}")


# ---- FINAL LOGIN WITH PARALLEL CODE DETECTION ----
def login(email, password):
    chrome_options = Options()
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=chrome_options)

    try:
        driver.get('https://tamu.collegescheduler.com/')

        # Email and password
        WebDriverWait(driver, 25).until(EC.visibility_of_element_located((By.NAME, "loginfmt")))
        driver.find_element(By.NAME, "loginfmt").send_keys(email + Keys.RETURN)

        WebDriverWait(driver, 25).until(EC.visibility_of_element_located((By.NAME, "passwd")))
        driver.find_element(By.NAME, "passwd").send_keys(password + Keys.RETURN)

        # Click Continue button
        time.sleep(2)
        WebDriverWait(driver, 25).until(
            EC.visibility_of_element_located((By.ID, "idSIButton9"))
        ).click()
        print("Continue button clicked... waiting for verification.")

        # --- NEW: Inline verification check ---
        try:
            # Wait for verification div to appear
            WebDriverWait(driver, 30).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "div.verification-code"))
            )
            code_element = driver.find_element(By.CSS_SELECTOR, "div.verification-code")
            code = code_element.text.strip()
            if re.fullmatch(r'\d{3}', code):
                print(f"Found verification code: {code}")
                # Trigger Discord broadcast without pausing Selenium
                asyncio.run_coroutine_threadsafe(
                    discord_broadcast_code(bot, code), bot.loop
                )
            else:
                print(f"Invalid code format detected: {code}")
        except Exception as wait_err:
            print(f"Verification code not detected: {wait_err}")
        # --------------------------------------

        # Continue MFA and login procedures
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
            for entry in data:
                cur.execute("""
                    INSERT INTO sections (section_id, term, subject, course, open_seats, instructor, times, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (section_id) DO UPDATE SET
                        open_seats = EXCLUDED.open_seats,
                        times = EXCLUDED.times,
                        last_updated = EXCLUDED.last_updated
                """, (
                    entry["section_id"],
                    entry["term"],
                    entry["subject"],
                    entry["course"],
                    entry["open_seats"],
                    entry["instructor"],
                    json.dumps(entry["times"]),
                    entry["last_updated"],
                ))
        conn.commit()
    finally:
        conn.close()


def fetch_all_data(driver):
    urls = generate_urls()
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

async def run_job_loop(driver):
    """Continuously run job(driver) every 10 minutes asynchronously."""
    while True:
        try:
            await asyncio.sleep(30)
            await asyncio.get_running_loop().run_in_executor(executor, job, driver)
            await check_courses()
        except Exception as e:
            print(f"Job loop error: {e}")
        await asyncio.sleep(600)  # 10 minutes


@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")



    # Begin Selenium login asynchronously so Discord remains responsive
    loop = asyncio.get_running_loop()
    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")

    if not email or not password:
        print("Missing credentials in .env — skipping login.")
        return

    # Kick off Selenium login in a non-blocking executor
    driver = await loop.run_in_executor(None, lambda: login(email, password))
    if driver:
        print("Login succeeded; driver stored on bot instance.")
        bot.driver = driver
        # Start the job loop (your recurring scraping function)
        asyncio.create_task(run_job_loop(driver))
    else:
        print("Login failed — driver not started.")



# ---- MAIN ----
if __name__ == "__main__":
    load_dotenv()
    bot.run(os.getenv("DISCORD_TOKEN"))
