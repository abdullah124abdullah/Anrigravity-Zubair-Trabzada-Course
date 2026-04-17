import os
import sys
import json
import threading
import re
import shutil
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, session

# Add parent dir to path so we can import scraper
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, save_user, get_user, get_user_by_email, save_google_token, create_job, update_job, get_job, get_latest_job
from app import gdrive

app = Flask(__name__)
app.secret_key = 'skool-scraper-secret-key-change-in-prod'

# Global state for the running job (simple single-user local version)
job_status = {
    'running': False,
    'progress': 0,
    'current_course': '',
    'current_lesson': '',
    'total': 0,
    'completed': 0,
    'logs': [],
    'error': None
}

# Global event to signal stopping to the scraper thread
stop_event = threading.Event()

def add_log(msg):
    """Add a log message to the global status."""
    job_status['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    # Keep only last 100 logs
    if len(job_status['logs']) > 100:
        job_status['logs'] = job_status['logs'][-100:]

# Initialize database on import
init_db()


@app.route('/')
def index():
    """Main dashboard page."""
    user_id = session.get('user_id')
    user = get_user(user_id) if user_id else None
    return render_template('index.html', user=user, job=job_status)


@app.route('/save-credentials', methods=['POST'])
def save_credentials():
    """Save Skool credentials and classroom URL."""
    skool_email = request.form.get('skool_email', '').strip()
    skool_password = request.form.get('skool_password', '').strip()
    classroom_url = request.form.get('classroom_url', '').strip()
    
    if not all([skool_email, skool_password, classroom_url]):
        return jsonify({'success': False, 'error': 'All fields are required'}), 400
    
    # Clean and normalize classroom URL regardless of what the user pastes in
    # Example inputs: 'skool.com/aiworkshop', 'https://skool.com/aiworkshop/classroom', 'aiworkshop'
    clean_input = classroom_url.lower().strip()
    
    # Try to extract from a URL format
    match = re.search(r'skool\.com/([^/]+)', clean_input)
    if match:
        community_name = match.group(1)
        # Handle the edge case where the database previously mangled it to skool.com/skool.com/
        if community_name == 'skool.com':
            match2 = re.search(r'skool\.com/skool\.com/([^/]+)', clean_input)
            if match2:
                community_name = match2.group(1)
            else:
                community_name = clean_input.replace('skool.com', '').replace('/', '')
    else:
        # Assume they just pasted the community name directly
        community_name = clean_input.replace('/', '').replace('https:', '').replace('http:', '')
        
    if not community_name:
        return jsonify({'success': False, 'error': 'Invalid Classroom URL format.'}), 400
        
    classroom_url = f'https://www.skool.com/{community_name}/classroom'
    
    user_id = save_user(skool_email, skool_password, classroom_url)
    session['user_id'] = user_id
    
    return jsonify({'success': True, 'user_id': user_id})


@app.route('/auth/google')
def auth_google():
    """Redirect to Google OAuth."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    
    redirect_uri = request.host_url.rstrip('/') + '/oauth2callback'
    auth_url, state, code_verifier = gdrive.get_auth_url(redirect_uri)
    session['oauth_state'] = state
    session['code_verifier'] = code_verifier
    return redirect(auth_url)


@app.route('/oauth2callback')
def oauth2callback():
    """Handle Google OAuth callback."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    
    code = request.args.get('code')
    if not code:
        return redirect(url_for('index'))
    
    try:
        redirect_uri = request.host_url.rstrip('/') + '/oauth2callback'
        code_verifier = session.get('code_verifier')
        token_json, email = gdrive.exchange_code(code, code_verifier=code_verifier, redirect_uri=redirect_uri)
        save_google_token(user_id, token_json, email)
        return redirect(url_for('index'))
    except Exception as e:
        return f"OAuth Error: {e}", 500


@app.route('/disconnect-google', methods=['POST'])
def disconnect_google():
    """Remove Google Drive connection."""
    user_id = session.get('user_id')
    if user_id:
        save_google_token(user_id, None, None)
    return jsonify({'success': True})


@app.route('/start', methods=['POST'])
def start_scraper():
    """Start the scraper in a background thread."""
    if job_status['running']:
        return jsonify({'success': False, 'error': 'A job is already running'}), 400
    
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Please save your credentials first'}), 400
    
    user = get_user(user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    
    # Reset status
    job_status['running'] = True
    job_status['progress'] = 0
    job_status['current_course'] = ''
    job_status['current_lesson'] = ''
    job_status['total'] = 0
    job_status['completed'] = 0
    job_status['logs'] = []
    job_status['error'] = None
    stop_event.clear()  # Clear any previous stop signal
    
    # Start scraper in background thread
    show_browser = True if request.args.get('show_browser') == 'true' else False
    thread = threading.Thread(target=run_scraper_job, args=(user, show_browser), daemon=True)
    thread.start()
    
    return jsonify({'success': True})


def run_scraper_job(user, show_browser):
    """Run the scraper in a background thread."""
    try:
        add_log(f"Starting scraper for {user['skool_email']}")
        add_log(f"Classroom: {user['classroom_url']}")
        
        # Import and configure the scraper (v5 with parallel downloads)
        import skool_scraper_v5 as scraper
        
        # Override scraper config with user's settings
        scraper.EMAIL = user['skool_email']
        scraper.PASSWORD = user['skool_password']
        scraper.CLASSROOM_URL = user['classroom_url']
        
        # Hook into scraper's print to capture logs
        original_print = print
        def hooked_print(*args, **kwargs):
            msg = ' '.join(str(a) for a in args)
            add_log(msg)
            
            # Parse progress from scraper output
            if '[COURSE]' in msg:
                course_name = msg.split('[COURSE]')[-1].strip()
                job_status['current_course'] = course_name
            elif '[LESSON]' in msg:
                lesson_name = msg.split(']')[-1].strip() if ']' in msg else msg
                job_status['current_lesson'] = lesson_name
                job_status['completed'] += 1
                if job_status['total'] > 0:
                    job_status['progress'] = int((job_status['completed'] / job_status['total']) * 100)
            elif 'Extracted' in msg and 'lessons' in msg:
                try:
                    # Parse "Extracted X modules, Y lessons"
                    parts = msg.split('Extracted')[1]
                    lesson_count = int(parts.split('modules,')[1].split('lessons')[0].strip())
                    job_status['total'] += lesson_count
                except:
                    pass
            
            original_print(*args, **kwargs)
        
        import builtins
        builtins.print = hooked_print
        
        # Run the scraper
        scraper.run(headless=not show_browser, google_token=user.get('google_token'), stop_event=stop_event)
        
        builtins.print = original_print
        
        add_log("✅ Scraping complete!")
        job_status['progress'] = 100
        
    except Exception as e:
        add_log(f"❌ Error: {str(e)}")
        job_status['error'] = str(e)
    finally:
        job_status['running'] = False


@app.route('/progress')
def get_progress():
    """Return current job progress as JSON."""
    return jsonify(job_status)


@app.route('/stop', methods=['POST'])
def stop_scraper():
    """Signal the scraper to stop (best-effort)."""
    job_status['running'] = False
    stop_event.set()
    add_log("⏹️ Stop requested by user.")
    return jsonify({'success': True})

@app.route('/reset', methods=['POST'])
def reset_progress():
    """Manually clear progress and downloads cache."""
    try:
        import shutil
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        progress_file = os.path.join(base_dir, "scraper_progress_v4.json")
        if os.path.exists(progress_file):
            try: os.remove(progress_file)
            except: pass
                
        downloads_dir = os.path.join(base_dir, "Course_Downloads")
        if os.path.exists(downloads_dir):
            try: shutil.rmtree(downloads_dir, ignore_errors=True)
            except: pass
            
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
