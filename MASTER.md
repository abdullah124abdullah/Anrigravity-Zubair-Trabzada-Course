# 🧠 MASTER PROJECT ANALYSIS & CONTEXT

> [!IMPORTANT]
> **Use this file:** Whenever the AI forgets what we are doing or you bring this project to a new Virtual Workspace, read this file so the AI instantly remembers the architecture, goals, and state of the project.

---

## 📍 Project Overview

- **Project Name**: Skool Scraper v5 — Parallel Automation Pipeline
- **Goal**: Scrape ALL unlocked courses from a Skool.com community (AI Workshop), download videos + screenshots, using a Flask dashboard.
- **Tech Stack**: Python 3, Playwright (browser automation), yt-dlp + FFmpeg (video downloading), Flask (web dashboard), SQLite (user data), Google Drive API (cloud sync).
- **Active Script**: `skool_scraper_v5.py`
- **Dashboard**: `app/server.py` (Runs on `http://localhost:5000`)

---

## 🗂️ Clean File Structure

After migrating to the clean Virtual Desktop, this is the expected file structure:

```
📁 Root/
├── MASTER.md                    (This file - Single source of truth)
├── skool_scraper_v5.py          (The Main Engine - Parallel Downloads)
├── cookies.txt                  (Playwright/Netscape cookies for yt-dlp)
├── storage_state.json           (Playwright session persistence)
├── scraper_progress_v4.json     (Tracks completed lesson URLs so we can resume)
├── skool_app.db                 (SQLite — user credentials for Dashboard)
├── google_secrets.json          (Google OAuth client ID/secret)
├── 📁 app/                      (Flask Web Dashboard & API Logic)
│   ├── server.py                (Flask routes + job runner)
│   ├── database.py              (SQLite CRUD operations)
│   ├── gdrive.py                (Google Drive upload/sync functionality)
│   ├── 📁 templates/
│   └── 📁 static/
└── 📁 Course_Downloads/         (Where the downloaded MP4s and PNGs are saved)
```

---

## 🔧 Component-by-Component Breakdown

### 1. `skool_scraper_v5.py` — Parallel Scraper Engine
- **DownloadManager**: Implements a threaded worker pool that allows simultaneous video downloads (no more waiting 10 minutes per lesson to navigate!). It captures the `.m3u8` JWT token instantly, preventing expiration errors.
- **Login/Session**: Logs in using `storage_state.json` and updates `cookies.txt`.
- **Navigation**: Uses `window.__NEXT_DATA__` to map the entire sidebar course structure. Avoids text-matching bugs by using direct indexes and URLs.
- **Processing**:
  1. Clicks play to trigger network m3u8 stream.
  2. Takes a full-page Chrome screenshot.
  3. Sends m3u8 stream to background `yt-dlp` thread directly to `Course_Downloads/`.
- **Resuming**: Highly intelligent. Checks `scraper_progress_v4.json` AND physically checks if the targeted `.mp4` / `.part` file exists in the directory.

### 2. `app/server.py` — Flask Web Dashboard
- Run locally via `python -m app.server` then open `localhost:5000`.
- Provides an active Terminal/Log output directly in the browser by capturing Python `print` statements.
- Allows connecting Google Drive integration and adding Skool credentials securely without changing code files.

### 3. Key Selectors & Mechanics
- Course cards: `[class*='CourseLinkWrapper']`
- Video Stream: Skool uses JWT-authenticated `.m3u8` streams originating from `stream.video.skool.com`. They expire in 60s, requiring immediate download.
- Play Button Logic: Requires `page.mouse.click()` (isTrusted=true) because Skool's Javascript rejects simulated Playwright clicks.

---

## 🎯 Next Steps / Why We Migrated Here
This workspace was cleaned to migrate it to a Virtual Desktop instance. The massive git history and messy v1-v4 files were removed so the Antigravity instance can work exclusively from this Master File and `v5`. 

**To Start Work in the new Virtual Desktop:**
1. Setup Python Virtual Environment
2. Run standard `pip install -r requirements.txt` (if exists) or install `flask playwright yt-dlp` manually.
3. Type `python -m app.server` to execute the code.
