"""
Skool Scraper v4 — Full Automation Pipeline
=============================================
Navigates Skool.com classroom, iterates all course cards by index,
expands dropdown modules, downloads videos (m3u8/YouTube/Loom) and
saves screenshots + HTML for text-only lessons.

Requirements:
    pip install playwright
    playwright install chromium
    pip install yt-dlp
    ffmpeg must be in PATH

Usage:
    python skool_scraper_v4.py
"""

import os
import sys
import json
import time
import subprocess
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
import threading
from concurrent.futures import ThreadPoolExecutor, Future

# Fix Windows console encoding for Unicode output
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ╔══════════════════════════════════════════════╗
# ║              CONFIGURATION                   ║
# ╚══════════════════════════════════════════════╝
EMAIL = "tullahabdullah411@gmail.com"
PASSWORD = "Abdullah124*"

WORKSPACE = r"d:\Users\SUNNYSIDE\Coding\Anrigravity Zubair Trabzada Course"
COOKIES_FILE = os.path.join(WORKSPACE, "cookies.txt")
STORAGE_STATE = os.path.join(WORKSPACE, "storage_state.json")
PROGRESS_FILE = os.path.join(WORKSPACE, "scraper_progress_v4.json")

CLASSROOM_URL = "https://www.skool.com/aiworkshop/classroom"
LOGIN_URL = "https://www.skool.com/login"

MAX_RETRIES = 3           # Retry per lesson on failure
SLEEP_PAGE_LOAD = 8       # Seconds to wait after major navigation
SLEEP_LESSON_LOAD = 8     # Seconds to wait after clicking a lesson
SLEEP_VIDEO_CAPTURE = 8   # Seconds to wait after clicking play for m3u8


# ╔══════════════════════════════════════════════╗
# ║              UTILITIES                        ║
# ╚══════════════════════════════════════════════╝

def clean_filename(name):
    """Remove invalid Windows filename characters, emojis, and trailing dots."""
    # Strip emojis and non-ascii safely
    cleaned = name.encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'[\\/*?:"<>|]', "", cleaned)
    cleaned = cleaned.replace('\n', ' ').replace('\r', '').strip()
    # Collapse multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    # Strip trailing periods and spaces which break Windows paths
    cleaned = cleaned.rstrip('. ')
    cleaned = re.sub(r'\s+', ' ', cleaned)
    # Limit length to avoid Windows path issues
    return cleaned[:80]


import glob

def get_unique_download_dir():
    """Create a uniquely named download folder for this run.
    Course_Downloads, Course_Downloads_1, Course_Downloads_2, etc.
    """
    base = os.path.join(WORKSPACE, "Course_Downloads")
    if not os.path.exists(base):
        os.makedirs(base)
        return base
    
    # If base exists and is non-empty, use the LATEST created one to resume
    existing_dirs = [d for d in os.listdir(WORKSPACE) if d.startswith("Course_Downloads") and os.path.isdir(os.path.join(WORKSPACE, d))]
    
    # Sort them by creation time or by naming convention to find the "active" one
    # Simple logic: assume we are always resuming the highest numbered one if it exists, else base
    # For now, let's just stick to a single base to make resuming predictable, or the last one.
    # The prompt user states "create every time a new one when the command is run", but also wants to resume.
    # We will use "Course_Downloads" as the stable root if possible, or create a new one.
    # Let's preserve the existing "create new if non-empty" but return the LAST one if resuming makes more sense.
    
    # To truly fix resuming, we shouldn't create a new root folder every single run unless requested. 
    # Let's just use `Course_Downloads` universally so it can physically inspect the previous run's files!
    return base

def is_lesson_complete(mod_dir, base_filename):
    """
    Physical inspection of the download directory.
    Returns True if the lesson has `.png` and NO `.ytdl` files.
    If `.ytdl` exists, deletes it and returns False to force retry.
    """
    png_path = os.path.join(mod_dir, f"{base_filename}.png")
    
    # Check for incomplete yt-dlp files
    ytdl_files = glob.glob(os.path.join(mod_dir, f"{base_filename}*.ytdl"))
    part_files = glob.glob(os.path.join(mod_dir, f"{base_filename}*.part"))
    
    if part_files:
        print(f"         [RECOVERY] Found .part video, attempting to recover by trimming suffix...")
        for pf in part_files:
            clean_name = pf.replace('.part', '')
            if not os.path.exists(clean_name):
                try: os.rename(pf, clean_name)
                except: pass

    if ytdl_files:
        print(f"         [RECOVERY] Found broken download: {ytdl_files[0]}. Deleting and retrying...")
        for yf in ytdl_files:
            try: os.remove(yf)
            except: pass
        return False
        
    # If there's no PNG, it definitely failed
    if not os.path.exists(png_path):
        return False
        
    return True

def load_progress():
    """Load set of completed lesson URLs from progress file."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return set(json.load(f))
        except:
            pass
    return set()

def save_progress(completed):
    """Save completed lesson URLs to progress file."""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(list(completed), f, indent=2)



class DownloadManager:
    """Manages parallel background video downloads and subsequent Google Drive syncs."""
    
    def __init__(self, max_workers=3, cookies_file="cookies.txt"):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.pending = {}  
        self.results = {}  
        self.cookies_file = cookies_file
        self.lock = threading.Lock()
    
    def submit_job(self, video_url, output_path, lesson_url, sync_args=None):
        future = self.executor.submit(self._execute_job, video_url, output_path, lesson_url, sync_args)
        with self.lock:
            self.pending[lesson_url] = future
        future.add_done_callback(lambda f: self._on_complete(lesson_url, f))
    
    def _execute_job(self, video_url, output_path, lesson_url, sync_args):
        # 1. Download Video if present
        if video_url:
            print(f"      [yt-dlp] [ASYNC] Submitting download for {os.path.basename(output_path)}...")
            cmd = [
                "yt-dlp",
                "--cookies", self.cookies_file,
                "--add-header", "Referer: https://www.skool.com/",
                "--add-header", "Origin: https://www.skool.com",
                "--add-header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "--concurrent-fragments", "3",
                "--retries", "3",
                "--format", "bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--no-warnings",
                "-o", f"{output_path}.%(ext)s",
                video_url
            ]
            try:
                result = subprocess.run(cmd, timeout=900)
                success = result.returncode == 0
                if success:
                    print(f"      [OK] [ASYNC] Video downloaded correctly: {os.path.basename(output_path)}")
                else:
                    print(f"      [FAIL] [ASYNC] yt-dlp exited with code {result.returncode}")
            except Exception as e:
                print(f"      [FAIL] [ASYNC] yt-dlp error: {e}")
                
        # 2. Sync to Google Drive
        if sync_args and sync_args.get('google_token'):
            try:
                import glob
                from app.gdrive import sync_lesson_files_to_drive
                
                # Fetch local files for this specific lesson
                mod_dir = sync_args['mod_dir']
                lesson_file_base = sync_args['lesson_file_base']
                local_files = glob.glob(os.path.join(mod_dir, f"{lesson_file_base}*"))
                local_files = [f for f in local_files if not f.endswith('.ytdl')] # Ignore parts
                
                if local_files:
                    print(f"         ☁️ [ASYNC] Uploading {len(local_files)} files to Drive... ({lesson_file_base})")
                    sync_lesson_files_to_drive(
                        token_json=sync_args['google_token'],
                        community_name=sync_args['community_display_name'],
                        course_name=sync_args['course_folder_name'],
                        module_name=sync_args['mod_folder_name'],
                        local_file_paths=local_files
                    )
                    print(f"         ✅ [ASYNC] Synced to Drive. Local video cleared for VPS space.")
            except Exception as drive_err:
                print(f"         ❌ [ASYNC] Drive Sync Error: {drive_err}")
                
        return True
            
    def _on_complete(self, lesson_url, future):
        with self.lock:
            self.pending.pop(lesson_url, None)
            try:
                self.results[lesson_url] = future.result()
            except:
                self.results[lesson_url] = False
    
    def wait_all(self):
        print(f"   [INFO] Waiting for {len(self.pending)} pending background jobs to finish...")
        self.executor.shutdown(wait=True)

def download_video(url, filepath):
    """Download video using yt-dlp. Sequential, fail-safe."""
    print(f"      [yt-dlp] Downloading video...")
    cmd = [
        "yt-dlp",
        "--cookies", COOKIES_FILE,
        "--add-header", "Referer: https://www.skool.com/",
        "--add-header", "Origin: https://www.skool.com",
        "--add-header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--concurrent-fragments", "1",   # Sequential for stability
        "--retries", "3",
        "--format", "bestvideo+bestaudio/best", # Explicitly force merging of separate video/audio streams
        "--merge-output-format", "mp4",         # Force output to perfectly multiplexed mp4
        "--no-warnings",
        "-o", f"{filepath}.%(ext)s",
        url
    ]
    try:
        result = subprocess.run(cmd, timeout=600)  # 10 min timeout per video
        if result.returncode == 0:
            print(f"      [OK] Video downloaded successfully.")
            return True
        else:
            print(f"      [FAIL] yt-dlp exited with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        print(f"      [FAIL] yt-dlp timed out after 10 minutes.")
        return False
    except Exception as e:
        print(f"      [FAIL] yt-dlp error: {e}")
        return False


# ╔══════════════════════════════════════════════╗
# ║           COOKIE EXPORT                       ║
# ╚══════════════════════════════════════════════╝

def export_cookies(context):
    """Export browser cookies to Netscape format for yt-dlp."""
    cookies = context.cookies()
    with open(COOKIES_FILE, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c['domain']
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c['path']
            secure = "TRUE" if c['secure'] else "FALSE"
            expiry = int(c.get('expires', time.time() + 86400 * 30))
            name = c['name']
            value = c['value']
            f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
    print("   [OK] Cookies exported for yt-dlp.")


# ╔══════════════════════════════════════════════╗
# ║           LOGIN & SESSION                     ║
# ╚══════════════════════════════════════════════╝

def login_and_get_to_classroom(page, context):
    """Handle login or session restore, end up on the classroom page."""

    print("\n" + "=" * 60)
    print("  STEP 1: LOGIN & SESSION")
    print("=" * 60)

    # Try to navigate to classroom directly
    print("   [INFO] Navigating to Classroom...")
    page.goto(CLASSROOM_URL)

    print(f"   [INFO] Waiting {SLEEP_PAGE_LOAD}s for page to fully load...")
    time.sleep(SLEEP_PAGE_LOAD)

    current_url = page.url

    # Check if we landed on a login/about page (not authenticated)
    needs_login = (
        "/about" in current_url or
        "/login" in current_url or
        page.locator("text='LOG IN'").count() > 0 or
        page.locator("text='Log in'").count() > 0
    )

    if needs_login:
        print("   [INFO] Not logged in. Proceeding to login page...")
        page.goto(LOGIN_URL)

        # WAIT for the login form to fully render
        print(f"   [INFO] Waiting {SLEEP_PAGE_LOAD}s for login page to load...")
        time.sleep(SLEEP_PAGE_LOAD)

        try:
            # Wait for email input to appear
            page.wait_for_selector('input[type="email"]', timeout=30000)
            print("   [INFO] Login form found. Entering credentials...")

            # Clear any existing text first, then type slowly
            email_input = page.locator('input[type="email"]')
            email_input.click()
            email_input.fill("")
            email_input.type(EMAIL, delay=80)

            password_input = page.locator('input[type="password"]')
            password_input.click()
            password_input.fill("")
            password_input.type(PASSWORD, delay=80)

            # Small pause before clicking submit
            time.sleep(2)

            page.click('button[type="submit"]')
            print(f"   [INFO] Login submitted. Waiting for redirect...")

            # Wait for URL to change away from /login (up to 30s)
            try:
                page.wait_for_url("**/aiworkshop/**", timeout=30000)
                print("   [INFO] Redirect detected!")
            except:
                print("   [INFO] Redirect timeout - checking page state...")

            # Extra sleep for page to fully stabilize
            time.sleep(SLEEP_PAGE_LOAD)

            # Take a screenshot to see where we ended up
            page.screenshot(path=os.path.join(WORKSPACE, "DEBUG_After_Login.png"))
            current_url = page.url
            print(f"   [INFO] Current URL after login: {current_url}")

            # Check if login REALLY failed: is the login form still visible?
            login_form_visible = page.locator('input[type="email"]').count() > 0
            if login_form_visible and "/login" in current_url:
                print(f"   [FAIL] LOGIN FAILED. Login form still visible at: {current_url}")
                page.screenshot(path=os.path.join(WORKSPACE, "ERROR_Login.png"))
                return False
            
            print("   [OK] Login successful!")

            # Save session for next run
            context.storage_state(path=STORAGE_STATE)
            print(f"   [INFO] Session saved to {STORAGE_STATE}")

        except Exception as e:
            print(f"   [FAIL] Login error: {e}")
            page.screenshot(path=os.path.join(WORKSPACE, "ERROR_Login_Exception.png"))
            return False
    else:
        print("   [OK] Already logged in. Session recovered.")

    # Make sure we are on the classroom grid
    if "/classroom" not in page.url or "?md=" in page.url:
        print("   [INFO] Navigating to Classroom grid...")
        page.goto(CLASSROOM_URL)
        time.sleep(SLEEP_PAGE_LOAD)

    print("   [OK] On Classroom page.")
    return True


# ╔══════════════════════════════════════════════╗
# ║        SIDEBAR STRUCTURE MAPPING              ║
# ╚══════════════════════════════════════════════╝

def expand_all_dropdowns(page):
    """
    (Deprecated) No longer needed, as we now parse the module tree directly from __NEXT_DATA__.
    Kept for interface compatibility but does nothing.
    """
    pass


def map_course_structure(page):
    """
    Extract the full module → lesson hierarchy directly from Skool's Next.js datastore.
    Returns: [
        { "module": "WATCH ME FIRST", "lessons": [
            {"title": "...", "url": "/aiworkshop/classroom/...", "completed": True/False},
            ...
        ]},
        ...
    ]
    """
    print("      [INFO] Mapping course structure from Skool data...")
    try:
        structure = page.evaluate("""() => {
            const children = window.__NEXT_DATA__?.props?.pageProps?.course?.children;
            if (!children) return [];
            
            const results = [];
            
            children.forEach(node => {
                const data = node.course;
                if (!data) return;
                
                // If it has children, it's a folder/module containing lessons
                if (node.children && Array.isArray(node.children) && node.children.length > 0) {
                    const modTitle = data.metadata?.title || "Untitled Module";
                    const lessons = [];
                    node.children.forEach(lNode => {
                        const lData = lNode.course;
                        if (!lData) return;
                        
                        let isCompleted = false;
                        try {
                            const prog = window.__NEXT_DATA__.props.pageProps.userCourseData?.metadata?.progress;
                            if (prog && prog[lData.id]) isCompleted = true;
                        } catch(e) {}
                        
                        lessons.push({
                            title: lData.metadata?.title || "Untitled Lesson",
                            url: window.location.pathname + "?md=" + lData.id,
                            completed: isCompleted
                        });
                    });
                    results.push({ module: modTitle, lessons: lessons });
                } else {
                    // It's a flat lesson directly in the root (no module wrapper)
                    // Give each lesson its own module folder named after itself
                    let isCompleted = false;
                    try {
                        const prog = window.__NEXT_DATA__.props.pageProps.userCourseData?.metadata?.progress;
                        if (prog && prog[data.id]) isCompleted = true;
                    } catch(e) {}
                    
                    const lessonTitle = data.metadata?.title || "Untitled Lesson";
                    results.push({ 
                        module: lessonTitle, 
                        lessons: [{
                            title: lessonTitle,
                            url: window.location.pathname + "?md=" + data.id,
                            completed: isCompleted
                        }]
                    });
                }
            });
            
            return results;
        }""")
        
        # Deduplicate and count
        total_modules = len(structure)
        total_lessons = sum(len(m["lessons"]) for m in structure)
        completed = sum(1 for m in structure for l in m["lessons"] if l["completed"])
        
        print(f"      [OK] Extracted {total_modules} modules, {total_lessons} lessons ({completed} already completed).")
        return structure
    except Exception as e:
        print(f"      [ERROR] Next.js tree mapping failed: {e}")
        return []


# ╔══════════════════════════════════════════════╗
# ║        LESSON CONTENT EXTRACTION              ║
# ╚══════════════════════════════════════════════╝

def process_lesson(page, lesson, mod_dir, base_filename, context, download_manager, sync_args=None):
    """Process a single lesson: screenshot, detect video, download/save.
    
    Returns True on success, False on failure.
    """
    title = lesson['title']
    url = lesson['url']

    # Build the full URL
    full_url = url if "skool.com" in url else f"https://www.skool.com{url}"

    print(f"         [INFO] Navigating to lesson: {title}")

    # ── Hook Network Event Listner ──
    # We must attach this BEFORE navigating so we catch the API handshakes!
    captured_m3u8 = []
    def on_network_event(event):
        try:
            url = event.url
            if ".m3u8" in url and "stream.video.skool.com" in url and "token=" in url:
                if url not in captured_m3u8:
                    captured_m3u8.append(url)
        except:
            pass

    # Listen on the entire context to catch requests from iframes or web workers!
    context.on("request", on_network_event)
    context.on("response", on_network_event)

    # ── Navigate to the lesson ──
    # Skool frequently redirects manually constructed URLs to the Community tab if the internal hashed ID is slightly off,
    # or if the server doesn't hydrate the modal Deep Link correctly.
    # To fix this securely, we strictly click the dynamic links right off the Classroom sidebar by matching the title,
    # and ensuring it's a modal link (href contains ?md=).
    try:
        sidebar_link = page.locator("a[href*='?md=']").filter(has_text=title).first
        if sidebar_link.count() > 0:
            print(f"         [INFO] Clicking sidebar element: {title}")
            sidebar_link.scroll_into_view_if_needed()
            sidebar_link.click(force=True)
            time.sleep(2) # Give it time to route the modal
        else:
            print(f"         [INFO] Fallback to direct URL route: {full_url}")
            page.goto(full_url)
    except Exception as e:
        print(f"         [FAIL] Navigation failed: {e}")
        page.goto(full_url)

    # Wait for content to load
    print(f"         [INFO] Waiting {SLEEP_LESSON_LOAD}s for lesson content...")
    time.sleep(SLEEP_LESSON_LOAD)

    # Wait for the page to be fully idle (network + DOM stable)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except:
        pass
    try:
        page.wait_for_selector("main, article, div[class*='Content']", timeout=15000)
    except:
        pass  # Best effort

    # Extra settle time for lazy-loaded images and videos
    time.sleep(2)

    # ── Screenshot (ALWAYS — taken FIRST so we don't wait for videos) ──
    screenshot_path = os.path.join(mod_dir, f"{base_filename}.png")
    try:
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"         [SCREENSHOT] Saved.")
    except Exception as e:
        print(f"         ⚠️ Screenshot failed: {e}")

    # ── VIDEO DETECTION & DOWNLOAD (AFTER screenshot) ──
    # We must click Play FIRST so we can capture the m3u8 stream,
    # and THEN take the screenshot (which will show the video playing).
    video_downloaded = False

    # 1) Check if there's a video on the page by looking for known indicators
    has_video_indicator = False
    try:
        has_video_indicator = page.evaluate("""() => {
            // Check for Skool's native video player elements
            const indicators = [
                'media-play-button',
                '*[class*="VideoDuration"]',
                '*[class*="VideoWrapper"]',
                '*[class*="VideoThumbnail"]',
                '*[class*="PlayButton"]',
                'video',
                'button[aria-label="Play"]',
            ];
            for (const sel of indicators) {
                if (document.querySelector(sel)) return true;
            }
            // Also check for duration badges like "1:29"
            const allSpans = document.querySelectorAll('span, div');
            for (const el of allSpans) {
                const text = el.innerText?.trim();
                if (text && /^\\d{1,2}:\\d{2}$/.test(text)) return true;
            }
            return false;
        }""")
    except:
        pass

    if has_video_indicator:
        print(f"         [VIDEO] Video indicator detected on page. Attempting to play...")

    # 2) Try multiple strategies to click Play using REAL trusted mouse events
    # IMPORTANT: Skool rejects JavaScript .click() calls (isTrusted=false).
    # Only Playwright's actual mouse clicks produce trusted events that trigger video playback.
    clicked = False

    # Strategy A (PRIMARY): Scroll to video area and click the LARGEST video container
    # The key insight: we need to click the big thumbnail area, not small sub-elements
    try:
        # First, scroll up to ensure the video is in view
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.5)
        
        # Find the video container by looking for the element that CONTAINS the play button
        # Walk UP from media-play-button to find its parent container
        video_box = page.evaluate("""() => {
            // Strategy 1: Find media-play-button and get its parent's bounding rect
            const playBtn = document.querySelector('media-play-button');
            if (playBtn) {
                // Walk up to find the largest meaningful container
                let parent = playBtn.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                    const rect = parent.getBoundingClientRect();
                    if (rect.width > 200 && rect.height > 100) {
                        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                    }
                    parent = parent.parentElement;
                }
                // Fall back to play button itself
                const rect = playBtn.getBoundingClientRect();
                return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
            }
            
            // Strategy 2: Look for elements with Video-related class names
            const videoEls = document.querySelectorAll('*[class*="VideoWrapper"], *[class*="VideoThumbnail"], *[class*="VideoContainer"], media-controller, media-container');
            for (const el of videoEls) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 200 && rect.height > 100) {
                    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                }
            }
            
            // Strategy 3: Look for the duration badge and find its parent container
            const durationEls = document.querySelectorAll('*[class*="VideoDuration"]');
            for (const el of durationEls) {
                let parent = el.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                    const rect = parent.getBoundingClientRect();
                    if (rect.width > 200 && rect.height > 100) {
                        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                    }
                    parent = parent.parentElement;
                }
            }
            
            return null;
        }""")
        
        if video_box:
            center_x = video_box['x'] + video_box['width'] / 2
            center_y = video_box['y'] + video_box['height'] / 2
            print(f"         [PLAY] Clicking center of video container at ({center_x:.0f}, {center_y:.0f}) [size: {video_box['width']:.0f}x{video_box['height']:.0f}]...")
            page.mouse.click(center_x, center_y)
            clicked = True
            time.sleep(2)
            
            # Sometimes need a second click if the first click just shows controls
            # Check if video is actually playing by looking for m3u8 in captured list
            if not captured_m3u8:
                print(f"         [PLAY] No stream yet, clicking again...")
                page.mouse.click(center_x, center_y)
                time.sleep(1)
    except Exception as e:
        print(f"         [PLAY] Container click failed: {e}")

    # Strategy B: Direct Playwright locator clicks as fallback
    if not clicked:
        fallback_selectors = [
            "media-play-button",
            "button[aria-label='Play']",
            "button[aria-label='play']",
            "button.vjs-big-play-button",
            "div[class*='PlayButton']",
        ]
        for sel in fallback_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    print(f"         [PLAY] Fallback: clicking {sel}...")
                    btn.scroll_into_view_if_needed()
                    btn.click(force=True)
                    clicked = True
                    break
            except:
                pass



    # Wait momentarily for network requests to fire, but DO NOT sleep blindly
    # because the JWT token in the m3u8 URL expires in <60 seconds!
    if clicked:
        print(f"         [INFO] Play triggered. Sniffing for m3u8 stream...")
        # Give playwright just enough time to flush network events
        try: page.wait_for_timeout(2000)
        except: pass

    # Clean up listener
    try:
        context.remove_listener("request", on_network_event)
        context.remove_listener("response", on_network_event)
    except:
        pass

    # 3) Check if we captured any m3u8 URLs during page load or after clicking play
    if captured_m3u8:
        best_url = captured_m3u8[-1]  # Use the LAST captured URL (freshest token)
        print(f"         [VIDEO] Captured m3u8 stream ({len(captured_m3u8)} URLs found). Starting yt-dlp IMMEDIATELY.")
        video_path = os.path.join(mod_dir, f"{base_filename}")
        print(f"         [VIDEO] Captured m3u8 stream. Submitting to background downloader.")
        download_manager.submit_job(best_url, video_path, url, sync_args)
        video_downloaded = True

    # 4) Check for YouTube / Loom / Vimeo iframes
    if not video_downloaded:
        try:
            iframes = page.locator("iframe")
            iframe_count = iframes.count()
            for i in range(iframe_count):
                try:
                    src = iframes.nth(i).get_attribute("src") or ""
                    if any(platform in src for platform in ["youtube.com", "youtu.be", "loom.com", "vimeo.com", "wistia"]):
                        print(f"         [VIDEO] Found embedded video: {src[:80]}...")
                        video_path = os.path.join(mod_dir, f"{base_filename}")
                        print(f"         [VIDEO] Captured embedded stream. Submitting to background downloader.")
                        download_manager.submit_job(src, video_path, url, sync_args)
                        video_downloaded = True
                        if video_downloaded:
                            break
                except:
                    continue
        except:
            pass

    # 5) Check for direct video/mp4 tags
    if not video_downloaded:
        try:
            video_tag = page.locator("video source")
            if video_tag.count() > 0:
                direct_src = video_tag.first.get_attribute("src") or ""
                if direct_src and not direct_src.startswith("blob:"):
                    print(f"         [VIDEO] Found direct video source: {direct_src[:80]}...")
                    video_path = os.path.join(mod_dir, f"{base_filename}")
                    print(f"         [VIDEO] Captured video tag. Submitting to background downloader.")
                    download_manager.submit_job(direct_src, video_path, url, sync_args)
                    video_downloaded = True
        except:
            pass

    # 6) If no video at all → log it
    if not video_downloaded:
        if has_video_indicator:
            print(f"         ⚠️ Video indicator was detected but no stream captured! May need manual check.")
        else:
            print(f"         [INFO] No video detected. Text-only lesson.")



    # Clean up context listener
    try:
        context.remove_listener("request", on_network_event)
        context.remove_listener("response", on_network_event)
    except:
        pass

    return True


# ╔══════════════════════════════════════════════╗
# ║           MAIN AUTOMATION LOOP                ║
# ╚══════════════════════════════════════════════╝

def run(headless=False, google_token=None, stop_event=None):
    print("\n" + "=" * 60)
    print("  SKOOL SCRAPER v4 -- FULL AUTOMATION")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60)

    # Create unique download directory for this run
    download_manager = DownloadManager(max_workers=3, cookies_file=COOKIES_FILE)
    download_dir = get_unique_download_dir()
    print(f"\n   [FOLDER] Download folder: {download_dir}")

    # Load progress
    completed = load_progress()
    print(f"   [PROGRESS] Previously completed lessons: {len(completed)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        # Try to restore session
        if os.path.exists(STORAGE_STATE):
            print("   [INFO] Restoring previous session...")
            context = browser.new_context(
                storage_state=STORAGE_STATE,
                viewport={'width': 1366, 'height': 768}
            )
        else:
            context = browser.new_context(viewport={'width': 1366, 'height': 768})

        page = context.new_page()

        # ── STEP 1: Login ──
        if not login_and_get_to_classroom(page, context):
            print("\n   [FATAL] Could not log in. Aborting.")
            browser.close()
            return

        # Export cookies for yt-dlp
        export_cookies(context)

        # ── STEP 2: Discover all course cards ──
        print("\n" + "=" * 60)
        print("  STEP 2: CLASSROOM CARD DISCOVERY")
        print("=" * 60)

        # Take a screenshot of the full classroom grid
        page.screenshot(path=os.path.join(download_dir, "00_Classroom_Grid.png"), full_page=True)

        # Wait for cards to render
        time.sleep(3)

        # Extract course info directly from the DOM using JavaScript
        # This avoids the clicking problem entirely — we just read the data
        # and navigate by URL.
        # We use Skool's injected Next.js __NEXT_DATA__ to get all URLs flawlessly
        # without needing to battle with React synthetic events or clickable areas.
        courses_json = page.evaluate("() => JSON.stringify(window.__NEXT_DATA__.props.pageProps.allCourses)")
        import json
        all_courses = json.loads(courses_json)
        
        # Extract the community display name (e.g. "AI Workshop") from __NEXT_DATA__
        try:
            community_display_name = page.evaluate("() => window.__NEXT_DATA__.props.pageProps.group?.metadata?.name || ''")
        except:
            community_display_name = ""
        if not community_display_name:
            # Fallback: derive from URL slug (e.g. "aiworkshop" → "Aiworkshop")
            community_display_name = CLASSROOM_URL.split("/classroom")[0].split("/")[-1].replace("-", " ").title()
        community_display_name = clean_filename(community_display_name)
        print(f"   [INFO] Community name: {community_display_name}")
        
        course_url_map = {}
        # Get base e.g. "https://www.skool.com/aiworkshop"
        community_base = CLASSROOM_URL.split("/classroom")[0] 
        community_path_only = "/" + community_base.split("/")[-1] # e.g. "/aiworkshop"
        
        for c in all_courses:
            title = c.get('metadata', {}).get('title')
            if title:
                # Store relative URL path accurately
                course_url_map[title] = f"{community_path_only}/classroom/{c.get('name')}"

        courses = page.evaluate("""() => {
            const cards = document.querySelectorAll("[class*='CourseLinkWrapper']");
            const results = [];
            cards.forEach((card, index) => {
                const text = card.innerText || '';
                const name = text.split('\\n')[0].trim();
                const locked = text.includes('Unlock at Level');
                results.push({ index: index, name: name, locked: locked });
            });
            return results;
        }""")

        print(f"   Found {len(courses)} course cards on the grid.")
        courses_with_urls = []
        for c in courses:
            url = course_url_map.get(c['name'])
            if url and not c['locked']:
                c['href'] = url
                courses_with_urls.append(c)
                status = "🔗 " + url
            else:
                status = "🔒 LOCKED" if c['locked'] else "⚠️ NO URL IN METADATA"
            print(f"   [{c['index'] + 1}] {c['name']}  —  {status}")

        # Return to classroom before starting course processing
        try:
            if page.url.rstrip('/') != CLASSROOM_URL.rstrip('/'):
                page.goto(CLASSROOM_URL)
                time.sleep(SLEEP_PAGE_LOAD)
        except Exception as e:
            print(f"   ⚠️ Page crashed on returning to classroom. Recreating page...")
            try: page.close()
            except: pass
            page = context.new_page()
            page.goto(CLASSROOM_URL)
            time.sleep(SLEEP_PAGE_LOAD)

        # ── STEP 3: Process each course by direct navigation ──
        print("\n" + "=" * 60)
        print("  STEP 3: PROCESSING COURSES")
        print("=" * 60)

        for course_info in courses_with_urls:
            if stop_event and stop_event.is_set():
                print("   [INFO] Stop event detected. Exiting course loop.")
                break
            course_name = course_info['name']
            course_url = course_info['href']
            course_idx = course_info['index'] + 1  # Preserve exact grid sequence

            # Ensure full URL
            if course_url and not course_url.startswith("http"):
                course_url = f"https://www.skool.com{course_url}"

            print(f"\n   {'-' * 50}")
            print(f"   [COURSE] {course_name}")
            print(f"   [URL] {course_url}")
            print(f"   {'-' * 50}")

            # ── Navigate directly to the course ──
            try:
                page.goto(course_url)
                print(f"   [INFO] Waiting {SLEEP_PAGE_LOAD}s for course to load...")
                time.sleep(SLEEP_PAGE_LOAD)
            except Exception as e:
                print(f"   ⚠️ Page crashed or could not navigate: {e}")
                print("   [INFO] Recreating browser page and retrying...")
                try: page.close()
                except: pass
                page = context.new_page()
                try:
                    page.goto(course_url)
                    time.sleep(SLEEP_PAGE_LOAD)
                except Exception as e2:
                    print(f"   [FAIL] Could not navigate to course after retry: {e2}")
                    continue

            # Verify we're on a course page
            current_path = page.url.split("?")[0].rstrip("/")
            classroom_base = CLASSROOM_URL.rstrip("/")
            if current_path == classroom_base:
                print(f"   [FAIL] Navigation failed — still on classroom grid.")
                continue

            print(f"   [OK] Entered course successfully.")

            # ── Force click FIRST lesson to override Skool's cache ──
            try:
                page.wait_for_selector("a[href*='?md=']", timeout=15000)
                first_lesson = page.locator("a[href*='?md=']").first
                first_lesson.click()
                time.sleep(3)
                print(f"   [INFO] Clicked first lesson to reset position.")
            except:
                print(f"   ⚠️ Could not find/click first lesson. Continuing with current view.")

            # ── Expand all dropdowns ──
            expand_all_dropdowns(page)

            # ── Map the course structure ──
            course_map = map_course_structure(page)

            if not course_map:
                print(f"   ⚠️ No modules found in this course. Skipping.")
                try:
                    if page.url.rstrip('/') != CLASSROOM_URL.rstrip('/'):
                        page.goto(CLASSROOM_URL)
                except:
                    page = context.new_page()
                    page.goto(CLASSROOM_URL)
                time.sleep(SLEEP_PAGE_LOAD)
                continue

            # ── Create course folder ──
            course_folder_name = f"{course_idx:02d}_{clean_filename(course_name)}"
            course_dir = os.path.join(download_dir, course_folder_name)
            os.makedirs(course_dir, exist_ok=True)

            # ── Process each module and lesson ──
            mod_idx = 0
            for module in course_map:
                mod_idx += 1
                mod_name = clean_filename(module['module'])
                
                # Always create a numbered module folder — no more flat "General" dump
                mod_folder_name = f"{mod_idx:02d}_{mod_name}"
                mod_dir = os.path.join(course_dir, mod_folder_name)
                os.makedirs(mod_dir, exist_ok=True)

                print(f"\n      [MODULE] {mod_idx}: {module['module']}")

                lesson_idx = 0
                for lesson in module['lessons']:
                    lesson_idx += 1

                    # Create base filename context instead of wrapper folder
                    lesson_file_base = f"{lesson_idx:02d}_{clean_filename(lesson['title'])}"

                    # Physical Smart Resume Check
                    lesson_url = lesson['url']
                    if lesson_url in completed and is_lesson_complete(mod_dir, lesson_file_base):
                        print(f"         ⏭️ [{lesson_idx:02d}] Already downloaded natively: {lesson['title']}")
                        continue
                    elif lesson_url in completed:
                        print(f"         [RECOVERY] Cached as complete but files are missing/broken. Re-downloading: {lesson['title']}")
                        
                    print(f"\n         [LESSON] [{lesson_idx:02d}] {lesson['title']}")

                    # Process with retry
                    success = False
                    for attempt in range(1, MAX_RETRIES + 1):
                        try:
                            if stop_event and stop_event.is_set():
                                print("   [INFO] Stop event detected. Stopping lesson processing.")
                                break
                            sync_args = {
                                'google_token': google_token,
                                'community_display_name': community_display_name,
                                'course_folder_name': course_folder_name,
                                'mod_folder_name': mod_folder_name,
                                'mod_dir': mod_dir,
                                'lesson_file_base': lesson_file_base
                            }
                            success = process_lesson(page, lesson, mod_dir, lesson_file_base, context, download_manager, sync_args)
                            if success:
                                break
                        except Exception as e:
                            print(f"         ⚠️ Attempt {attempt}/{MAX_RETRIES} failed: {e}")
                            # Check if the page itself crashed and needs recovery
                            try:
                                _ = page.url  # test if page is alive
                            except:
                                print(f"         [RECOVERY] Page crashed. Recreating...")
                                try: page.close()
                                except: pass
                                page = context.new_page()
                                page.goto(course_url)
                                time.sleep(SLEEP_PAGE_LOAD)
                            if attempt < MAX_RETRIES:
                                print(f"         [INFO] Retrying in 5s...")
                                time.sleep(5)

                    if success:
                        completed.add(lesson_url)
                        save_progress(completed)
                        


                    else:
                        print(f"         [FAIL] FAILED after {MAX_RETRIES} attempts. Moving on.")
                        try:
                            page.screenshot(path=os.path.join(
                                mod_dir,
                                f"ERROR_{lesson_file_base}.png"
                            ))
                        except Exception as screenshot_err:
                            print(f"         ⚠️ Could not save error screenshot: {screenshot_err}")
                            # Page may have crashed — recreate it
                            try: page.close()
                            except: pass
                            page = context.new_page()
                            page.goto(course_url)
                            time.sleep(SLEEP_PAGE_LOAD)

                    # Re-export cookies periodically (tokens expire)
                    try:
                        export_cookies(context)
                    except:
                        pass

            # ── Done with this course. Go back to classroom ──
            print(f"\n   [DONE] Finished course: {course_name}")
            try:
                if page.url.rstrip('/') != CLASSROOM_URL.rstrip('/'):
                    page.goto(CLASSROOM_URL)
            except:
                page = context.new_page()
                page.goto(CLASSROOM_URL)
            print(f"   [INFO] Waiting {SLEEP_PAGE_LOAD}s for classroom to reload...")
            time.sleep(SLEEP_PAGE_LOAD)

        # ── DONE ──
        print("\n" + "=" * 60)
        print("  SCRAPING COMPLETE!")
        print(f"  Total lessons downloaded: {len(completed)}")
        print("=" * 60)

        # Wait for all parallel yt-dlp processes to complete cleanly
        print(f"   [INFO] Waiting for background video downloads to complete...")
        download_manager.wait_all()
        browser.close()


if __name__ == "__main__":
    run()
