import os
import json
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Path to the Google OAuth secrets file
SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'google_secrets.json')

SCOPES = ['https://www.googleapis.com/auth/drive.file']

def get_flow(redirect_uri='http://localhost:5000/oauth2callback'):
    """Create a Google OAuth2 flow."""
    secrets = json.load(open(SECRETS_FILE))
    
    client_config = {
        "web": {
            "client_id": secrets["client_id"],
            "client_secret": secrets["client_secret"],
            "auth_uri": secrets["auth_uri"],
            "token_uri": secrets["token_uri"],
            "redirect_uris": secrets["redirect_uris"]
        }
    }
    
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    return flow

def get_auth_url(redirect_uri='http://localhost:5000/oauth2callback'):
    """Get the Google OAuth authorization URL."""
    flow = get_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    code_verifier = getattr(flow, 'code_verifier', None)
    return auth_url, state, code_verifier

def exchange_code(code, state=None, code_verifier=None, redirect_uri='http://localhost:5000/oauth2callback'):
    """Exchange the authorization code for credentials."""
    flow = get_flow(redirect_uri)
    if code_verifier:
        flow.code_verifier = code_verifier
    
    flow.fetch_token(code=code)
    credentials = flow.credentials
    
    # Get the user's email
    service = build('oauth2', 'v2', credentials=credentials)
    try:
        user_info = service.userinfo().get().execute()
        email = user_info.get('email', 'Unknown')
    except:
        email = 'Connected'
    
    token_data = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': list(credentials.scopes)
    }
    
    return json.dumps(token_data), email

def get_drive_service(token_json):
    """Build a Google Drive service from saved token JSON."""
    token_data = json.loads(token_json)
    credentials = Credentials(
        token=token_data['token'],
        refresh_token=token_data.get('refresh_token'),
        token_uri=token_data['token_uri'],
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=token_data.get('scopes', SCOPES)
    )
    return build('drive', 'v3', credentials=credentials)

def create_drive_folder(token_json, folder_name):
    """Create a folder on Google Drive. Returns the folder ID."""
    service = get_drive_service(token_json)
    
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

def upload_file_to_drive(token_json, file_path, parent_folder_id=None):
    """Upload a single file to Google Drive."""
    service = get_drive_service(token_json)
    
    file_name = os.path.basename(file_path)
    file_metadata = {'name': file_name}
    if parent_folder_id:
        file_metadata['parents'] = [parent_folder_id]
    
    media = MediaFileUpload(file_path, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

def upload_folder_to_drive(token_json, local_folder, drive_folder_name=None):
    """Upload an entire folder structure to Google Drive."""
    if not drive_folder_name:
        drive_folder_name = os.path.basename(local_folder)
    
    # Create root folder
    root_id = create_drive_folder(token_json, drive_folder_name)
    
    for root, dirs, files in os.walk(local_folder):
        # Calculate relative path from local_folder
        rel_path = os.path.relpath(root, local_folder)
        
        if rel_path == '.':
            current_parent = root_id
        else:
            # Create nested folders
            parts = rel_path.split(os.sep)
            current_parent = root_id
            for part in parts:
                current_parent = create_drive_folder(token_json, part)
        
        # Upload files in this directory
        for file_name in files:
            file_path = os.path.join(root, file_name)
            try:
                upload_file_to_drive(token_json, file_path, current_parent)
            except Exception as e:
                print(f"Failed to upload {file_path}: {e}")
    return root_id


# ── INLINE SYNC OPTIMIZED FOR VPS STORAGE ──
drive_folder_cache = {}

def get_or_create_drive_folder(service, parent_id, folder_name, token_json=""):
    """Finds a folder by name in a parent, or creates it if it doesn't exist."""
    # Use token_json hash to sandbox the memory cache per Google Profile!
    cache_key = f"{hash(token_json)}_{parent_id}_{folder_name}"
    if cache_key in drive_folder_cache:
        try:
            # Quick check if it really exists (handles case where user manually deleted folder mid-run)
            service.files().get(fileId=drive_folder_cache[cache_key], fields="id").execute()
            return drive_folder_cache[cache_key]
        except:
            # If 404 Error, drop it from memory and create a new one!
            del drive_folder_cache[cache_key]
            
    query = f"'{parent_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = results.get('files', [])
    
    if files:
        folder_id = files[0]['id']
    else:
        file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
        if parent_id != 'root':
            file_metadata['parents'] = [parent_id]
        folder = service.files().create(body=file_metadata, fields='id').execute()
        folder_id = folder.get('id')
        
    drive_folder_cache[cache_key] = folder_id
    return folder_id

def sync_lesson_files_to_drive(token_json, community_name, course_name, module_name, local_file_paths):
    """Uploads files to Drive in a 3-level hierarchy: Community → Course → Module.
    
    Example: AI Workshop / 01_Start Here / 01_Welcome / 01_Welcome.png
    """
    service = get_drive_service(token_json)
    
    # 1. Get/Create ONE Root Folder (named after the community)
    root_id = get_or_create_drive_folder(service, 'root', community_name, token_json)
    
    # 2. Get/Create Course Folder (numbered, e.g. "01_Start Here")
    course_id = get_or_create_drive_folder(service, root_id, course_name, token_json)
    
    # 3. Get/Create Module Folder (numbered, e.g. "01_Welcome")
    mod_id = get_or_create_drive_folder(service, course_id, module_name, token_json)
    
    # 4. Upload Files
    for file_path in local_file_paths:
        if os.path.exists(file_path):
            file_name = os.path.basename(file_path)
            
            # Check if file already exists so we don't duplicate on resume
            query = f"'{mod_id}' in parents and name='{file_name}' and trashed=false"
            results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
            if not results.get('files'):
                media = MediaFileUpload(file_path, resumable=True)
                service.files().create(body={'name': file_name, 'parents': [mod_id]}, media_body=media, fields='id').execute()
            
            # Delete heavy video files locally to prevent VPS disk explosion
            if file_path.endswith('.mp4') or file_path.endswith('.mkv') or file_path.endswith('.webm'):
                try:
                    os.remove(file_path)
                except:
                    pass
