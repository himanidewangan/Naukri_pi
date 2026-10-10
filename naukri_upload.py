#!/usr/bin/env python3
"""
Naukri resume refresher for Raspberry Pi (Raspberry Pi OS, ARM).
Uses system Chromium + chromedriver (installed by setup_pi.sh),
a persistent browser profile (fewer OTP prompts), and a local state file
for the 3-name rotation. Scheduled with cron.
"""
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
import os
import sys
import json
import shutil
import tempfile
import imaplib
import email
import re
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import socket
import functools
import urllib.request


def use_ipv4(fn):
    """Force IPv4 for the wrapped function (IPv6 handshake gets dropped on some home networks)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        orig = socket.getaddrinfo

        def v4(host, port, family=0, type=0, proto=0, flags=0):
            return orig(host, port, socket.AF_INET, type, proto, flags)

        socket.getaddrinfo = v4
        try:
            return fn(*args, **kwargs)
        finally:
            socket.getaddrinfo = orig
    return wrapper

# ==== 🔐 Hardcoded Configuration (edit these) ====
NAUKRI_EMAIL = "Your naukri email"
NAUKRI_PASSWORD = "your naukri password"

# Gmail account that receives the Naukri OTP (use an App Password)
EMAIL_ADDRESS = "your email"
EMAIL_APP_PASSWORD = "16 didgit app password"

# Account used to send the status email, and who receives it
SENDER_EMAIL = "your email"
SENDER_APP_PASSWORD = "16 didgit app password"
RECEIVER_EMAIL = "your email"

# Discord notifications (preferred). Paste your webhook URL between the quotes.
# Leave empty ("") to use email only.
DISCORD_WEBHOOK_URL = "your discord server webhook"
BASE = Path(__file__).resolve().parent
RESUME_PATH = BASE / "Your_Resume.pdf"   # original; never renamed

RESUME_NAMES = [
    "Name1.pdf",
    "Name2.pdf",
    "Name3.pdf",
]

HEADLESS = True

PROFILE_DIR = BASE / ".chrome_profile"   # keeps you logged in between runs
STATE_FILE = BASE / "state.json"         # remembers which resume name is next
IST = timezone(timedelta(hours=5, minutes=30))


# ==== 🔁 Rotation (state file; advances only after a successful upload) ====
def peek_resume_name():
    idx = 0
    try:
        idx = int(json.loads(STATE_FILE.read_text()).get("next", 0))
    except Exception:
        pass
    return idx % len(RESUME_NAMES), RESUME_NAMES[idx % len(RESUME_NAMES)]


def advance_rotation(idx):
    STATE_FILE.write_text(json.dumps({"next": (idx + 1) % len(RESUME_NAMES)}))


# ==== 🧭 Locate Pi's Chromium + chromedriver ====
def first_existing(paths, which_names=()):
    for p in paths:
        if Path(p).exists():
            return p
    for n in which_names:
        found = shutil.which(n)
        if found:
            return found
    return None


def get_chrome_binary():
    return first_existing(
        ['/usr/bin/chromium-browser', '/usr/bin/chromium'],
        ('chromium-browser', 'chromium'),
    )


def get_chromedriver():
    return first_existing(
        ['/usr/bin/chromedriver',
         '/usr/lib/chromium-browser/chromedriver',
         '/usr/lib/chromium/chromedriver'],
        ('chromedriver',),
    )


def find_otp_inputs(driver):
    upper = "translate({0},'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')"
    attrs = ["@name", "@id", "@placeholder", "@aria-label"]
    cond = " or ".join(f"contains({upper.format(a)},'OTP')" for a in attrs)
    inputs = driver.find_elements(By.XPATH, f"//input[({cond}) and not(@type='hidden')]")
    return [i for i in inputs if i.is_displayed()]


def _extract_body(message):
    body, html_body = '', ''
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == 'attachment':
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or 'utf-8', errors='ignore')
            ctype = part.get_content_type()
            if ctype == 'text/plain' and not body:
                body = text
            elif ctype == 'text/html' and not html_body:
                html_body = text
    else:
        payload = message.get_payload(decode=True)
        if payload:
            text = payload.decode(message.get_content_charset() or 'utf-8', errors='ignore')
            if message.get_content_type() == 'text/html':
                html_body = text
            else:
                body = text
    if not body and html_body:
        body = re.sub(r'<[^>]+>', ' ', html_body)
    return body


@use_ipv4
def fetch_naukri_otp(timeout=120, poll=6):
    """Poll the inbox until a fresh unread Naukri OTP email shows up."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD.replace(" ", ""))
            mail.select('inbox')
            ids = []
            for criteria in ('(UNSEEN FROM "naukri")', '(UNSEEN SUBJECT "OTP")', '(UNSEEN SUBJECT "Verification")'):
                status, data = mail.search(None, criteria)
                if status == 'OK' and data and data[0]:
                    ids = data[0].split()
                    break
            if ids:
                status, data = mail.fetch(ids[-1], '(RFC822)')  # newest; marks it read
                if status == 'OK':
                    message = email.message_from_bytes(data[0][1])
                    text = (message.get('Subject') or '') + ' ' + _extract_body(message)
                    match = re.search(r'\b(\d{4,8})\b', text)
                    if match:
                        mail.logout()
                        return match.group(1)
            mail.logout()
        except Exception as e:
            last_err = e
        time.sleep(poll)
    raise RuntimeError(f'OTP email not received within {timeout}s. Last error: {last_err}')


@use_ipv4
def send_discord(subject, body):
    if not DISCORD_WEBHOOK_URL:
        return False
    payload = json.dumps({"content": f"**{subject}**\n{body}"[:1900]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "naukri-pi-bot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
        print("💬 Discord notification sent." if ok else "❌ Discord returned an error status.")
        return ok
    except Exception as e:
        print("❌ Discord notification failed:", e)
        return False


@use_ipv4
def send_email(subject, body):
    """Notify via Discord if configured; otherwise (or if it fails) email: SSL 465, then STARTTLS 587."""
    if send_discord(subject, body):
        return
    msg = MIMEMultipart()
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    password = SENDER_APP_PASSWORD.replace(" ", "")
    for mode in ("ssl465", "starttls587"):
        try:
            if mode == "ssl465":
                server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30)
            else:
                server = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
                server.starttls()
            server.login(SENDER_EMAIL, password)
            server.send_message(msg)
            server.quit()
            print(f"📧 Status email sent via {mode}.")
            return
        except Exception as e:
            print(f"❌ Email via {mode} failed: {e}")


def build_driver():
    chrome_binary = get_chrome_binary()
    driver_path = get_chromedriver()
    if not chrome_binary or not driver_path:
        raise RuntimeError(
            f"Chromium/chromedriver not found (chromium={chrome_binary}, driver={driver_path}). "
            "Run setup_pi.sh or: sudo apt install chromium-browser chromium-chromedriver"
        )
    print(f"Chromium: {chrome_binary}\nchromedriver: {driver_path}")

    # clear stale lock if a previous run was killed
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (PROFILE_DIR / lock).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    options = webdriver.ChromeOptions()
    options.binary_location = chrome_binary
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")   # Pi's /dev/shm is small
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--window-size=1366,850")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    driver.set_page_load_timeout(90)  # Pi can be slow

    # Remove the "HeadlessChrome" marker but keep the real UA/version/platform
    try:
        ua = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
    except Exception:
        pass
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
    })
    return driver


def is_logged_in(driver):
    driver.get("https://www.naukri.com/mnjuser/profile")
    time.sleep(6)
    url = driver.current_url.lower()
    return 'mnjuser/profile' in url and 'login' not in url


def login(driver, wait):
    driver.get("https://www.naukri.com/")
    time.sleep(3)
    wait.until(EC.element_to_be_clickable((By.ID, "login_Layer"))).click()
    time.sleep(1)

    email_input = wait.until(EC.visibility_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter your active Email ID / Username']")))
    email_input.clear()
    email_input.send_keys(NAUKRI_EMAIL)

    password_input = wait.until(EC.visibility_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter your password']")))
    password_input.clear()
    password_input.send_keys(NAUKRI_PASSWORD)

    driver.find_element(By.XPATH, "//button[@type='submit']").click()
    print("✅ Login form submitted.")
    time.sleep(6)

    otp_inputs = find_otp_inputs(driver)
    if otp_inputs:
        print(f"🔐 Detected {len(otp_inputs)} OTP input(s). Waiting for email OTP...")
        otp_code = fetch_naukri_otp()
        print("🔐 OTP received.")
        if len(otp_inputs) == 1:
            otp_inputs[0].clear()
            otp_inputs[0].send_keys(otp_code)
        else:
            for i, char in enumerate(otp_code):
                if i < len(otp_inputs):
                    otp_inputs[i].clear()
                    otp_inputs[i].send_keys(char)
        try:
            up = "translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')"
            driver.find_element(
                By.XPATH,
                f"//button[@type='submit' or contains({up},'VERIFY') or contains({up},'SUBMIT')]"
            ).click()
        except Exception:
            pass
        time.sleep(6)

    try:
        wait.until(EC.any_of(
            EC.url_contains('mnjuser'),
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'/mnjuser/profile') or contains(text(),'My Naukri')]"))
        ))
    except TimeoutException:
        pass

    if find_otp_inputs(driver):
        raise RuntimeError('Login still requires OTP/verification after submission.')
    if 'login' in driver.current_url.lower() and 'mnjuser' not in driver.current_url.lower():
        raise RuntimeError('Naukri login did not complete. Check credentials or captcha.')
    print("✅ Logged in successfully.")


# ==== 🚀 Main ====
def upload_resume():
    if not RESUME_PATH.exists():
        raise FileNotFoundError(f'Resume file not found at: {RESUME_PATH}')

    idx, new_name = peek_resume_name()
    tmpdir = Path(tempfile.mkdtemp())
    staged = tmpdir / new_name
    shutil.copy(RESUME_PATH, staged)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M} IST] Uploading as: {new_name}")

    driver = build_driver()
    wait = WebDriverWait(driver, 30)

    try:
        if is_logged_in(driver):
            print("✅ Existing session still valid, skipping login.")
        else:
            login(driver, wait)
            driver.get("https://www.naukri.com/mnjuser/profile")
            time.sleep(6)

        upload_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='file']")))
        upload_input.send_keys(str(staged))
        print(f"⬆️ File sent to upload input as {new_name}")

        confirmed = False
        try:
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((
                By.XPATH,
                "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'SUCCESSFULLY UPLOADED')]"
            )))
            confirmed = True
        except TimeoutException:
            print("⚠️ Upload toast not detected (selector/text may have changed).")

        advance_rotation(idx)
        status = "confirmed by Naukri" if confirmed else "sent, but no confirmation toast was seen"
        print(f"✅ Resume upload {status}.")
        send_email(
            "✅ Naukri Resume Upload" if confirmed else "⚠️ Naukri Resume Upload (unconfirmed)",
            f"Hi Himani,\n\nResume upload {status}.\nFile name used: {new_name}\n"
            f"Time (IST): {datetime.now(IST):%Y-%m-%d %H:%M}\n\nRegards,\nYour Pi 🤖"
        )

    except Exception as e:
        print("❌ Error occurred:", e)
        print(traceback.format_exc())
        try:
            driver.save_screenshot(str(BASE / "error.png"))
            (BASE / "error_page.html").write_text(driver.page_source, encoding='utf-8')
        except Exception:
            pass
        send_email("❌ Naukri Resume Upload FAILED", f"Error: {e}\n\nFile name attempted: {new_name}")
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        upload_resume()
    except Exception as e:
        print("❌ Exited with error:", e)
        sys.exit(1)
