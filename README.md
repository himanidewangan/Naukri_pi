# 🤖📄 Naukri Resume Refresher

> *Because manually re-uploading your resume every few hours is not a personality trait, it's a cry for automation.*

A tiny Raspberry Pi robot that logs into Naukri, re-uploads my resume under a **rotating file name**, fetches the **2FA code from my mailbox** when asked, and then pings me on **Discord** to say "done, human. go touch grass."

---

## 😩 The Problem

Job portals love fresh profiles. The folk wisdom: recruiters search by recency, so a recently updated profile floats up and a stale one sinks.

So my routine became:

1. Open Naukri.
2. Upload resume.
3. Wait.
4. Do it again in a few hours.
5. Forget. Feel guilty. Repeat.

A constant, boring, *very automatable* loop. So I automated it.

---

## ✨ What It Does

- ⬆️ Uploads your resume to Naukri on a schedule (cron)
- 🔁 Rotates **3 different file names** each run (profile looks lively, your original file is never touched)
- 📬 Handles **email 2FA/OTP** by reading it from Gmail over IMAP
- 🍪 Reuses a **saved browser session**, so most runs skip login and OTP entirely
- 💬 Sends a **Discord webhook** (or email fallback) on success *and* failure
- 📸 Saves `error.png` + `error_page.html` when something breaks, so debugging isn't guesswork

---

## 🏗️ Architecture

```
            ┌──────────────┐
            │  cron (Pi)   │  every 2h: 9,11,13,...,23 IST
            └──────┬───────┘
                   │
            ┌──────▼───────┐
            │   run.sh     │  flock → no overlapping runs
            └──────┬───────┘
                   │
        ┌──────────▼───────────┐
        │   naukri_upload.py   │
        └──┬──────┬─────────┬──┘
           │      │         │
 ┌─────────▼─┐ ┌──▼──────┐ ┌▼────────────┐
 │ state.json│ │Chromium │ │ Gmail IMAP  │
 │ (which    │ │headless │ │ (grab OTP   │
 │  name is  │ │+ saved  │ │  if asked)  │
 │  next?)   │ │ profile │ └─────────────┘
 └───────────┘ └──┬──────┘
                  │  login (if needed) → upload renamed copy
                  ▼
             naukri.com
                  │
        success / failure
                  ▼
        Discord webhook  ──(fallback)──▶  Email
```

**One run, step by step**

1. cron fires `run.sh`; `flock` makes sure two runs never collide.
2. Script reads `state.json` to see which of the 3 names is next.
3. It copies your resume to a temp folder under that name (original stays untouched).
4. Headless Chromium opens Naukri using a **persistent profile**. Still logged in? Skip to step 6.
5. Not logged in? Log in. If Naukri asks for an OTP, poll Gmail for it and type it in.
6. Upload the renamed copy, wait for the "successfully uploaded" confirmation.
7. Advance the rotation **only if** the upload went through, then notify Discord.

---

## 🧠 Design Decisions

| Decision | Why |
|---|---|
| **Raspberry Pi + cron** over GitHub Actions | Cloud runners get a fresh browser and a datacenter IP every time, which is the fastest route to captchas and constant OTPs. A Pi at home has a stable IP and a persistent browser profile. |
| **System Chromium + chromedriver** (apt) | `webdriver-manager` doesn't fetch ARM Linux drivers. The distro packages are matched to each other and just work. |
| **Persistent Chrome profile** | Stay logged in between runs, so OTP is the exception, not the rule. |
| **Rotation state in `state.json`** | Cloud runners are stateless, a Pi isn't. A tiny file beats cleverness. |
| **Advance rotation only on success** | A failed run retries the same name instead of silently skipping it. |
| **Copy-then-rename** | The original resume is never modified. Upload a disposable copy, delete it after. |
| **OTP via IMAP polling** | The email lands seconds later. The script waits up to 2 minutes instead of failing on the first empty inbox. |
| **Discord webhook, not a bot** | A webhook is one URL and one HTTPS POST. No bot to host, no extra dependency. |
| **IPv4 forced for mail connections** | On my network, IPv6 connections to Gmail connected but died mid-handshake. Forcing IPv4 sidesteps it. |
| **`flock` in `run.sh`** | If a slow Pi run overlaps the next one, the second just exits. No two Chromiums fighting over one profile. |
| **Screenshot + HTML dump on failure** | Websites change. When selectors break, I want evidence, not vibes. |

---

## 🧱 Why Chromium Can't Be Removed (I Tried to Imagine It)

"Just call their API with `requests`, it'd be so much lighter!" Great instinct, honestly. Here's why I didn't:

1. **There's no official API** for candidates to upload resumes. Anything I'd call is a private, undocumented endpoint that can change any day.
2. **Login is JavaScript-driven.** Tokens and cookies get minted by client-side scripts. A plain HTTP client has to reverse-engineer all of it.
3. **Bot protection** checks browser fingerprints. A bare HTTP client is the *first* thing it rejects.
4. **The OTP step is interleaved** with the login flow, which makes a hand-rolled client even more fragile.
5. **Fragility trade-off:** a browser breaks when the *page* changes (fix: update a selector). A reverse-engineered client breaks when *anything* changes (fix: capture traffic and start over).

Chromium is the landlord. I'm just renting. 🏠

I made it cheap instead: headless, minimal flags, persistent profile, one short run every couple of hours. A Pi 4 handles it fine.

---

## 🚀 Setup (Raspberry Pi OS)

```bash
# 1. Put these in one folder on the Pi
#    naukri_upload.py, setup_pi.sh, <your_resume>.pdf

# 2. Fill in the config at the top of naukri_upload.py
#    (Naukri login, Gmail app password, resume path/names, Discord webhook)

# 3. One-time setup: installs Chromium, chromedriver, a venv + selenium, run.sh
bash setup_pi.sh

# 4. Test a manual run
./run.sh && tail -n 30 naukri.log
```

> ⚠️ **Verify cron got installed:** run `crontab -l`. You should see one line pointing at `run.sh`. If it's empty, add it yourself (below). Ask me how I know. 😅

### Schedule (every 2 hours, 9:00 to 23:00)

```bash
(crontab -l 2>/dev/null | grep -v "naukri/run.sh"; echo "0 9,11,13,15,17,19,21,23 * * * $HOME/naukri/run.sh") | crontab -
```

Make sure the Pi's timezone is right so those hours mean *your* hours: `sudo timedatectl set-timezone Asia/Kolkata`

### Config you need

| Setting | Notes |
|---|---|
| Naukri email + password | Your login |
| Gmail **App Password** | 2-Step Verification on, then create one at myaccount.google.com/apppasswords |
| `RESUME_PATH` | Your real resume PDF |
| `RESUME_NAMES` | 3 file names to rotate through |
| `DISCORD_WEBHOOK_URL` | Server Settings → Integrations → Webhooks. Empty = email only |

### 🔒 Do NOT commit your secrets

The config lives at the top of the script, so keep your real values **local** and commit only a placeholder version. Add a `.gitignore`:

```
state.json
.chrome_profile/
naukri.log
error.png
error_page.html
venv/
run.sh
```

If you ever paste a password or webhook anywhere public, rotate it. Immediately. (Yes, a webhook URL counts as a password.)

---

## 🩹 Gotchas I Hit So You Don't Have To

- **Cron entry missing** after setup → check `crontab -l`.
- **"Connection unexpectedly closed" sending email** → suspected IPv6 weirdness on my network; the mail code now forces IPv4.
- **Selectors drifting** → Naukri tweaks its UI. Check `error_page.html`, update the XPath, move on.
- **Captcha** → set `HEADLESS = False`, run once with a screen or VNC, solve it by hand. The saved profile remembers.
- **Low-RAM Pi** → add some swap if Chromium gets OOM-killed.

---

## 🔮 Future Improvements

- [ ] Move secrets to a `.env` file or the system keyring (goodbye hardcoded config)
- [ ] Run as a **systemd timer** instead of cron, with proper retry/backoff
- [ ] **Health check / dead-man's switch**: alert me if *no* successful upload in N hours
- [ ] Smarter confirmation: verify the "last updated" timestamp on the profile, not only the toast
- [ ] Jittered schedule so runs aren't on the exact hour, every hour, forever
- [ ] Dockerize it (ARM image) for easier setup
- [ ] Support other portals behind a small adapter interface
- [ ] Optional Prometheus/Grafana panel because every project secretly wants a dashboard

---

## ⚖️ Disclaimer

This is a personal hobby project. Automating third-party websites may violate their terms of service, and the site can respond with captchas, logouts, or account restrictions. Use it on **your own account**, at your own risk, and don't hammer anything. More runs ≠ better results; a couple of refreshes a day is usually plenty.

---

## 🛠️ Stack

Python · Selenium · Chromium (headless) · IMAP/SMTP · Discord Webhooks · cron · `flock` · Raspberry Pi 4

## 📄 License

MIT. Fork it, break it, make it better.

---

*Built by a human who got tired of being a cron job. ⏰*
