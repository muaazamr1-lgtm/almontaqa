import os, json, sqlite3, hashlib, hmac, secrets, time, re, urllib.request, urllib.parse, urllib.error
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse
from yt_dlp import YoutubeDL
import stats as stats_engine

DB_PATH = os.environ.get('DB_PATH', '/data/almontaqa.sqlite3')
APP_SECRET = os.environ.get('APP_SECRET', '')
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
SYNC_MIN_INTERVAL = 600  # seconds; silent background sync is throttled per user (10 min)
if len(APP_SECRET) < 32: raise RuntimeError('APP_SECRET must be configured with at least 32 characters')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL, hide_thumbnails INTEGER NOT NULL DEFAULT 0, watch_later_enabled INTEGER NOT NULL DEFAULT 0, usage_limits_enabled INTEGER NOT NULL DEFAULT 0, username TEXT, account_type TEXT NOT NULL DEFAULT 'user', parent_user_id INTEGER, parent_mode TEXT NOT NULL DEFAULT 'user');
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, active_child_id INTEGER, expires_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS child_link_codes (code TEXT PRIMARY KEY, parent_user_id INTEGER NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER, FOREIGN KEY(parent_user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT NOT NULL, created_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_categories (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, daily_limit_minutes INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, UNIQUE(user_id,name), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_category_orders (user_id INTEGER PRIMARY KEY, category_ids TEXT NOT NULL, updated_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_collection_category_orders (user_id INTEGER PRIMARY KEY, category_ids TEXT NOT NULL, updated_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS watch_sessions (user_id INTEGER NOT NULL, session_id TEXT NOT NULL, video_id TEXT NOT NULL, category_id INTEGER, seconds INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(user_id,session_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS watch_time_daily (user_id INTEGER NOT NULL, session_id TEXT NOT NULL, video_id TEXT NOT NULL, category_id INTEGER, watch_date TEXT NOT NULL, seconds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,session_id,watch_date), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_sync_state (user_id INTEGER PRIMARY KEY, last_sync_at INTEGER NOT NULL DEFAULT 0, last_sync_added INTEGER NOT NULL DEFAULT 0, last_sync_errors TEXT NOT NULL DEFAULT '[]', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_channels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_channel_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_video_id TEXT NOT NULL DEFAULT '', last_video_published_at TEXT NOT NULL DEFAULT '', ignore_shorts INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,youtube_channel_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlists (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_playlist_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_item_count INTEGER NOT NULL DEFAULT 0, last_video_published_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,youtube_playlist_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlist_seen (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, seen_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(playlist_id) REFERENCES user_playlists(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_collection_categories (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(user_id,name), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_collection_playlists (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_playlist_id TEXT NOT NULL, title TEXT NOT NULL, channel_title TEXT NOT NULL DEFAULT 'YouTube', thumbnail_url TEXT, category_id INTEGER NOT NULL, added_at INTEGER NOT NULL, video_count INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,youtube_playlist_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES user_collection_categories(id));
CREATE TABLE IF NOT EXISTS user_collection_videos (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, position INTEGER NOT NULL, title TEXT NOT NULL, channel_title TEXT NOT NULL, published_at TEXT NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, duration_seconds INTEGER, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(playlist_id) REFERENCES user_collection_playlists(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_collection_history (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, playlist_title TEXT NOT NULL, category_id INTEGER, category_name TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL, title TEXT NOT NULL, channel_title TEXT NOT NULL, published_at TEXT NOT NULL, watched_at INTEGER NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, duration_seconds INTEGER, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES user_collection_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS user_collection_completed (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, category_id INTEGER, category_name TEXT NOT NULL DEFAULT '', completed_at INTEGER NOT NULL, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES user_collection_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS user_video_sources (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id,source_type,source_id), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_videos (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER NOT NULL, published_at TEXT NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, duration_seconds INTEGER, watch_later INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_watch_history (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER, category_name TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL, watched_at INTEGER NOT NULL, thumbnail_url TEXT, video_url TEXT, duration_seconds INTEGER, completed INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES user_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS user_completed_videos (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER, category_name TEXT NOT NULL DEFAULT '', completed_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES user_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS user_video_decisions (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('record','discard')), decided_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS video_identity (video_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', channel_title TEXT NOT NULL DEFAULT '', fetched_at INTEGER NOT NULL DEFAULT 0);
'''
_initialized_databases = set()
_database_init_lock = threading.Lock()

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con=sqlite3.connect(DB_PATH,timeout=20); con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        if DB_PATH not in _initialized_databases:
            with _database_init_lock:
                if DB_PATH not in _initialized_databases:
                    initialize_database(con)
                    _initialized_databases.add(DB_PATH)
        return con
    except Exception:
        con.close()
        raise

def initialize_database(con):
    # Migrations run once per database per process, never on ordinary reads.
    con.execute('PRAGMA journal_mode=WAL'); con.executescript(SCHEMA)
    con.execute('BEGIN IMMEDIATE')
    decision_schema=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_video_decisions'").fetchone()
    if decision_schema and 'count_no_history' in decision_schema['sql']:
        con.execute("CREATE TABLE user_video_decisions_new (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('record','discard')), decided_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)")
        con.execute("INSERT INTO user_video_decisions_new(user_id,video_id,action,decided_at) SELECT user_id,video_id,CASE WHEN action='count_no_history' THEN 'discard' ELSE action END,decided_at FROM user_video_decisions")
        con.execute('DROP TABLE user_video_decisions')
        con.execute('ALTER TABLE user_video_decisions_new RENAME TO user_video_decisions')
    # Remove retired integration credentials and obsolete player preference data.
    con.execute('DROP TABLE IF EXISTS user_preferences')
    for table in ('notion_oauth_states','user_integrations','notion_archive_queue'):
        con.execute('DROP TABLE IF EXISTS '+table)
    for table,columns in (('user_categories',('notion_database_id','notion_data_source_id')),('user_videos',('notion_page_id',)),('categories',('notion_database_id','notion_data_source_id')),('videos',('notion_page_id',))):
        existing={row['name'] for row in con.execute('PRAGMA table_info('+table+')')}
        for column in columns:
            if column in existing:con.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        con.execute("DELETE FROM settings WHERE key='integrations'")
    video_cols={r['name'] for r in con.execute('PRAGMA table_info(user_videos)')}
    if 'watched' not in video_cols: con.execute('ALTER TABLE user_videos ADD COLUMN watched INTEGER NOT NULL DEFAULT 0')
    if 'duration_seconds' not in video_cols: con.execute('ALTER TABLE user_videos ADD COLUMN duration_seconds INTEGER')
    if 'watch_later' not in video_cols: con.execute('ALTER TABLE user_videos ADD COLUMN watch_later INTEGER NOT NULL DEFAULT 0')
    user_cols={r['name'] for r in con.execute('PRAGMA table_info(users)')}
    if 'hide_thumbnails' not in user_cols: con.execute('ALTER TABLE users ADD COLUMN hide_thumbnails INTEGER NOT NULL DEFAULT 0')
    if 'watch_later_enabled' not in user_cols: con.execute('ALTER TABLE users ADD COLUMN watch_later_enabled INTEGER NOT NULL DEFAULT 0')
    if 'usage_limits_enabled' not in user_cols: con.execute('ALTER TABLE users ADD COLUMN usage_limits_enabled INTEGER NOT NULL DEFAULT 0')
    if 'username' not in user_cols: con.execute('ALTER TABLE users ADD COLUMN username TEXT')
    if 'account_type' not in user_cols: con.execute("ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'user'")
    if 'parent_user_id' not in user_cols: con.execute('ALTER TABLE users ADD COLUMN parent_user_id INTEGER')
    if 'parent_mode' not in user_cols: con.execute("ALTER TABLE users ADD COLUMN parent_mode TEXT NOT NULL DEFAULT 'user'")
    session_cols={r['name'] for r in con.execute('PRAGMA table_info(sessions)')}
    if 'active_child_id' not in session_cols: con.execute('ALTER TABLE sessions ADD COLUMN active_child_id INTEGER')
    # Give existing accounts a friendly, unique username so they can sign in by either email or username.
    used_usernames={str(row['username']).lower() for row in con.execute("SELECT username FROM users WHERE username IS NOT NULL AND TRIM(username)<>''")}
    for row in con.execute("SELECT id,email FROM users WHERE username IS NULL OR TRIM(username)=''").fetchall():
        base=re.sub(r'[^\w.-]','',str(row['email'] or '').split('@')[0],flags=re.UNICODE).strip('._-')[:28]
        if len(base)<3:base='user'+str(row['id'])
        candidate=base[:32];suffix=0
        while candidate.lower() in used_usernames:
            suffix+=1;tail=str(row['id']) if suffix==1 else str(row['id'])+str(suffix)
            candidate=(base[:max(1,32-len(tail))]+tail)[:32]
        con.execute('UPDATE users SET username=? WHERE id=?',(candidate,row['id']))
        used_usernames.add(candidate.lower())
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique ON users(username COLLATE NOCASE)')
    con.execute('CREATE INDEX IF NOT EXISTS users_parent_link ON users(parent_user_id)')
    category_cols={r['name'] for r in con.execute('PRAGMA table_info(user_categories)')}
    if 'daily_limit_minutes' not in category_cols: con.execute('ALTER TABLE user_categories ADD COLUMN daily_limit_minutes INTEGER NOT NULL DEFAULT 0')
    for table in ('watch_sessions','watch_time_daily'):
        columns={row['name'] for row in con.execute('PRAGMA table_info('+table+')')}
        if 'library_type' not in columns:con.execute("ALTER TABLE "+table+" ADD COLUMN library_type TEXT NOT NULL DEFAULT 'video'")
        if 'collection_category_id' not in columns:con.execute('ALTER TABLE '+table+' ADD COLUMN collection_category_id INTEGER')
        if 'collection_playlist_id' not in columns:con.execute('ALTER TABLE '+table+' ADD COLUMN collection_playlist_id INTEGER')
        if 'channel_id' not in columns:con.execute("ALTER TABLE "+table+" ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''")
        if 'channel_title' not in columns:con.execute("ALTER TABLE "+table+" ADD COLUMN channel_title TEXT NOT NULL DEFAULT ''")
        if 'video_title' not in columns:con.execute("ALTER TABLE "+table+" ADD COLUMN video_title TEXT NOT NULL DEFAULT ''")
    history_cols={r['name'] for r in con.execute('PRAGMA table_info(user_watch_history)')}
    for column,sql_type in (('thumbnail_url','TEXT'),('video_url','TEXT'),('duration_seconds','INTEGER'),('completed','INTEGER NOT NULL DEFAULT 1')):
        if column not in history_cols:con.execute('ALTER TABLE user_watch_history ADD COLUMN '+column+' '+sql_type)
    con.execute('DROP TRIGGER IF EXISTS preserve_watched_video_history')
    con.execute('''CREATE TRIGGER preserve_watched_video_history BEFORE DELETE ON user_videos WHEN OLD.watched=1 BEGIN
        INSERT INTO user_completed_videos(user_id,video_id,title,channel_title,category_id,category_name,completed_at)
        VALUES(OLD.user_id,OLD.video_id,OLD.title,OLD.channel_title,OLD.category_id,COALESCE((SELECT name FROM user_categories WHERE id=OLD.category_id),''),strftime('%s','now'))
        ON CONFLICT(user_id,video_id) DO UPDATE SET completed_at=excluded.completed_at;
    END''')
    con.execute("DELETE FROM user_watch_history WHERE NOT EXISTS(SELECT 1 FROM user_video_decisions d WHERE d.user_id=user_watch_history.user_id AND d.video_id=user_watch_history.video_id AND d.action='record')")
    con.execute('INSERT OR IGNORE INTO user_completed_videos(user_id,video_id,title,channel_title,category_id,category_name,completed_at) SELECT h.user_id,h.video_id,h.title,h.channel_title,h.category_id,h.category_name,h.watched_at FROM user_watch_history h WHERE h.completed=1')
    con.execute('INSERT OR IGNORE INTO user_completed_videos(user_id,video_id,title,channel_title,category_id,category_name,completed_at) SELECT v.user_id,v.video_id,v.title,v.channel_title,v.category_id,COALESCE(c.name,\'\'),v.created_at FROM user_videos v LEFT JOIN user_categories c ON c.id=v.category_id WHERE v.watched=1')
    # Preserve legacy totals once; later restarts must not reassign a session's full total to a new day.
    if not con.execute("SELECT 1 FROM app_migrations WHERE name='watch_time_daily_backfill_v1'").fetchone():
        con.execute("INSERT OR IGNORE INTO watch_time_daily(user_id,session_id,video_id,category_id,watch_date,seconds) SELECT user_id,session_id,video_id,category_id,date(updated_at,'unixepoch'),seconds FROM watch_sessions WHERE seconds>0")
        con.execute("INSERT INTO app_migrations(name,applied_at) VALUES('watch_time_daily_backfill_v1',?)",(int(time.time()),))
    channel_cols={r['name'] for r in con.execute('PRAGMA table_info(user_channels)')}
    if 'last_video_id' not in channel_cols: con.execute("ALTER TABLE user_channels ADD COLUMN last_video_id TEXT NOT NULL DEFAULT ''")
    if 'last_video_published_at' not in channel_cols: con.execute("ALTER TABLE user_channels ADD COLUMN last_video_published_at TEXT NOT NULL DEFAULT ''")
    if 'ignore_shorts' not in channel_cols: con.execute("ALTER TABLE user_channels ADD COLUMN ignore_shorts INTEGER NOT NULL DEFAULT 0")
    playlist_cols={r['name'] for r in con.execute('PRAGMA table_info(user_playlists)')}
    if 'last_item_count' not in playlist_cols: con.execute('ALTER TABLE user_playlists ADD COLUMN last_item_count INTEGER NOT NULL DEFAULT 0')
    if 'last_video_published_at' not in playlist_cols: con.execute("ALTER TABLE user_playlists ADD COLUMN last_video_published_at TEXT NOT NULL DEFAULT ''")
    seen_cols={r['name'] for r in con.execute('PRAGMA table_info(user_playlist_seen)')}
    if 'seen_at' not in seen_cols: con.execute('ALTER TABLE user_playlist_seen ADD COLUMN seen_at INTEGER NOT NULL DEFAULT 0')
    con.execute('UPDATE user_playlist_seen SET seen_at=COALESCE((SELECT created_at FROM user_playlists WHERE id=user_playlist_seen.playlist_id),0) WHERE seen_at=0')
    con.execute('CREATE INDEX IF NOT EXISTS user_videos_feed ON user_videos(user_id,published_at DESC)')
    con.execute('CREATE INDEX IF NOT EXISTS watch_sessions_category ON watch_sessions(user_id,category_id)')
    con.execute('CREATE INDEX IF NOT EXISTS watch_time_daily_period ON watch_time_daily(user_id,watch_date,category_id)')
    con.execute('CREATE INDEX IF NOT EXISTS user_watch_history_category ON user_watch_history(user_id,category_id)')
    con.execute('CREATE INDEX IF NOT EXISTS user_completed_category ON user_completed_videos(user_id,category_id)')
    con.execute('CREATE INDEX IF NOT EXISTS user_video_decisions_id ON user_video_decisions(user_id,video_id)')
    con.execute('CREATE INDEX IF NOT EXISTS user_collection_videos_order ON user_collection_videos(user_id,playlist_id,position)')
    con.execute('CREATE INDEX IF NOT EXISTS user_collection_history_category ON user_collection_history(user_id,category_id,watched_at DESC)')
    con.execute('CREATE INDEX IF NOT EXISTS watch_time_daily_collection_period ON watch_time_daily(user_id,library_type,watch_date,collection_category_id)')
    con.commit()

def token_sig(raw): return hmac.new(APP_SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
def hash_password(password,salt=None):
    salt=salt or secrets.token_bytes(16); value=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000); return salt.hex()+'$'+value.hex()
def verify_password(password,encoded):
    try:
        salt,expected=encoded.split('$',1); actual=hash_password(password,bytes.fromhex(salt)).split('$',1)[1]; return hmac.compare_digest(actual,expected)
    except Exception: return False

def normalize_username(value):
    return str(value or '').strip()

def valid_username(value):
    value=normalize_username(value)
    return bool(re.fullmatch(r'[\w.-]{3,32}',value,flags=re.UNICODE) and re.search(r'\w',value,flags=re.UNICODE))

def valid_email(value):
    return bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',str(value or '').strip()))

def valid_account_type(value):
    return value if value in ('user','parent','child') else 'user'

def is_synthetic_email(value):
    return str(value or '').lower().endswith(('@almontaqa.local','@elmontaqa.local'))

def new_child_email():
    return 'child-'+secrets.token_hex(10)+'@almontaqa.local'

def new_link_code():
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(6))

def parse_channel_id(value):
    raw=value.strip()
    if '/playlist' in raw or 'list=' in raw:raise ValueError('هذا رابط قائمة تشغيل. أضِف القائمة من تبويب «قوائم التشغيل».')
    if re.fullmatch(r'UC[A-Za-z0-9_-]{22}',raw):return raw
    handle=None
    if raw.startswith('@') and re.fullmatch(r'@[^\s/?#]{2,80}',raw):handle=raw[1:]
    parsed=urllib.parse.urlparse(raw if '://' in raw else 'https://'+raw)
    parts=[p for p in parsed.path.split('/') if p]
    for i,p in enumerate(parts):
        if p=='channel' and i+1<len(parts) and re.fullmatch(r'UC[A-Za-z0-9_-]{22}',parts[i+1]):return parts[i+1]
        if p.startswith('@') and re.fullmatch(r'@[^\s/?#]{2,80}',p):handle=p[1:]
    if not handle or parsed.hostname not in ('youtube.com','www.youtube.com','m.youtube.com') and not raw.startswith('@'):
        raise ValueError('أدخل رابط قناة YouTube أو @handle صالحًا.')
    handle_url='https://www.youtube.com/@'+urllib.parse.quote(handle,safe='_-.')
    req=urllib.request.Request(handle_url,headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36','Accept-Language':'en-US,en;q=0.9'})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            page=response.read(5_000_000).decode('utf-8','ignore')
            final_path=urllib.parse.urlparse(response.geturl()).path
    except (urllib.error.URLError,TimeoutError) as exc:
        raise ValueError('تعذر الوصول إلى صفحة قناة YouTube: '+str(exc))
    for candidate in (final_path,):
        match=re.search(r'/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)',candidate)
        if match:return match.group(1)
    patterns=(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',r'itemprop=["\']channelId["\'][^>]*content=["\'](UC[A-Za-z0-9_-]{22})["\']',r'content=["\'](UC[A-Za-z0-9_-]{22})["\'][^>]*itemprop=["\']channelId["\']')
    for pattern in patterns:
        match=re.search(pattern,page)
        if match:return match.group(1)
    raise ValueError('لم أستطع استخراج معرّف القناة من صفحة YouTube لهذا الرابط. جرّب رابط /channel/UC… مباشرة.')
def youtube_channel_head(channel_id):
    url='https://www.youtube.com/channel/'+urllib.parse.quote(channel_id,safe='')+'/videos'
    try:
        with YoutubeDL({'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1,'playlist_items':'1:1'}) as ydl:info=ydl.extract_info(url,download=False)
    except Exception as exc:raise ValueError('تعذر قراءة القناة من YouTube: '+str(exc))
    if not info:raise ValueError('لم أستطع قراءة القناة من YouTube. تأكد أنها عامة والرابط صحيح.')
    title=info.get('channel') or info.get('uploader') or info.get('title') or 'قناة YouTube'
    if title.endswith(' - Videos'):title=title[:-9]
    entries=info.get('entries') or [];return title,(entries[0].get('id') or '') if entries and entries[0] else ''

def timestamp_epoch(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError,OverflowError):return None

def later_timestamp(first,second):
    first_epoch=timestamp_epoch(first);second_epoch=timestamp_epoch(second)
    if first_epoch is None:return second or first or ''
    if second_epoch is None:return first or second or ''
    return second if second_epoch>first_epoch else first

def normalize_duration(value):
    try:
        seconds=int(float(value))
        return seconds if seconds>=0 else None
    except (ValueError,TypeError,OverflowError):return None

YOUTUBE_THUMB_RE=re.compile(r'(?:i\d*\.ytimg\.com|img\.youtube\.com)/(?:vi|vi_webp)/([A-Za-z0-9_-]{6,})/')
def best_youtube_thumbnail(url,video_id=''):
    """Return the sharpest public YouTube thumbnail available for a video."""
    candidate=str(url or '').strip()
    match=YOUTUBE_THUMB_RE.search(candidate)
    vid=str(video_id or (match.group(1) if match else '')).strip()
    if (match or not candidate) and re.fullmatch(r'[A-Za-z0-9_-]{11}',vid):
        return 'https://i.ytimg.com/vi/'+vid+'/maxresdefault.jpg'
    return candidate

def sync_source_error(exc):
    """Return a short, user-facing message instead of exposing yt-dlp diagnostics."""
    message=str(exc).lower()
    if any(marker in message for marker in ("sign in to confirm", "not a bot", "cookies-from-browser", "confirm you're not")):
        return 'يطلب YouTube تحققًا مؤقتًا من الخادم. أعد المحاولة لاحقًا.'
    if any(marker in message for marker in ('timed out', 'timeout', 'urlopen error')):
        return 'انتهت مهلة الاتصال بـ YouTube. أعد المحاولة بعد قليل.'
    return 'تعذر الوصول إلى هذا المصدر في YouTube. تأكد أنه متاح للعامة، ثم أعد المحاولة.'

def fetch_video_duration(video_id):
    options={'skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1}
    with YoutubeDL(options) as ydl:info=ydl.extract_info('https://www.youtube.com/watch?v='+urllib.parse.quote(video_id,safe=''),download=False)
    return normalize_duration(info.get('duration')) if info else None

def ensure_video_duration(video):
    duration=normalize_duration(video.get('duration_seconds'))
    if duration is not None:return duration
    try:return fetch_video_duration(video['video_id'])
    except Exception:return None

def fetch_video_identity(video_id):
    """Resolve a video's title and channel name from YouTube's public oEmbed endpoint."""
    url='https://www.youtube.com/oembed?format=json&url='+urllib.parse.quote('https://www.youtube.com/watch?v='+video_id,safe='')
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; Almontaqa/1.0)'})
    with urllib.request.urlopen(request,timeout=8) as response:
        payload=json.loads(response.read().decode('utf-8','replace'))
    title=' '.join(str(payload.get('title') or '').split())
    channel=' '.join(str(payload.get('author_name') or '').split())
    return title,channel

_identity_thread_started=False
_identity_thread_lock=threading.Lock()

def backfill_video_identity(limit=400):
    """Recover channel names for historical watch rows whose source video was removed from the library."""
    con=db()
    try:
        rows=con.execute("""SELECT w.video_id FROM watch_time_daily w
            LEFT JOIN video_identity i ON i.video_id=w.video_id
            WHERE COALESCE(w.channel_title,'')='' AND (i.fetched_at IS NULL OR i.fetched_at<?)
              AND NOT EXISTS(SELECT 1 FROM user_videos v WHERE v.user_id=w.user_id AND v.video_id=w.video_id)
              AND NOT EXISTS(SELECT 1 FROM user_watch_history h WHERE h.user_id=w.user_id AND h.video_id=w.video_id AND COALESCE(h.channel_title,'')<>'')
              AND NOT EXISTS(SELECT 1 FROM user_completed_videos u WHERE u.user_id=w.user_id AND u.video_id=w.video_id AND COALESCE(u.channel_title,'')<>'')
            GROUP BY w.video_id LIMIT ?""",(int(time.time())-30*86400,limit)).fetchall()
        update_tables=[table for table in ('user_watch_history','user_completed_videos','user_collection_history','user_collection_completed','user_collection_videos') if 'channel_title' in {column['name'] for column in con.execute('PRAGMA table_info('+table+')')}]
    finally:con.close()
    for row in rows:
        video_id=row['video_id'];title=channel=''
        try:title,channel=fetch_video_identity(video_id)
        except Exception:pass
        con=db()
        try:
            con.execute("INSERT INTO video_identity(video_id,title,channel_title,fetched_at) VALUES(?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE video_identity.title END,channel_title=CASE WHEN excluded.channel_title<>'' THEN excluded.channel_title ELSE video_identity.channel_title END,fetched_at=excluded.fetched_at",(video_id,title,channel,int(time.time())))
            if channel:
                con.execute("UPDATE watch_time_daily SET channel_title=?,video_title=CASE WHEN COALESCE(video_title,'')='' THEN ? ELSE video_title END WHERE video_id=? AND COALESCE(channel_title,'')=''",(channel,title,video_id))
                for table in update_tables:
                    con.execute('UPDATE '+table+" SET channel_title=? WHERE video_id=? AND COALESCE(channel_title,'')=''",(channel,video_id))
            con.commit()
        except Exception:pass
        finally:con.close()
        time.sleep(0.15)

def start_identity_backfill():
    global _identity_thread_started
    with _identity_thread_lock:
        if _identity_thread_started:return
        _identity_thread_started=True
    threading.Thread(target=_run_identity_backfill,daemon=True).start()

def _run_identity_backfill():
    try:backfill_video_identity()
    except Exception:pass

def youtube_rss_feed(channel_id):
    """Read public metadata without requesting video playback information."""
    url='https://www.youtube.com/feeds/videos.xml?channel_id='+urllib.parse.quote(channel_id,safe='')
    request=urllib.request.Request(url,headers={'User-Agent':'Elmontaqa/2.0'})
    with urllib.request.urlopen(request,timeout=15) as response:
        root=ET.fromstring(response.read(2_000_000))
    ns={'a':'http://www.w3.org/2005/Atom','yt':'http://www.youtube.com/xml/schemas/2015','m':'http://search.yahoo.com/mrss/'}
    title=root.findtext('a:title',default='قناة YouTube',namespaces=ns)
    videos=[]
    for entry in root.findall('a:entry',ns):
        vid=entry.findtext('yt:videoId',namespaces=ns)
        published=entry.findtext('a:published',namespaces=ns)
        if not vid or timestamp_epoch(published) is None:raise ValueError('بيانات نشر غير مكتملة من YouTube؛ حاول مجددًا.')
        thumbnail=entry.find('m:group/m:thumbnail',ns)
        videos.append({'video_id':vid,'title':entry.findtext('a:title',default='(بدون عنوان)',namespaces=ns),
            'description':entry.findtext('m:group/m:description',default='',namespaces=ns),
            'channel_id':channel_id,'channel_title':title,'published_at':published,
            'thumbnail_url':best_youtube_thumbnail(thumbnail.get('url') if thumbnail is not None else '',vid),
            'video_url':'https://www.youtube.com/watch?v='+vid,'duration_seconds':None})
    videos.sort(key=lambda video:timestamp_epoch(video['published_at']),reverse=True)
    return title,videos

def youtube_feed(channel_id,since_timestamp,cursor_video_id=''):
    try:
        title,recent=youtube_rss_feed(channel_id)
    except (urllib.error.URLError,TimeoutError,ET.ParseError,ValueError):
        recent=[]
    if recent and (any(v['video_id']==cursor_video_id for v in recent) or timestamp_epoch(recent[-1]['published_at'])<=since_timestamp):
        # RSS is limited to recent uploads. Only use it when it covers the checkpoint.
        videos=[v for v in recent if timestamp_epoch(v['published_at'])>since_timestamp and v['video_id']!=cursor_video_id]
        return title,videos,(videos[0]['video_id'] if videos else cursor_video_id)
    url='https://www.youtube.com/channel/'+urllib.parse.quote(channel_id,safe='')+'/videos'; videos=[]; batch_size=50; start=1; feed_title='قناة YouTube'; next_cursor=cursor_video_id or ''; reached_cursor=False
    while True:
        try:
            options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1,'playlist_items':f'{start}:{start+batch_size-1}'}
            with YoutubeDL(options) as ydl:info=ydl.extract_info(url,download=False)
            if not info:raise ValueError('لم أستطع قراءة القناة من YouTube. تأكد أنها عامة والرابط صحيح.')
            feed_title=info.get('channel') or info.get('uploader') or info.get('title') or feed_title
            if feed_title.endswith(' - Videos'):feed_title=feed_title[:-9]
            entries=info.get('entries') or []
            if not entries:break
            stop=False
            with YoutubeDL({'skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1}) as ydl:
                for entry in entries:
                    if not entry or not entry.get('id'):continue
                    if cursor_video_id and entry['id']==cursor_video_id:reached_cursor=True;stop=True;break
                    video=ydl.extract_info('https://www.youtube.com/watch?v='+urllib.parse.quote(entry['id'],safe=''),download=False)
                    if not video:raise ValueError('تعذر قراءة بيانات فيديو من YouTube؛ لم تُحدّث نقطة المزامنة. حاول مجددًا لاحقًا.')
                    timestamp=video.get('timestamp')
                    try:published_at=datetime.fromtimestamp(float(timestamp),tz=datetime.now().astimezone().tzinfo).isoformat() if timestamp else ''
                    except (ValueError,TypeError,OverflowError):published_at=''
                    if published_at:
                        try:
                            if datetime.fromisoformat(published_at).timestamp()<=since_timestamp:next_cursor=video['id'];stop=True;break
                        except ValueError:pass
                    if not published_at:raise ValueError('لم تتوفر بيانات نشر الفيديو؛ لم تُحدّث نقطة المزامنة.')
                    feed_title=video.get('channel') or video.get('uploader') or feed_title
                    vid=video['id']; videos.append({'video_id':vid,'title':video.get('title') or '(بدون عنوان)','description':video.get('description') or '', 'channel_id':video.get('channel_id') or channel_id,'channel_title':video.get('channel') or video.get('uploader') or feed_title,'published_at':published_at,'thumbnail_url':best_youtube_thumbnail(video.get('thumbnail'),vid),'video_url':'https://www.youtube.com/watch?v='+vid,'duration_seconds':normalize_duration(video.get('duration'))})
            if stop or len(entries)<batch_size:break
            start+=batch_size
        except ValueError:raise
        except Exception as exc:raise ValueError('تعذر جلب فيديوهات القناة من YouTube: '+str(exc))
    if videos:next_cursor=videos[0]['video_id']
    return feed_title,videos,next_cursor

def youtube_channel_short_ids(channel_id,candidate_ids):
    """Identify actual Shorts from the channel's Shorts tab, not by duration."""
    candidates=set(candidate_ids)
    if not candidates:return set()
    url='https://www.youtube.com/channel/'+urllib.parse.quote(channel_id,safe='')+'/shorts'
    try:
        options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1,'playlist_items':'1:50'}
        with YoutubeDL(options) as ydl:info=ydl.extract_info(url,download=False)
    except Exception as exc:
        message=str(exc)
        if 'does not have a shorts tab' in message.lower():return set()
        raise ValueError('تعذر تحديد فيديوهات Shorts لهذه القناة من YouTube: '+message)
    if not info:raise ValueError('تعذر فتح تبويب Shorts للقناة؛ لم تُحدّث نقطة الجلب. حاول مجددًا.')
    return {entry['id'] for entry in (info.get('entries') or []) if entry and entry.get('id') in candidates}

def parse_playlist_id(value):
    raw=value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{10,100}',raw):return raw
    parsed=urllib.parse.urlparse(raw if '://' in raw else 'https://'+raw)
    query=urllib.parse.parse_qs(parsed.query)
    playlist_id=(query.get('list') or [''])[0]
    if not playlist_id or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',playlist_id):
        raise ValueError('أدخل رابط قائمة تشغيل YouTube عامًا أو معرّف القائمة.')
    return playlist_id

def youtube_playlist_feed(playlist_id,start_index=1,include_details=False,known_video_ids=()):
    url='https://www.youtube.com/playlist?list='+urllib.parse.quote(playlist_id)
    try:
        options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':15,'retries':1,'extractor_retries':1}
        if start_index>1:options['playlist_items']=f'{start_index}:'
        with YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False)
    except Exception as exc:raise ValueError('تعذر قراءة قائمة YouTube. تأكد أنها عامة وأن الرابط صحيح: '+str(exc))
    if not info:raise ValueError('لم أستطع فتح قائمة التشغيل. تأكد أنها عامة.')
    videos=[];entries=info.get('entries') or []
    # Membership, not upload time, decides whether a playlist video is new.
    # Flat listings supply titles and thumbnails without bot-blocked playback calls.
    for entry in entries:
        if not entry or not entry.get('id'):continue
        vid=entry['id'];channel=entry.get('channel_id') or entry.get('uploader_id') or ''
        videos.append({'video_id':vid,'title':entry.get('title') or '(بدون عنوان)','description':entry.get('description') or '',
            'channel_id':channel,'channel_title':entry.get('channel') or entry.get('uploader') or 'YouTube',
            'published_at':datetime.now().astimezone().isoformat(),
            'thumbnail_url':best_youtube_thumbnail(entry.get('thumbnail') or (entry.get('thumbnails',[{}])[-1].get('url') if entry.get('thumbnails') else None),vid),
            'video_url':'https://www.youtube.com/watch?v='+vid,'duration_seconds':normalize_duration(entry.get('duration'))})
    total_count=info.get('playlist_count')
    try:total_count=int(total_count) if total_count is not None else start_index+len(entries)-1
    except (ValueError,TypeError):total_count=start_index+len(entries)-1
    return info.get('title') or 'قائمة تشغيل YouTube',videos,total_count

def youtube_playlist_library_feed(playlist_id):
    """Read the full current playlist once for the user's separate playlist library."""
    url='https://www.youtube.com/playlist?list='+urllib.parse.quote(playlist_id,safe='')
    try:
        options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':False,'socket_timeout':25,'retries':1,'extractor_retries':1}
        with YoutubeDL(options) as ydl:info=ydl.extract_info(url,download=False)
    except Exception as exc:raise ValueError('تعذر قراءة قائمة YouTube كاملة. تأكد أنها عامة وأن الرابط صحيح: '+sync_source_error(exc))
    if not info:raise ValueError('لم أستطع فتح قائمة التشغيل. تأكد أنها عامة.')
    now_iso=datetime.now().astimezone().isoformat();videos=[]
    for position,entry in enumerate(info.get('entries') or [],1):
        if not entry or not entry.get('id'):continue
        video_id=str(entry['id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):continue
        timestamp=entry.get('release_timestamp') or entry.get('timestamp')
        try:published_at=datetime.fromtimestamp(float(timestamp),tz=datetime.now().astimezone().tzinfo).isoformat() if timestamp else ''
        except (ValueError,TypeError,OverflowError):published_at=''
        if not published_at:
            upload_date=str(entry.get('upload_date') or '')
            try:published_at=datetime.strptime(upload_date,'%Y%m%d').astimezone().isoformat() if re.fullmatch(r'\d{8}',upload_date) else now_iso
            except ValueError:published_at=now_iso
        thumbnails=entry.get('thumbnails') or []
        videos.append({'position':position,'video_id':video_id,'title':entry.get('title') or '(بدون عنوان)',
            'channel_title':entry.get('channel') or entry.get('uploader') or info.get('channel') or 'YouTube',
            'published_at':published_at,'thumbnail_url':best_youtube_thumbnail(entry.get('thumbnail') or (thumbnails[-1].get('url') if thumbnails else None),video_id),
            'video_url':'https://www.youtube.com/watch?v='+video_id,'duration_seconds':normalize_duration(entry.get('duration'))})
    thumbs=info.get('thumbnails') or []
    playlist_thumbnail=info.get('thumbnail') or (thumbs[-1].get('url') if thumbs else None) or (videos[0]['thumbnail_url'] if videos else None)
    playlist_thumbnail=best_youtube_thumbnail(playlist_thumbnail,videos[0]['video_id'] if videos else '')
    return info.get('title') or 'قائمة تشغيل YouTube',info.get('channel') or info.get('uploader') or 'YouTube',playlist_thumbnail,videos

def remove_channel_from_library(con,user_id,channel_id):
    channel=con.execute('SELECT id,youtube_channel_id FROM user_channels WHERE id=? AND user_id=?',(channel_id,user_id)).fetchone()
    if not channel:raise LookupError('القناة غير موجودة')
    candidate_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_videos WHERE user_id=? AND channel_id=?',(user_id,channel['youtube_channel_id']))}
    candidate_ids.update(r['video_id'] for r in con.execute('SELECT video_id FROM user_video_sources WHERE user_id=? AND source_type=\'channel\' AND source_id=?',(user_id,str(channel_id))))
    keep_ids=set();delete_ids=set()
    for video_id in candidate_ids:
        video=con.execute('SELECT channel_id FROM user_videos WHERE user_id=? AND video_id=?',(user_id,video_id)).fetchone()
        if not video:continue
        other_source=con.execute('SELECT 1 FROM user_video_sources WHERE user_id=? AND video_id=? AND NOT(source_type=\'channel\' AND source_id=?) LIMIT 1',(user_id,video_id,str(channel_id))).fetchone()
        other_playlist=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND video_id=? LIMIT 1',(user_id,video_id)).fetchone()
        (keep_ids if other_source or other_playlist else delete_ids).add(video_id)
    con.execute('DELETE FROM user_video_sources WHERE user_id=? AND source_type=\'channel\' AND source_id=?',(user_id,str(channel_id)))
    if delete_ids:
        marks=','.join('?' for _ in delete_ids)
        con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+marks+')',(user_id,*delete_ids))
        con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+marks+')',(user_id,*delete_ids))
    con.execute('DELETE FROM user_channels WHERE id=? AND user_id=?',(channel_id,user_id))
    return {'removed_videos':len(delete_ids),'kept_shared_videos':len(keep_ids)}

def remove_followed_playlist(con,user_id,playlist_id):
    playlist=con.execute('SELECT id,title,created_at FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user_id)).fetchone()
    if not playlist:raise LookupError('قائمة التشغيل غير موجودة')
    candidate_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_video_sources WHERE user_id=? AND source_type=\'playlist\' AND source_id=?',(user_id,str(playlist_id)))}
    candidate_ids.update(r['video_id'] for r in con.execute('SELECT s.video_id FROM user_playlist_seen s JOIN user_videos v ON v.user_id=s.user_id AND v.video_id=s.video_id WHERE s.user_id=? AND s.playlist_id=? AND v.created_at>=?',(user_id,playlist_id,playlist['created_at'])))
    keep_ids=set();delete_ids=set()
    for video_id in candidate_ids:
        video=con.execute('SELECT channel_id FROM user_videos WHERE user_id=? AND video_id=?',(user_id,video_id)).fetchone()
        if not video:continue
        other_source=con.execute('SELECT 1 FROM user_video_sources WHERE user_id=? AND video_id=? AND NOT(source_type=\'playlist\' AND source_id=?) LIMIT 1',(user_id,video_id,str(playlist_id))).fetchone()
        active_channel=con.execute('SELECT 1 FROM user_channels WHERE user_id=? AND youtube_channel_id=? LIMIT 1',(user_id,video['channel_id'])).fetchone()
        other_playlist=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND video_id=? AND playlist_id<>? LIMIT 1',(user_id,video_id,playlist_id)).fetchone()
        (keep_ids if other_source or active_channel or other_playlist else delete_ids).add(video_id)
    if delete_ids:
        marks=','.join('?' for _ in delete_ids)
        con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+marks+')',(user_id,*delete_ids))
        con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+marks+')',(user_id,*delete_ids))
    con.execute('DELETE FROM user_video_sources WHERE user_id=? AND source_type=\'playlist\' AND source_id=?',(user_id,str(playlist_id)))
    con.execute('DELETE FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user_id))
    return {'title':playlist['title'],'removed_videos':len(delete_ids),'kept_shared_videos':len(keep_ids)}

class Handler(BaseHTTPRequestHandler):
    server_version='ElMontaqa/2.0'
    def log_message(self,fmt,*args): print('%s - %s'%(self.log_date_time_string(),fmt%args))
    def send_json(self,status,value,headers=None):
        body=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','same-origin')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def redirect(self,location):
        self.send_response(302); self.send_header('Location',location); self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','same-origin'); self.end_headers()
    def read_json(self):
        try:
            lengths=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(lengths)>1:return None
            length=lengths[0] if lengths else '0'
            if not re.fullmatch(r'[0-9]{1,5}',length):return None
            n=int(length)
            if n>20000:return None
            if n and self.headers.get_content_type()!='application/json':return None
            # Bound incomplete-body reads as well as their size. Negative lengths
            # must never reach read(), where they mean read until EOF.
            self.connection.settimeout(10)
            raw=self.rfile.read(n)
            if len(raw)!=n:return None
            value=json.loads(raw or b'{}')
            return value if isinstance(value,dict) else None
        except Exception:return None
    def _session_token_hash(self):
        cookie=SimpleCookie(self.headers.get('Cookie','')); morsel=cookie.get('session')
        if not morsel:return None
        try:
            raw,sig=morsel.value.split('.',1)
            if not hmac.compare_digest(sig,token_sig(raw)):return None
            return hashlib.sha256(raw.encode()).hexdigest()
        except Exception:return None
    def session(self):
        cookie=SimpleCookie(self.headers.get('Cookie','')); morsel=cookie.get('session')
        if not morsel:return None
        try:
            raw,sig=morsel.value.split('.',1)
            if not hmac.compare_digest(sig,token_sig(raw)):return None
            con=db()
            try:
                row=con.execute('''SELECT users.id,users.email,users.name,users.username,COALESCE(users.account_type,'user') AS account_type,
                    users.parent_user_id,COALESCE(users.parent_mode,'user') AS parent_mode,users.created_at,
                    COALESCE(users.hide_thumbnails,0) AS hide_thumbnails,COALESCE(users.watch_later_enabled,0) AS watch_later_enabled,
                    COALESCE(users.usage_limits_enabled,0) AS usage_limits_enabled,sessions.active_child_id
                    FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?''',(hashlib.sha256(raw.encode()).hexdigest(),int(time.time()))).fetchone()
                if not row:return None
                owner_id=row['id'];owner_name=row['name']
                if row['active_child_id']:
                    child=con.execute('''SELECT id,email,name,username,COALESCE(account_type,'user') AS account_type,parent_user_id,
                        COALESCE(parent_mode,'user') AS parent_mode,created_at,COALESCE(hide_thumbnails,0) AS hide_thumbnails,
                        COALESCE(watch_later_enabled,0) AS watch_later_enabled,COALESCE(usage_limits_enabled,0) AS usage_limits_enabled
                        FROM users WHERE id=? AND account_type='child' AND parent_user_id=?''',(row['active_child_id'],owner_id)).fetchone()
                    if child:
                        res=dict(child);res['is_supervising']=True;res['supervisor_id']=owner_id;res['supervisor_name']=owner_name
                    else:
                        res=dict(row);res.pop('active_child_id',None);res['is_supervising']=False;res['supervisor_id']=None;res['supervisor_name']=None
                else:
                    res=dict(row);res.pop('active_child_id',None);res['is_supervising']=False;res['supervisor_id']=None;res['supervisor_name']=None
                res['account_type']=res.get('account_type') or 'user';res['parent_mode']=res.get('parent_mode') or 'user'
                res['hide_thumbnails']=bool(res['hide_thumbnails']);res['watch_later_enabled']=bool(res['watch_later_enabled']);res['usage_limits_enabled']=bool(res['usage_limits_enabled'])
                if res['account_type']=='child' and not res['is_supervising'] and res.get('parent_user_id'):
                    parent=con.execute('SELECT name FROM users WHERE id=?',(res['parent_user_id'],)).fetchone()
                    res['supervisor_name']=parent['name'] if parent else None
                return res
            finally:con.close()
        except Exception:return None
    def account_owner_id(self,user):
        if not user:return None
        if user.get('is_supervising'):return user.get('supervisor_id')
        if user.get('account_type')=='parent':return user['id']
        return None
    def child_view_locked(self,user):
        return bool(user and user.get('account_type')=='child' and not user.get('is_supervising'))
    def load_children(self,con,owner_id):
        if not owner_id:return []
        rows=con.execute('''SELECT u.id,u.name,u.username,u.created_at,
            (SELECT COUNT(*) FROM user_videos v WHERE v.user_id=u.id) AS videos,
            (SELECT COUNT(*) FROM user_collection_playlists p WHERE p.user_id=u.id) AS playlists,
            (SELECT COALESCE(SUM(w.seconds),0) FROM watch_time_daily w WHERE w.user_id=u.id) AS watch_seconds
            FROM users u WHERE u.parent_user_id=? AND u.account_type='child' ORDER BY u.id''',(owner_id,)).fetchall()
        return [dict(row) for row in rows]
    def user_payload(self,con,user):
        if not user:return None
        keys=('id','name','email','username','account_type','parent_mode','created_at','hide_thumbnails','watch_later_enabled','usage_limits_enabled','is_supervising','supervisor_id','supervisor_name','parent_user_id')
        payload={key:user.get(key) for key in keys}
        payload['email']='' if is_synthetic_email(payload.get('email')) else payload.get('email')
        payload['account_type']=payload.get('account_type') or 'user'
        payload['parent_mode']=payload.get('parent_mode') or 'user'
        payload['is_supervising']=bool(payload.get('is_supervising'))
        payload['can_manage']=payload['account_type']!='child' or payload['is_supervising']
        payload['is_child_view']=payload['account_type']=='child' and not payload['is_supervising']
        payload['parent_only']=payload['account_type']=='parent' and payload['parent_mode']=='supervisor' and not payload['is_supervising']
        owner_id=payload['supervisor_id'] if payload['is_supervising'] else (payload['id'] if payload['account_type']=='parent' else None)
        payload['children']=self.load_children(con,owner_id) if owner_id else []
        if payload['account_type']=='child' and not payload['is_supervising'] and not payload.get('supervisor_name') and payload.get('parent_user_id'):
            parent=con.execute('SELECT name FROM users WHERE id=?',(payload['parent_user_id'],)).fetchone()
            payload['supervisor_name']=parent['name'] if parent else None
        return payload
    def cookie_login(self,raw):
        secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
        return f'session={raw}.{token_sig(raw)}; HttpOnly{secure}; SameSite=Lax; Path=/; Max-Age=2592000'
    def cookie_logout(self):
        secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''
        return f'session=; HttpOnly{secure}; SameSite=Lax; Path=/; Max-Age=0'
    def admin_ok(self): return bool(ADMIN_TOKEN) and hmac.compare_digest(self.headers.get('X-Admin-Token',''),ADMIN_TOKEN)
    def need_admin(self):
        if not self.admin_ok(): self.send_json(403,{'error':'رمز الإدارة غير صحيح'}); return False
        return True
    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path
        if path=='/health':return self.send_json(200,{'ok':True})
        if path in ('/icon.svg','/favicon.ico','/favicon.png','/favicon-96x96.png','/apple-touch-icon.png','/apple-touch-icon-180.png','/icon-192.png','/icon-512.png','/og.png','/site.webmanifest'):
            asset={
                '/icon.svg':('icon.svg','image/svg+xml'),
                '/favicon.ico':('favicon.ico','image/x-icon'),
                '/favicon.png':('apple-touch-icon.png','image/png'),
                '/favicon-96x96.png':('favicon-96x96.png','image/png'),
                '/apple-touch-icon.png':('apple-touch-icon.png','image/png'),
                '/apple-touch-icon-180.png':('apple-touch-icon-180.png','image/png'),
                '/icon-192.png':('icon-192.png','image/png'),
                '/icon-512.png':('icon-512.png','image/png'),
                '/og.png':('og.png','image/png'),
                '/site.webmanifest':('site.webmanifest','application/manifest+json; charset=utf-8'),
            }[path]
            try:
                with open(asset[0],'rb') as f:body=f.read()
                self.send_response(200);self.send_header('Content-Type',asset[1]);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','public, max-age=86400');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
            except OSError:self.send_error(404)
            return
        if path=='/api/me':
            user=self.session()
            if not user:return self.send_json(200,{'user':None})
            con=db()
            try:return self.send_json(200,{'user':self.user_payload(con,user)})
            finally:con.close()
        if path=='/api/me/updates':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db()
            try:
                vids=con.execute('SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest FROM user_videos WHERE user_id=?',(user['id'],)).fetchone()
                pls=con.execute('SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest FROM user_collection_playlists WHERE user_id=?',(user['id'],)).fetchone()
                colv=con.execute('SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest FROM user_collection_videos WHERE user_id=?',(user['id'],)).fetchone()
                sync=con.execute('SELECT last_sync_at,last_sync_added FROM user_sync_state WHERE user_id=?',(user['id'],)).fetchone()
            finally:con.close()
            sync_at=int(sync['last_sync_at']) if sync else 0
            added=int(sync['last_sync_added']) if sync else 0
            version='%d:%d:%d:%d:%d:%d'%(vids['total'],vids['latest'],pls['total'],pls['latest'],colv['total'],colv['latest'])
            return self.send_json(200,{'version':version,'videos':int(vids['total']),'playlists':int(pls['total']),'playlist_videos':int(colv['total']),'last_sync_at':sync_at,'last_sync_added':added,'server_time':int(time.time())})
        if path=='/api/me/children':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            owner_id=self.account_owner_id(user)
            if not owner_id:return self.send_json(403,{'error':'هذا الخيار متاح لحساب ولي الأمر'})
            con=db()
            try:return self.send_json(200,{'children':self.load_children(con,owner_id)})
            finally:con.close()
        if path=='/api/feed':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض مكتبتك'})
            con=db(); cats=[dict(r) for r in con.execute('SELECT id,name FROM user_categories WHERE user_id=? ORDER BY id',(user['id'],))]
            saved_order=con.execute('SELECT category_ids FROM user_category_orders WHERE user_id=?',(user['id'],)).fetchone()
            if saved_order:
                try:
                    order_ids=[int(value) for value in json.loads(saved_order['category_ids'])]
                    order_index={category_id:index for index,category_id in enumerate(order_ids)}
                    cats.sort(key=lambda category:(order_index.get(category['id'],len(order_index)),category['id']))
                except (TypeError,ValueError,json.JSONDecodeError):pass
            rows=con.execute('SELECT v.video_id,v.title,v.channel_title,v.category_id,c.name AS category,v.published_at,v.thumbnail_url,v.video_url,v.duration_seconds,v.watch_later,CASE WHEN v.watched=1 OR EXISTS(SELECT 1 FROM user_completed_videos h WHERE h.user_id=v.user_id AND h.video_id=v.video_id) THEN 1 ELSE 0 END AS watched,CASE WHEN EXISTS(SELECT 1 FROM user_watch_history h WHERE h.user_id=v.user_id AND h.video_id=v.video_id) THEN 1 ELSE 0 END AS in_history FROM user_videos v JOIN user_categories c ON c.id=v.category_id WHERE v.user_id=? ORDER BY v.published_at DESC LIMIT 100',(user['id'],)).fetchall(); con.close(); return self.send_json(200,{'categories':cats,'videos':[dict(r) for r in rows]})
        if path=='/api/me/playlist-library':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض مكتبتك'})
            con=db()
            try:
                categories=[dict(row) for row in con.execute('SELECT c.id,c.name,(SELECT COUNT(*) FROM user_collection_playlists p WHERE p.user_id=c.user_id AND p.category_id=c.id) AS playlist_count,(SELECT COUNT(*) FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id WHERE v.user_id=c.user_id AND p.category_id=c.id) AS video_count FROM user_collection_categories c WHERE c.user_id=? ORDER BY c.id',(user['id'],))]
                saved_order=con.execute('SELECT category_ids FROM user_collection_category_orders WHERE user_id=?',(user['id'],)).fetchone()
                if saved_order:
                    try:
                        order_ids=[int(value) for value in json.loads(saved_order['category_ids'])]
                        order_index={category_id:index for index,category_id in enumerate(order_ids)}
                        categories.sort(key=lambda category:(order_index.get(category['id'],len(order_index)),category['id']))
                    except (TypeError,ValueError,json.JSONDecodeError):pass
                playlists=[dict(row) for row in con.execute('SELECT p.id,p.youtube_playlist_id,p.title,p.channel_title,p.thumbnail_url,p.category_id,c.name AS category,p.added_at,(SELECT v.video_id FROM user_collection_videos v WHERE v.user_id=p.user_id AND v.playlist_id=p.id ORDER BY v.position,v.rowid LIMIT 1) AS thumbnail_video_id,(SELECT COUNT(*) FROM user_collection_videos v WHERE v.user_id=p.user_id AND v.playlist_id=p.id) AS video_count,(SELECT COUNT(*) FROM user_collection_completed done WHERE done.user_id=p.user_id AND done.playlist_id=p.id) AS completed_count,(SELECT COALESCE(SUM(v.duration_seconds),0) FROM user_collection_videos v WHERE v.user_id=p.user_id AND v.playlist_id=p.id) AS available_duration_seconds FROM user_collection_playlists p JOIN user_collection_categories c ON c.id=p.category_id WHERE p.user_id=? ORDER BY p.id DESC',(user['id'],))]
            finally:con.close()
            return self.send_json(200,{'categories':categories,'playlists':playlists})
        if path in ('/api/me/playlist-library/items','/api/me/playlist-library/item'):
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض مكتبتك'})
            query=urllib.parse.parse_qs(parsed.query)
            try:playlist_id=int(query.get('playlist_id',[''])[0])
            except (TypeError,ValueError):return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            con=db()
            try:
                playlist=con.execute('SELECT p.id,p.youtube_playlist_id,p.title,p.channel_title,p.thumbnail_url,p.category_id,c.name AS category,p.added_at FROM user_collection_playlists p JOIN user_collection_categories c ON c.id=p.category_id WHERE p.user_id=? AND p.id=?',(user['id'],playlist_id)).fetchone()
                if not playlist:return self.send_json(404,{'error':'قائمة التشغيل غير موجودة في مكتبتك'})
                if path.endswith('/item'):
                    video_id=query.get('video_id',[''])[0]
                    video=con.execute('SELECT v.video_id,v.title,v.channel_title,v.published_at,v.thumbnail_url,v.video_url,v.duration_seconds,v.position,p.id AS playlist_id,p.title AS playlist_title,p.category_id,c.name AS category FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id JOIN user_collection_categories c ON c.id=p.category_id WHERE v.user_id=? AND v.playlist_id=? AND v.video_id=?',(user['id'],playlist_id,video_id)).fetchone()
                    if not video:return self.send_json(404,{'error':'الفيديو غير موجود في قائمة التشغيل'})
                    return self.send_json(200,{'video':dict(video)})
                videos=[dict(row) for row in con.execute('SELECT v.video_id,v.title,v.channel_title,v.published_at,v.thumbnail_url,v.video_url,v.duration_seconds,v.position,p.id AS playlist_id,p.title AS playlist_title,p.category_id,c.name AS category FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id JOIN user_collection_categories c ON c.id=p.category_id WHERE v.user_id=? AND v.playlist_id=? ORDER BY v.position,v.rowid',(user['id'],playlist_id))]
            finally:con.close()
            return self.send_json(200,{'playlist':dict(playlist),'videos':videos})
        if path in ('/api/me/statistics','/api/me/statistics/history','/api/me/statistics/detail','/api/me/statistics/calendar'):
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض إحصائياتك'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'إحصائيات هذا الحساب تظهر لولي الأمر فقط'})
            query=urllib.parse.parse_qs(parsed.query)
            def qparam(name,default=''):
                values=query.get(name) or []
                return values[0] if values else default
            try:offset_minutes=int(qparam('tz_offset','0') or 0)
            except (TypeError,ValueError):offset_minutes=0
            offset_minutes=max(-840,min(840,offset_minutes))
            offset=offset_minutes*60
            today_value=qparam('today','')
            try:today=date.fromisoformat(today_value) if today_value else stats_engine.local_date(int(time.time()),offset)
            except ValueError:today=stats_engine.local_date(int(time.time()),offset)
            library=qparam('library','video')
            if library not in ('video','collection'):library='video'
            month_key=qparam('month','') or today.strftime('%Y-%m')
            stats_period=stats_engine.period(qparam('period','30d'),today,offset)
            con=db()
            try:
                if path=='/api/me/statistics':
                    if library=='video':payload,_=stats_engine.video_stats(con,user['id'],user['created_at'],stats_period,offset,month_key,today)
                    else:payload,_=stats_engine.collection_stats(con,user['id'],user['created_at'],stats_period,offset,month_key,today)
                    return self.send_json(200,payload)
                if path=='/api/me/statistics/calendar':
                    return self.send_json(200,stats_engine.calendar(con,user['id'],library,month_key,today,offset,user['created_at']))
                if path=='/api/me/statistics/detail':
                    detail_type=qparam('type','');detail_key=qparam('key','')
                    if not detail_key:return self.send_json(400,{'error':'التفاصيل غير صالحة'})
                    try:
                        if library=='video':result=stats_engine.video_detail(con,user['id'],user['created_at'],stats_period,offset,month_key,today,detail_type,detail_key)
                        else:result=stats_engine.collection_detail(con,user['id'],user['created_at'],stats_period,offset,month_key,today,detail_type,detail_key)
                    except (ValueError,TypeError):return self.send_json(400,{'error':'التفاصيل غير صالحة'})
                    return self.send_json(200,result)
                try:
                    page=max(1,int(qparam('page','1') or 1));limit=max(10,min(100,int(qparam('limit','40') or 40)))
                except (TypeError,ValueError):page,limit=1,40
                category=qparam('category','all');channel=qparam('channel','all');needle=qparam('q','');playlist=qparam('playlist','all')
                try:
                    if library=='video':result=stats_engine.video_history(con,user['id'],user['created_at'],stats_period,offset,month_key,today,needle,category,channel,page,limit)
                    else:result=stats_engine.collection_history(con,user['id'],user['created_at'],stats_period,offset,month_key,today,needle,category,channel,playlist,page,limit)
                except (ValueError,TypeError):return self.send_json(400,{'error':'خيارات السجل غير صالحة'})
                return self.send_json(200,result)
            finally:
                con.close()
        if path=='/api/me/settings':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'إعدادات هذا الحساب يديرها ولي الأمر'})
            con=db()
            try:
                categories=[dict(row) for row in con.execute('SELECT c.id,c.name,c.daily_limit_minutes,(SELECT COUNT(*) FROM user_channels ch WHERE ch.user_id=c.user_id AND ch.category_id=c.id) AS channel_count,(SELECT COUNT(*) FROM user_playlists pl WHERE pl.user_id=c.user_id AND pl.category_id=c.id) AS playlist_count,(SELECT COUNT(*) FROM user_videos v WHERE v.user_id=c.user_id AND v.category_id=c.id) AS video_count FROM user_categories c WHERE c.user_id=? ORDER BY c.id',(user['id'],))]
                channels=[dict(row) for row in con.execute('SELECT ch.id,ch.youtube_channel_id,ch.title,ch.category_id,ch.ignore_shorts,c.name AS category FROM user_channels ch JOIN user_categories c ON c.id=ch.category_id WHERE ch.user_id=? ORDER BY ch.id',(user['id'],))]
                playlists=[dict(row) for row in con.execute('SELECT pl.id,pl.youtube_playlist_id,pl.title,pl.category_id,c.name AS category FROM user_playlists pl JOIN user_categories c ON c.id=pl.category_id WHERE pl.user_id=? ORDER BY pl.id',(user['id'],))]
            finally:con.close()
            return self.send_json(200,{'categories':categories,'channels':channels,'playlists':playlists,'hide_thumbnails':bool(user.get('hide_thumbnails')),'watch_later_enabled':bool(user.get('watch_later_enabled')),'usage_limits_enabled':bool(user.get('usage_limits_enabled'))})
        if path=='/api/admin/feedback':
            if not self.need_admin():return
            con=db(); rows=con.execute('SELECT feedback.message,feedback.created_at,users.name,users.email FROM feedback LEFT JOIN users ON users.id=feedback.user_id ORDER BY feedback.created_at DESC LIMIT 500').fetchall(); con.close(); return self.send_json(200,{'feedback':[dict(r) for r in rows]})
        if path in ('/','/statistics','/watch','/playlists','/supervision','/settings','/account','/login','/register'):
            try:
                with open('index.html','rb') as f: body=f.read()
                self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','same-origin'); self.end_headers(); self.wfile.write(body)
            except OSError:self.send_error(500)
            return
        self.send_error(404)
    def do_POST(self):
        path=urlparse(self.path).path; data=self.read_json()
        if data is None:return self.send_json(400,{'error':'بيانات غير صالحة'})
        child_allowed_writes={'/api/login','/api/register','/api/logout','/api/feedback','/api/me/watch-time','/api/me/sync','/api/me/switch-account','/api/me/videos/remove','/api/me/playlist-library/action'}
        if path not in child_allowed_writes:
            current=self.session()
            if self.child_view_locked(current):return self.send_json(403,{'error':'حساب الطفل للعرض فقط. يعدّل بياناته ولي الأمر.'})
        if path=='/api/me/settings/save-all':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            row_specs={
                'channels':('channel_id',('category_id','ignore_shorts')),
                'playlists':('playlist_id',('category_id',)),
                'categories':('category_id',('name','daily_limit_minutes')),
                'collection_categories':('category_id',('name',)),
                'collection_playlists':('playlist_id',('category_id',)),
            }
            remove_specs={'remove_channel_ids':'channel_id','remove_playlist_ids':'playlist_id','remove_category_ids':'category_id','remove_collection_category_ids':'category_id','remove_collection_playlist_ids':'playlist_id'}
            rows={};removals={}
            try:
                for key,(id_key,fields) in row_specs.items():
                    values=data.get(key,[])
                    if not isinstance(values,list) or len(values)>500:raise ValueError
                    parsed=[]
                    for item in values:
                        if not isinstance(item,dict):raise ValueError
                        record={id_key:int(item.get(id_key))}
                        if record[id_key]<1:raise ValueError
                        for field in fields:
                            value=item.get(field)
                            if field=='name':
                                value=str(value or '').strip()[:100]
                                if not value:raise ValueError
                            elif field=='ignore_shorts':
                                if not isinstance(value,bool):raise ValueError
                            elif field=='daily_limit_minutes':
                                if not isinstance(value,(int,float)) or isinstance(value,bool):raise ValueError
                                value=int(value)
                                if not 0<=value<=1440:raise ValueError
                            else:
                                value=int(value)
                                if value<1:raise ValueError
                            record[field]=value
                        parsed.append(record)
                    rows[key]=parsed
                for key,id_key in remove_specs.items():
                    values=data.get(key,[])
                    if not isinstance(values,list) or len(values)>500:raise ValueError
                    parsed=[int(value) for value in values]
                    if any(value<1 for value in parsed):raise ValueError
                    removals[key]=parsed
            except (TypeError,ValueError):return self.send_json(400,{'error':'توجد تغييرات غير صالحة في الإعدادات'})
            total=sum(map(len,rows.values()))+sum(map(len,removals.values()))
            if total>1500:return self.send_json(400,{'error':'عدد التغييرات أكبر من المسموح'})
            id_groups={
                'channels':([item['channel_id'] for item in rows['channels']],removals['remove_channel_ids']),
                'playlists':([item['playlist_id'] for item in rows['playlists']],removals['remove_playlist_ids']),
                'categories':([item['category_id'] for item in rows['categories']],removals['remove_category_ids']),
                'collection_categories':([item['category_id'] for item in rows['collection_categories']],removals['remove_collection_category_ids']),
                'collection_playlists':([item['playlist_id'] for item in rows['collection_playlists']],removals['remove_collection_playlist_ids']),
            }
            if any(len(set(updated+removed))!=len(updated+removed) for updated,removed in id_groups.values()):return self.send_json(400,{'error':'عنصر مكرر في تغييرات الإعدادات'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                table_specs={'channels':'user_channels','playlists':'user_playlists','categories':'user_categories','collection_categories':'user_collection_categories','collection_playlists':'user_collection_playlists'}
                for key,(updated,removed) in id_groups.items():
                    ids=updated+removed
                    if not ids:continue
                    marks=','.join('?' for _ in ids)
                    found={item['id'] for item in con.execute('SELECT id FROM '+table_specs[key]+' WHERE user_id=? AND id IN ('+marks+')',(user['id'],*ids))}
                    if found!=set(ids):return self.send_json(404,{'error':'عنصر واحد أو أكثر غير موجود في حسابك'})
                ref_groups=(
                    ('channels','category_id','user_categories',removals['remove_category_ids']),
                    ('playlists','category_id','user_categories',removals['remove_category_ids']),
                    ('collection_playlists','category_id','user_collection_categories',removals['remove_collection_category_ids']),
                )
                for key,field,table,deleted in ref_groups:
                    ids={item[field] for item in rows[key]}
                    if ids & set(deleted):return self.send_json(409,{'error':'لا يمكن تعيين عنصر إلى تصنيف مُجهّز للإزالة'})
                    if ids:
                        marks=','.join('?' for _ in ids);found={item['id'] for item in con.execute('SELECT id FROM '+table+' WHERE user_id=? AND id IN ('+marks+')',(user['id'],*ids))}
                        if found!=ids:return self.send_json(404,{'error':'تصنيف واحد أو أكثر غير موجود في حسابك'})
                def apply_category_names(table,key,id_key):
                    items=rows[key]
                    if not items:return
                    changed={item[id_key]:item['name'] for item in items}
                    removed=set(removals['remove_category_ids'] if key=='categories' else removals['remove_collection_category_ids'])
                    existing={item['id']:item['name'] for item in con.execute('SELECT id,name FROM '+table+' WHERE user_id=?',(user['id'],)) if item['id'] not in removed}
                    existing.update(changed)
                    names=list(existing.values())
                    if len(names)!=len(set(names)):raise ValueError('يوجد تصنيف آخر بهذا الاسم')
                    for category_id in changed:
                        con.execute('UPDATE '+table+' SET name=? WHERE id=? AND user_id=?',(f'__pending_{secrets.token_hex(10)}__',category_id,user['id']))
                    con.executemany('UPDATE '+table+' SET name=? WHERE id=? AND user_id=?',[(name,category_id,user['id']) for category_id,name in changed.items()])
                apply_category_names('user_categories','categories','category_id')
                apply_category_names('user_collection_categories','collection_categories','category_id')
                if rows['categories']:con.executemany('UPDATE user_categories SET daily_limit_minutes=? WHERE id=? AND user_id=?',[(item['daily_limit_minutes'],item['category_id'],user['id']) for item in rows['categories']])
                if rows['channels']:con.executemany('UPDATE user_channels SET category_id=?,ignore_shorts=? WHERE id=? AND user_id=?',[(item['category_id'],int(item['ignore_shorts']),item['channel_id'],user['id']) for item in rows['channels']])
                if rows['playlists']:con.executemany('UPDATE user_playlists SET category_id=? WHERE id=? AND user_id=?',[(item['category_id'],item['playlist_id'],user['id']) for item in rows['playlists']])
                if rows['collection_playlists']:con.executemany('UPDATE user_collection_playlists SET category_id=? WHERE id=? AND user_id=?',[(item['category_id'],item['playlist_id'],user['id']) for item in rows['collection_playlists']])
                if 'hide_thumbnails' in data:
                    con.execute('UPDATE users SET hide_thumbnails=? WHERE id=?',(1 if data.get('hide_thumbnails') else 0,user['id']))
                if 'watch_later_enabled' in data:
                    con.execute('UPDATE users SET watch_later_enabled=? WHERE id=?',(1 if data.get('watch_later_enabled') else 0,user['id']))
                if 'usage_limits_enabled' in data:
                    con.execute('UPDATE users SET usage_limits_enabled=? WHERE id=?',(1 if data.get('usage_limits_enabled') else 0,user['id']))
                totals={'removed_videos':0,'kept_shared_videos':0}
                for channel_id in removals['remove_channel_ids']:
                    result=remove_channel_from_library(con,user['id'],channel_id);totals['removed_videos']+=result['removed_videos'];totals['kept_shared_videos']+=result['kept_shared_videos']
                for playlist_id in removals['remove_playlist_ids']:
                    result=remove_followed_playlist(con,user['id'],playlist_id);totals['removed_videos']+=result['removed_videos'];totals['kept_shared_videos']+=result['kept_shared_videos']
                collection_videos_removed=0
                for playlist_id in removals['remove_collection_playlist_ids']:
                    collection_videos_removed+=con.execute('SELECT COUNT(*) FROM user_collection_videos WHERE user_id=? AND playlist_id=?',(user['id'],playlist_id)).fetchone()[0]
                    con.execute('DELETE FROM user_collection_playlists WHERE id=? AND user_id=?',(playlist_id,user['id']))
                for category_id in removals['remove_category_ids']:
                    counts=[con.execute('SELECT COUNT(*) FROM '+table+' WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0] for table in ('user_channels','user_playlists','user_videos')]
                    if any(counts):raise ValueError('لا يمكن إزالة تصنيف مكتبة الفيديوهات قبل نقل المصادر أو إزالة الفيديوهات المرتبطة به')
                    con.execute('DELETE FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id']))
                for category_id in removals['remove_collection_category_ids']:
                    if con.execute('SELECT 1 FROM user_collection_playlists WHERE user_id=? AND category_id=? LIMIT 1',(user['id'],category_id)).fetchone():raise ValueError('انقل قوائم التشغيل إلى تصنيف آخر قبل إزالة التصنيف')
                    con.execute('DELETE FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id']))
                con.commit()
            except ValueError as exc:
                con.rollback();return self.send_json(409,{'error':str(exc)})
            except sqlite3.IntegrityError:
                con.rollback();return self.send_json(409,{'error':'تعذر حفظ التصنيفات بسبب اسم مكرر أو ارتباط غير صالح'})
            finally:con.close()
            return self.send_json(200,{'ok':True,'removed_channels':len(removals['remove_channel_ids']),'removed_playlists':len(removals['remove_playlist_ids']),'removed_collection_playlists':len(removals['remove_collection_playlist_ids']),'removed_videos':totals['removed_videos']+collection_videos_removed,'kept_shared_videos':totals['kept_shared_videos']})
        if path=='/api/me/settings/general':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db()
            try:
                if 'hide_thumbnails' in data:con.execute('UPDATE users SET hide_thumbnails=? WHERE id=?',(1 if data.get('hide_thumbnails') else 0,user['id']))
                if 'watch_later_enabled' in data:con.execute('UPDATE users SET watch_later_enabled=? WHERE id=?',(1 if data.get('watch_later_enabled') else 0,user['id']))
                if 'usage_limits_enabled' in data:con.execute('UPDATE users SET usage_limits_enabled=? WHERE id=?',(1 if data.get('usage_limits_enabled') else 0,user['id']))
                con.commit()
                row=con.execute('SELECT hide_thumbnails,watch_later_enabled,usage_limits_enabled FROM users WHERE id=?',(user['id'],)).fetchone()
            finally:con.close()
            return self.send_json(200,{'ok':True,'hide_thumbnails':bool(row['hide_thumbnails']),'watch_later_enabled':bool(row['watch_later_enabled']),'usage_limits_enabled':bool(row['usage_limits_enabled'])})
        if path=='/api/register':
            name=str(data.get('name','')).strip()[:80]
            account_type=valid_account_type(str(data.get('account_type','user')))
            if account_type=='child':
                return self.send_json(400,{'error':'حساب الطفل يُنشأ من داخل حساب ولي الأمر عبر صفحة الإشراف على الأبناء'})
            username=normalize_username(data.get('username'))
            email=str(data.get('email','')).strip().lower()[:254]
            password=str(data.get('password',''))
            parent_mode=str(data.get('parent_mode','both'))
            if account_type!='parent':parent_mode='user'
            elif parent_mode not in ('both','supervisor'):parent_mode='both'
            if not name or not valid_username(username) or len(password)<10:
                return self.send_json(400,{'error':'أدخل اسمًا واسم مستخدم صالحًا (3–32 حرفًا) وكلمة مرور من 10 أحرف على الأقل'})
            if not valid_email(email):
                return self.send_json(400,{'error':'أدخل بريدًا إلكترونيًا صحيحًا'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                cur=con.execute('INSERT INTO users(email,name,password_hash,created_at,username,account_type,parent_user_id,parent_mode) VALUES(?,?,?,?,?,?,?,?)',(email,name,hash_password(password),int(time.time()),username,account_type,None,parent_mode))
                user_id=cur.lastrowid
                raw=secrets.token_urlsafe(32)
                con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user_id,int(time.time())+30*86400))
                con.commit()
                user=dict(con.execute('SELECT id,email,name,username,account_type,parent_user_id,parent_mode,created_at,hide_thumbnails,watch_later_enabled,usage_limits_enabled FROM users WHERE id=?',(user_id,)).fetchone())
                user.update({'is_supervising':False,'supervisor_id':None,'supervisor_name':None})
                return self.send_json(201,{'user':self.user_payload(con,user)},{'Set-Cookie':self.cookie_login(raw)})
            except sqlite3.IntegrityError:
                con.rollback();return self.send_json(409,{'error':'اسم المستخدم أو البريد الإلكتروني مسجل بالفعل'})
            finally:con.close()
        if path=='/api/login':
            identifier=str(data.get('identifier') or data.get('email') or '').strip();password=str(data.get('password',''))
            if not identifier or not password:return self.send_json(400,{'error':'أدخل بيانات الدخول'})
            con=db()
            try:
                if '@' in identifier:
                    user=con.execute('''SELECT id,email,name,username,account_type,parent_user_id,parent_mode,password_hash,created_at,
                        COALESCE(hide_thumbnails,0) AS hide_thumbnails,COALESCE(watch_later_enabled,0) AS watch_later_enabled,
                        COALESCE(usage_limits_enabled,0) AS usage_limits_enabled FROM users WHERE email=?''',(identifier.lower(),)).fetchone()
                else:
                    user=con.execute('''SELECT id,email,name,username,account_type,parent_user_id,parent_mode,password_hash,created_at,
                        COALESCE(hide_thumbnails,0) AS hide_thumbnails,COALESCE(watch_later_enabled,0) AS watch_later_enabled,
                        COALESCE(usage_limits_enabled,0) AS usage_limits_enabled FROM users WHERE username=? COLLATE NOCASE''',(identifier,)).fetchone()
                if not user or not verify_password(password,user['password_hash']):
                    return self.send_json(401,{'error':'بيانات الدخول غير صحيحة'})
                raw=secrets.token_urlsafe(32)
                con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user['id'],int(time.time())+30*86400))
                con.commit()
                account=dict(user);account.pop('password_hash',None);account.update({'is_supervising':False,'supervisor_id':None,'supervisor_name':None})
                return self.send_json(200,{'user':self.user_payload(con,account)},{'Set-Cookie':self.cookie_login(raw)})
            finally:con.close()
        if path=='/api/logout':
            cookie=SimpleCookie(self.headers.get('Cookie',''));m=cookie.get('session')
            if m:
                try:
                    raw,sig=m.value.split('.',1)
                    if hmac.compare_digest(sig,token_sig(raw)):
                        con=db();con.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),));con.commit();con.close()
                except Exception:pass
            return self.send_json(200,{'ok':True},{'Set-Cookie':self.cookie_logout()})
        if path=='/api/feedback':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لإرسال رأيك'})
            message=str(data.get('message','')).strip()
            if not message or len(message)>1000:return self.send_json(400,{'error':'اكتب رأيًا لا يتجاوز 1000 حرف'})
            con=db();con.execute('INSERT INTO feedback(user_id,message,created_at) VALUES(?,?,?)',(user['id'],message,int(time.time())));con.commit();con.close();return self.send_json(201,{'ok':True})
        if path=='/api/me/switch-account':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'هذا الخيار متاح لولي الأمر'})
            owner_id=self.account_owner_id(user)
            token_hash=self._session_token_hash()
            if not owner_id or not token_hash:return self.send_json(403,{'error':'لا يمكن تبديل الحساب الآن'})
            child_value=data.get('child_id')
            con=db()
            try:
                if child_value in (None,'',0,'0','null'):
                    con.execute('UPDATE sessions SET active_child_id=NULL WHERE token_hash=?',(token_hash,));con.commit()
                    return self.send_json(200,{'ok':True,'child':None})
                try:child_id=int(child_value)
                except (TypeError,ValueError):return self.send_json(400,{'error':'حساب الطفل غير صالح'})
                child=con.execute("SELECT id,name FROM users WHERE id=? AND account_type='child' AND parent_user_id=?",(child_id,owner_id)).fetchone()
                if not child:return self.send_json(404,{'error':'حساب الطفل غير مرتبط بحسابك'})
                con.execute('UPDATE sessions SET active_child_id=? WHERE token_hash=?',(child_id,token_hash));con.commit()
                return self.send_json(200,{'ok':True,'child':dict(child)})
            finally:con.close()
        if path=='/api/me/account/profile':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'بيانات حساب الطفل يعدّلها ولي الأمر'})
            if user.get('is_supervising'):return self.send_json(403,{'error':'بيانات حساب الطفل تُعدّل من قسم الأبناء في حساب ولي الأمر'})
            con=db()
            try:
                row=con.execute('SELECT name,email,username,password_hash FROM users WHERE id=?',(user['id'],)).fetchone()
                name=str(data.get('name',row['name'])).strip()[:80]
                username=normalize_username(data.get('username',row['username']))
                email=str(data.get('email',row['email'])).strip().lower()[:254]
                if not name or not valid_username(username) or not valid_email(email):
                    return self.send_json(400,{'error':'تحقق من الاسم واسم المستخدم والبريد الإلكتروني'})
                identity_changed=username.lower()!=str(row['username'] or '').lower() or email!=str(row['email'] or '').lower()
                if identity_changed and not verify_password(str(data.get('current_password','')),row['password_hash']):
                    return self.send_json(403,{'error':'أدخل كلمة المرور الحالية لتغيير اسم المستخدم أو البريد'})
                con.execute('UPDATE users SET name=?,username=?,email=? WHERE id=?',(name,username,email,user['id']));con.commit()
                updated=dict(con.execute('SELECT id,email,name,username,account_type,parent_user_id,parent_mode,created_at,hide_thumbnails,watch_later_enabled,usage_limits_enabled FROM users WHERE id=?',(user['id'],)).fetchone())
                updated.update({'is_supervising':False,'supervisor_id':None,'supervisor_name':None})
                return self.send_json(200,{'ok':True,'user':self.user_payload(con,updated)})
            except sqlite3.IntegrityError:
                con.rollback();return self.send_json(409,{'error':'اسم المستخدم أو البريد الإلكتروني مستخدم بالفعل'})
            finally:con.close()
        if path=='/api/me/account/password':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'كلمة مرور حساب الطفل يعدّلها ولي الأمر'})
            if user.get('is_supervising'):return self.send_json(403,{'error':'كلمة مرور الطفل تُعدّل من قسم الأبناء'})
            current_password=str(data.get('current_password',''));new_password=str(data.get('new_password',''))
            if len(new_password)<10:return self.send_json(400,{'error':'كلمة المرور الجديدة يجب أن تكون 10 أحرف على الأقل'})
            con=db()
            try:
                row=con.execute('SELECT password_hash FROM users WHERE id=?',(user['id'],)).fetchone()
                if not row or not verify_password(current_password,row['password_hash']):return self.send_json(403,{'error':'كلمة المرور الحالية غير صحيحة'})
                con.execute('UPDATE users SET password_hash=? WHERE id=?',(hash_password(new_password),user['id']))
                token_hash=self._session_token_hash()
                if token_hash:con.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?',(user['id'],token_hash))
                con.commit()
                return self.send_json(200,{'ok':True})
            finally:con.close()
        if path=='/api/me/account/delete':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if self.child_view_locked(user):return self.send_json(403,{'error':'حساب الطفل يديره ولي الأمر'})
            if user.get('is_supervising'):return self.send_json(403,{'error':'أنهِ وضع الإشراف على الطفل أولًا'})
            con=db()
            try:
                owner_id=user['id']
                ids=[owner_id]
                if user.get('account_type')=='parent':
                    ids+= [row['id'] for row in con.execute("SELECT id FROM users WHERE parent_user_id=? AND account_type='child'",(owner_id,)).fetchall()]
                data_tables=('user_playlist_seen','user_videos','user_channels','user_playlists','user_collection_videos','user_collection_playlists','user_categories','user_collection_categories','watch_sessions','watch_time_daily','user_watch_history','user_completed_videos','user_collection_history','user_collection_completed','user_video_sources','user_video_decisions','user_category_orders','user_collection_category_orders','sessions','feedback')
                for uid in ids:
                    for table in data_tables:
                        con.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
                    con.execute('DELETE FROM child_link_codes WHERE parent_user_id=?',(uid,))
                placeholders=','.join('?' for _ in ids)
                con.execute('DELETE FROM users WHERE id IN ('+placeholders+')',ids)
                con.commit()
                return self.send_json(200,{'ok':True},{'Set-Cookie':self.cookie_logout()})
            finally:con.close()
        if path=='/api/me/children':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            owner_id=self.account_owner_id(user)
            if not owner_id:return self.send_json(403,{'error':'إنشاء حساب طفل متاح لولي الأمر فقط'})
            name=str(data.get('name','')).strip()[:80];username=normalize_username(data.get('username'));password=str(data.get('password',''))
            if not name or not valid_username(username) or len(password)<10:
                return self.send_json(400,{'error':'أدخل اسم الطفل واسم مستخدم صالحًا وكلمة مرور من 10 أحرف على الأقل'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                if not con.execute("SELECT 1 FROM users WHERE id=? AND account_type='parent'",(owner_id,)).fetchone():
                    con.rollback();return self.send_json(403,{'error':'حساب ولي الأمر غير متاح'})
                con.execute('INSERT INTO users(email,name,password_hash,created_at,username,account_type,parent_user_id,parent_mode) VALUES(?,?,?,?,?,?,?,?)',(new_child_email(),name,hash_password(password),int(time.time()),username,'child',owner_id,'user'))
                con.commit()
                return self.send_json(201,{'ok':True,'children':self.load_children(con,owner_id)})
            except sqlite3.IntegrityError:
                con.rollback();return self.send_json(409,{'error':'اسم المستخدم مستخدم بالفعل'})
            finally:con.close()
        if path=='/api/me/children/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            owner_id=self.account_owner_id(user)
            if not owner_id:return self.send_json(403,{'error':'تعديل بيانات الطفل متاح لولي الأمر فقط'})
            try:child_id=int(data.get('child_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'حساب الطفل غير صالح'})
            con=db()
            try:
                child=con.execute("SELECT id FROM users WHERE id=? AND account_type='child' AND parent_user_id=?",(child_id,owner_id)).fetchone()
                if not child:return self.send_json(404,{'error':'حساب الطفل غير مرتبط بحسابك'})
                updates=[];values=[]
                if 'name' in data:
                    name=str(data.get('name','')).strip()[:80]
                    if not name:return self.send_json(400,{'error':'اكتب اسم الطفل'})
                    updates.append('name=?');values.append(name)
                if 'username' in data:
                    username=normalize_username(data.get('username'))
                    if not valid_username(username):return self.send_json(400,{'error':'اسم المستخدم غير صالح'})
                    updates.append('username=?');values.append(username)
                if 'password' in data and str(data.get('password','')):
                    password=str(data.get('password'))
                    if len(password)<10:return self.send_json(400,{'error':'كلمة المرور يجب أن تكون 10 أحرف على الأقل'})
                    updates.append('password_hash=?');values.append(hash_password(password))
                if not updates:return self.send_json(400,{'error':'لا توجد تغييرات لحفظها'})
                values.append(child_id)
                con.execute('UPDATE users SET '+','.join(updates)+' WHERE id=?',tuple(values));con.commit()
                return self.send_json(200,{'ok':True,'children':self.load_children(con,owner_id)})
            except sqlite3.IntegrityError:
                con.rollback();return self.send_json(409,{'error':'اسم المستخدم مستخدم بالفعل'})
            finally:con.close()
        if path=='/api/me/categories':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:
                cur=con.execute("INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)",(user['id'],name,int(time.time())))
                con.commit();category_id=cur.lastrowid
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل'})
            finally:con.close()
            return self.send_json(201,{'ok':True,'id':category_id,'name':name})
        if path=='/api/me/categories/order':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            raw_ids=data.get('category_ids')
            if not isinstance(raw_ids,list) or len(raw_ids)>500:return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            try:category_ids=[int(value) for value in raw_ids]
            except (TypeError,ValueError):return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            if any(category_id<1 for category_id in category_ids) or len(category_ids)!=len(set(category_ids)):return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            con=db()
            try:
                current_ids={row['id'] for row in con.execute('SELECT id FROM user_categories WHERE user_id=?',(user['id'],))}
                if set(category_ids)!=current_ids:return self.send_json(409,{'error':'تغيرت قائمة التصنيفات. حدّث الصفحة ثم حاول مجددًا.'})
                con.execute('INSERT INTO user_category_orders(user_id,category_ids,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET category_ids=excluded.category_ids,updated_at=excluded.updated_at',(user['id'],json.dumps(category_ids),int(time.time())))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'category_ids':category_ids})
        if path=='/api/me/categories/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:
                cur=con.execute('UPDATE user_categories SET name=? WHERE id=? AND user_id=?',(name,category_id,user['id']))
                con.commit()
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'يوجد تصنيف آخر بهذا الاسم'})
            finally:con.close()
            if not cur.rowcount:return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            return self.send_json(200,{'ok':True,'name':name})
        if path=='/api/me/categories/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                category=con.execute('SELECT id,name FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
                if not category:return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
                counts=[con.execute('SELECT COUNT(*) FROM '+table+' WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0] for table in ('user_channels','user_playlists','user_videos')]
                if any(counts):return self.send_json(409,{'error':'لا يمكن إزالة تصنيف مرتبط بقنوات أو قوائم تشغيل أو فيديوهات. انقل المصادر أو أزل محتواها أولًا.'})
                con.execute('DELETE FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id']));con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'name':category['name']})
        if path=='/api/me/playlist-library/categories/order':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            raw_ids=data.get('category_ids')
            if not isinstance(raw_ids,list) or len(raw_ids)>500:return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            try:category_ids=[int(value) for value in raw_ids]
            except (TypeError,ValueError):return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            if any(category_id<1 for category_id in category_ids) or len(category_ids)!=len(set(category_ids)):return self.send_json(400,{'error':'ترتيب التصنيفات غير صالح'})
            con=db()
            try:
                current_ids={row['id'] for row in con.execute('SELECT id FROM user_collection_categories WHERE user_id=?',(user['id'],))}
                if set(category_ids)!=current_ids:return self.send_json(409,{'error':'تغيرت قائمة التصنيفات. حدّث الصفحة ثم حاول مجددًا.'})
                con.execute('INSERT INTO user_collection_category_orders(user_id,category_ids,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET category_ids=excluded.category_ids,updated_at=excluded.updated_at',(user['id'],json.dumps(category_ids),int(time.time())))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'category_ids':category_ids})
        if path=='/api/me/playlist-library/categories':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:cur=con.execute('INSERT INTO user_collection_categories(user_id,name,created_at) VALUES(?,?,?)',(user['id'],name,int(time.time())));con.commit()
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل في مكتبة قوائم التشغيل'})
            finally:con.close()
            return self.send_json(201,{'ok':True,'id':cur.lastrowid,'name':name})
        if path=='/api/me/playlist-library/categories/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'التصنيف غير صالح'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:cur=con.execute('UPDATE user_collection_categories SET name=? WHERE id=? AND user_id=?',(name,category_id,user['id']));con.commit()
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'يوجد تصنيف آخر بهذا الاسم في مكتبة قوائم التشغيل'})
            finally:con.close()
            if not cur.rowcount:return self.send_json(404,{'error':'التصنيف غير موجود في مكتبة قوائم التشغيل'})
            return self.send_json(200,{'ok':True,'name':name})
        if path=='/api/me/playlist-library/categories/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'التصنيف غير صالح'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE');category=con.execute('SELECT id,name FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
                if not category:return self.send_json(404,{'error':'التصنيف غير موجود في مكتبة قوائم التشغيل'})
                if con.execute('SELECT 1 FROM user_collection_playlists WHERE user_id=? AND category_id=? LIMIT 1',(user['id'],category_id)).fetchone():return self.send_json(409,{'error':'انقل قوائم التشغيل إلى تصنيف آخر قبل إزالة هذا التصنيف'})
                con.execute('DELETE FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id']));con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'name':category['name']})
        if path=='/api/me/playlist-library/playlists':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=parse_playlist_id(str(data.get('playlist','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'اختر تصنيفًا من مكتبة قوائم التشغيل'})
            con=db();category=con.execute('SELECT id FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone();con.close()
            if not category:return self.send_json(404,{'error':'التصنيف غير موجود في مكتبة قوائم التشغيل'})
            try:title,channel_title,thumbnail_url,items=youtube_playlist_library_feed(playlist_id)
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            if not items:return self.send_json(400,{'error':'لم تحتوِ القائمة على فيديوهات عامة متاحة للاستيراد'})
            now=int(time.time());con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                if not con.execute('SELECT 1 FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone():return self.send_json(404,{'error':'التصنيف غير موجود في مكتبة قوائم التشغيل'})
                cur=con.execute('INSERT INTO user_collection_playlists(user_id,youtube_playlist_id,title,channel_title,thumbnail_url,category_id,added_at,video_count) VALUES(?,?,?,?,?,?,?,?)',(user['id'],playlist_id,title,channel_title,thumbnail_url,category_id,now,len(items)));local_id=cur.lastrowid
                con.executemany('INSERT OR IGNORE INTO user_collection_videos(user_id,playlist_id,video_id,position,title,channel_title,published_at,thumbnail_url,video_url,duration_seconds) VALUES(?,?,?,?,?,?,?,?,?,?)',[(user['id'],local_id,item['video_id'],item['position'],item['title'],item['channel_title'],item['published_at'],item['thumbnail_url'],item['video_url'],item['duration_seconds']) for item in items])
                con.commit()
            except sqlite3.IntegrityError:
                return self.send_json(409,{'error':'قائمة التشغيل مضافة بالفعل إلى مكتبة قوائم التشغيل'})
            finally:con.close()
            return self.send_json(201,{'ok':True,'id':local_id,'title':title,'video_count':len(items)})
        if path=='/api/me/playlist-library/playlists/category':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'));category_id=int(data.get('category_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'اختر قائمة وتصنيفًا صالحين'})
            con=db();playlist=con.execute('SELECT id FROM user_collection_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone();category=con.execute('SELECT id FROM user_collection_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not playlist or not category:con.close();return self.send_json(404,{'error':'القائمة أو التصنيف غير موجود في مكتبتك'})
            con.execute('UPDATE user_collection_playlists SET category_id=? WHERE id=? AND user_id=?',(category_id,playlist_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True})
        if path=='/api/me/playlist-library/playlists/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            con=db();playlist=con.execute('SELECT id,title FROM user_collection_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone()
            if not playlist:con.close();return self.send_json(404,{'error':'قائمة التشغيل غير موجودة في مكتبتك'})
            con.execute('DELETE FROM user_collection_playlists WHERE id=? AND user_id=?',(playlist_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'title':playlist['title']})
        if path=='/api/me/playlist-library/action':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            video_id=str(data.get('video_id','')).strip();action=data.get('action')
            if not video_id or not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):return self.send_json(400,{'error':'الفيديو غير صالح'})
            if action not in ('record','discard'):return self.send_json(400,{'error':'اختر مشاهدة أو إزالة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                video=con.execute('''SELECT v.*,p.title AS playlist_title,p.category_id,c.name AS category_name
                    FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id
                    JOIN user_collection_categories c ON c.id=p.category_id
                    WHERE v.user_id=? AND v.playlist_id=? AND v.video_id=?''',(user['id'],playlist_id,video_id)).fetchone()
                if not video:return self.send_json(404,{'error':'الفيديو لم يعد موجودًا في مكتبة قوائم التشغيل'})
                now=int(time.time())
                if action=='record':
                    con.execute('''INSERT INTO user_collection_completed(user_id,playlist_id,video_id,category_id,category_name,completed_at) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(user_id,playlist_id,video_id) DO UPDATE SET category_id=excluded.category_id,category_name=excluded.category_name,completed_at=excluded.completed_at''',(user['id'],playlist_id,video_id,video['category_id'],video['category_name'],now))
                if action=='record':
                    con.execute('''INSERT INTO user_collection_history(user_id,playlist_id,video_id,playlist_title,category_id,category_name,position,title,channel_title,published_at,watched_at,thumbnail_url,video_url,duration_seconds)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,playlist_id,video_id) DO UPDATE SET playlist_title=excluded.playlist_title,category_id=excluded.category_id,category_name=excluded.category_name,position=excluded.position,title=excluded.title,channel_title=excluded.channel_title,published_at=excluded.published_at,watched_at=excluded.watched_at,thumbnail_url=excluded.thumbnail_url,video_url=excluded.video_url,duration_seconds=excluded.duration_seconds''',
                        (user['id'],playlist_id,video_id,video['playlist_title'],video['category_id'],video['category_name'],video['position'],video['title'],video['channel_title'],video['published_at'],now,video['thumbnail_url'],video['video_url'],video['duration_seconds']))
                con.execute('DELETE FROM user_collection_videos WHERE user_id=? AND playlist_id=? AND video_id=?',(user['id'],playlist_id,video_id));con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'removed':True,'recorded':action=='record'})
        if path=='/api/me/playlist-library/history/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'))
            except (TypeError,ValueError):return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            video_id=str(data.get('video_id','')).strip();con=db();removed=con.execute('DELETE FROM user_collection_history WHERE user_id=? AND playlist_id=? AND video_id=?',(user['id'],playlist_id,video_id)).rowcount;con.close()
            if not removed:return self.send_json(404,{'error':'المشاهدة غير موجودة في السجل'})
            return self.send_json(200,{'ok':True,'removed':True})
        if path=='/api/me/playlist-library/history/clear':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db();count=con.execute('SELECT COUNT(*) FROM user_collection_history WHERE user_id=?',(user['id'],)).fetchone()[0];con.execute('DELETE FROM user_collection_history WHERE user_id=?',(user['id'],));con.commit();con.close();return self.send_json(200,{'ok':True,'count':count})
        if path=='/api/me/playlists':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=parse_playlist_id(str(data.get('playlist','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا'})
            con=db();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            try:title,items,total_count=youtube_playlist_feed(playlist_id)
            except ValueError as exc:con.close();return self.send_json(400,{'error':str(exc)})
            added_at=int(time.time());added_at_iso=datetime.fromtimestamp(added_at,tz=datetime.now().astimezone().tzinfo).isoformat()
            try:
                cur=con.execute('INSERT INTO user_playlists(user_id,youtube_playlist_id,title,category_id,created_at,last_item_count,last_video_published_at) VALUES(?,?,?,?,?,?,?)',(user['id'],playlist_id,title,category_id,added_at,total_count,added_at_iso));local_playlist_id=cur.lastrowid
                con.executemany('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',[(user['id'],local_playlist_id,item['video_id'],added_at) for item in items]);con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'قائمة التشغيل مضافة بالفعل'})
            con.close();return self.send_json(201,{'ok':True,'title':title,'youtube_playlist_id':playlist_id,'baseline_count':total_count})
        if path=='/api/me/playlists/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'));category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر قائمة تشغيل وتصنيفًا صالحين'})
            con=db();playlist=con.execute('SELECT id FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not playlist or not category:con.close();return self.send_json(404,{'error':'قائمة التشغيل أو التصنيف غير موجود في حسابك'})
            con.execute('UPDATE user_playlists SET category_id=? WHERE id=? AND user_id=?',(category_id,playlist_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True})
        if path=='/api/me/playlists/update-many':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            items=data.get('playlists');remove_items=data.get('remove_playlist_ids',[])
            if (not isinstance(items,list) or not isinstance(remove_items,list) or len(items)+len(remove_items)<1 or len(items)+len(remove_items)>500
                or any(not isinstance(item,dict) for item in items)):
                return self.send_json(400,{'error':'قائمة قوائم التشغيل غير صالحة'})
            updates=[]
            try:
                for item in items:
                    playlist_id=int(item.get('playlist_id'));category_id=int(item.get('category_id'))
                    if playlist_id<1 or category_id<1:raise ValueError
                    updates.append((playlist_id,category_id))
                remove_ids=[int(playlist_id) for playlist_id in remove_items]
                if any(playlist_id<1 for playlist_id in remove_ids):raise ValueError
            except (TypeError,ValueError):return self.send_json(400,{'error':'اختر تصنيفًا صالحًا لكل قائمة تشغيل'})
            playlist_ids=[item[0] for item in updates];all_playlist_ids=playlist_ids+remove_ids
            if len(set(all_playlist_ids))!=len(all_playlist_ids):return self.send_json(400,{'error':'القائمة مكررة في طلب الحفظ'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                marks=','.join('?' for _ in all_playlist_ids)
                owned_playlists={row['id'] for row in con.execute('SELECT id FROM user_playlists WHERE user_id=? AND id IN ('+marks+')',(user['id'],*all_playlist_ids))}
                if owned_playlists!=set(all_playlist_ids):return self.send_json(404,{'error':'قائمة واحدة أو أكثر غير موجودة في حسابك'})
                category_ids={item[1] for item in updates}
                if category_ids:
                    marks=','.join('?' for _ in category_ids)
                    owned_categories={row['id'] for row in con.execute('SELECT id FROM user_categories WHERE user_id=? AND id IN ('+marks+')',(user['id'],*category_ids))}
                    if owned_categories!=category_ids:return self.send_json(404,{'error':'تصنيف واحد أو أكثر غير موجود في حسابك'})
                if updates:con.executemany('UPDATE user_playlists SET category_id=? WHERE id=? AND user_id=?',[(category_id,playlist_id,user['id']) for playlist_id,category_id in updates])
                totals={'removed_videos':0,'kept_shared_videos':0}
                for playlist_id in remove_ids:
                    result=remove_followed_playlist(con,user['id'],playlist_id)
                    totals['removed_videos']+=result['removed_videos'];totals['kept_shared_videos']+=result['kept_shared_videos']
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':len(updates),'removed_playlists':len(remove_ids),**totals})
        if path=='/api/me/playlists/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'))
            except Exception:return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE');result=remove_followed_playlist(con,user['id'],playlist_id);con.commit()
            except LookupError as exc:return self.send_json(404,{'error':str(exc)})
            finally:con.close()
            return self.send_json(200,{'ok':True,**result})
        if path=='/api/me/channels':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=parse_channel_id(str(data.get('channel','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا'})
            con=db();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            try:channel_title,last_video_id=youtube_channel_head(channel_id)
            except ValueError as exc:con.close();return self.send_json(400,{'error':str(exc)})
            added_at=int(time.time());added_at_iso=datetime.fromtimestamp(added_at,tz=datetime.now().astimezone().tzinfo).isoformat()
            try:con.execute('INSERT INTO user_channels(user_id,youtube_channel_id,title,category_id,created_at,last_video_id,last_video_published_at) VALUES(?,?,?,?,?,?,?)',(user['id'],channel_id,channel_title,category_id,added_at,last_video_id,added_at_iso));con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'القناة مضافة بالفعل'})
            con.close();return self.send_json(201,{'ok':True,'title':channel_title,'youtube_channel_id':channel_id})
        if path=='/api/me/channels/update-many':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            items=data.get('channels');remove_items=data.get('remove_channel_ids',[])
            if (not isinstance(items,list) or not isinstance(remove_items,list) or len(items)+len(remove_items)<1 or len(items)+len(remove_items)>500
                or any(not isinstance(item,dict) for item in items)):
                return self.send_json(400,{'error':'قائمة القنوات غير صالحة'})
            updates=[]
            try:
                for item in items:
                    channel_id=int(item.get('channel_id'));category_id=int(item.get('category_id'));ignore_shorts=item.get('ignore_shorts')
                    if channel_id<1 or category_id<1 or not isinstance(ignore_shorts,bool):raise ValueError
                    updates.append((channel_id,category_id,int(ignore_shorts)))
                remove_ids=[int(channel_id) for channel_id in remove_items]
                if any(channel_id<1 for channel_id in remove_ids):raise ValueError
            except (TypeError,ValueError):return self.send_json(400,{'error':'اختر تصنيفًا وخيار Shorts صالحين لكل قناة'})
            channel_ids=[item[0] for item in updates];all_channel_ids=channel_ids+remove_ids
            if len(set(all_channel_ids))!=len(all_channel_ids):return self.send_json(400,{'error':'القناة مكررة في طلب الحفظ'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                marks=','.join('?' for _ in all_channel_ids)
                owned_channels={row['id'] for row in con.execute('SELECT id FROM user_channels WHERE user_id=? AND id IN ('+marks+')',(user['id'],*all_channel_ids))}
                if owned_channels!=set(all_channel_ids):return self.send_json(404,{'error':'قناة واحدة أو أكثر غير موجودة في حسابك'})
                category_ids={item[1] for item in updates}
                if category_ids:
                    marks=','.join('?' for _ in category_ids)
                    owned_categories={row['id'] for row in con.execute('SELECT id FROM user_categories WHERE user_id=? AND id IN ('+marks+')',(user['id'],*category_ids))}
                    if owned_categories!=category_ids:return self.send_json(404,{'error':'تصنيف واحد أو أكثر غير موجود في حسابك'})
                if updates:con.executemany('UPDATE user_channels SET category_id=?,ignore_shorts=? WHERE id=? AND user_id=?',[(category_id,ignore_shorts,channel_id,user['id']) for channel_id,category_id,ignore_shorts in updates])
                totals={'removed_videos':0,'kept_shared_videos':0}
                for channel_id in remove_ids:
                    result=remove_channel_from_library(con,user['id'],channel_id)
                    totals['removed_videos']+=result['removed_videos'];totals['kept_shared_videos']+=result['kept_shared_videos']
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':len(updates),'removed_channels':len(remove_ids),**totals})
        if path=='/api/me/channels/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=int(data.get('channel_id'));category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر قناة وتصنيفًا صالحين'})
            con=db();channel=con.execute('SELECT id FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id'])).fetchone();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not channel or not category:con.close();return self.send_json(404,{'error':'القناة أو التصنيف غير موجود في حسابك'})
            ignore_shorts=int(bool(data.get('ignore_shorts'))) if 'ignore_shorts' in data else None
            con.execute('UPDATE user_channels SET category_id=?,ignore_shorts=COALESCE(?,ignore_shorts) WHERE id=? AND user_id=?',(category_id,ignore_shorts,channel_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'ignore_shorts':bool(ignore_shorts) if ignore_shorts is not None else None})
        if path=='/api/me/channels/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=int(data.get('channel_id'))
            except Exception:return self.send_json(400,{'error':'القناة غير صالحة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE');result=remove_channel_from_library(con,user['id'],channel_id);con.commit()
            except LookupError:return self.send_json(404,{'error':'القناة غير موجودة'})
            finally:con.close()
            return self.send_json(200,{'ok':True,**result})
        if path=='/api/me/watch-time':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            session_id=data.get('session_id');video_id=data.get('video_id');seconds=data.get('seconds');watch_date=data.get('watch_date');library_type=data.get('library_type','video');collection_playlist_id=None
            raw_duration=data.get('duration_seconds')
            duration=int(raw_duration) if isinstance(raw_duration,(int,float)) and not isinstance(raw_duration,bool) and 0<=raw_duration<=172800 else None
            if (not isinstance(session_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',session_id)
                or not isinstance(video_id,str) or not 1<=len(video_id)<=100
                or type(seconds) is not int or not 0<=seconds<=86400 or library_type not in ('video','collection')):
                return self.send_json(400,{'error':'بيانات وقت المشاهدة غير صالحة'})
            if library_type=='collection':
                try:collection_playlist_id=int(data.get('playlist_id'))
                except (TypeError,ValueError):return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            now=int(time.time())
            try:watch_date=date.fromisoformat(watch_date) if isinstance(watch_date,str) else date.fromtimestamp(now)
            except ValueError:return self.send_json(400,{'error':'تاريخ المشاهدة غير صالح'})
            if watch_date.isoformat()!=data.get('watch_date',watch_date.isoformat()):return self.send_json(400,{'error':'تاريخ المشاهدة غير صالح'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                if library_type=='collection':
                    video=con.execute('''SELECT v.title,v.channel_title,p.category_id FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id
                        WHERE v.user_id=? AND v.playlist_id=? AND v.video_id=?''',(user['id'],collection_playlist_id,video_id)).fetchone()
                    category_id=None;collection_category_id=video['category_id'] if video else None
                    video_title=(video['title'] if video else '') or '';channel_id='';channel_title=(video['channel_title'] if video else '') or ''
                else:
                    video=con.execute('SELECT title,channel_id,channel_title,category_id FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id)).fetchone()
                    category_id=video['category_id'] if video else None;collection_category_id=None
                    video_title=(video['title'] if video else '') or '';channel_id=(video['channel_id'] if video else '') or '';channel_title=(video['channel_title'] if video else '') or ''
                if not video:return self.send_json(404,{'error':'الفيديو غير موجود'})
                if duration:
                    if library_type=='collection':
                        con.execute('UPDATE user_collection_videos SET duration_seconds=? WHERE user_id=? AND playlist_id=? AND video_id=? AND (duration_seconds IS NULL OR duration_seconds<=0)',(duration,user['id'],collection_playlist_id,video_id))
                    else:
                        con.execute('UPDATE user_videos SET duration_seconds=? WHERE user_id=? AND video_id=? AND (duration_seconds IS NULL OR duration_seconds<=0)',(duration,user['id'],video_id))
                previous=con.execute('SELECT video_id,seconds,started_at,library_type,collection_playlist_id FROM watch_sessions WHERE user_id=? AND session_id=?',(user['id'],session_id)).fetchone()
                if previous and (previous['video_id']!=video_id or previous['library_type']!=library_type or (library_type=='collection' and previous['collection_playlist_id']!=collection_playlist_id)):return self.send_json(409,{'error':'جلسة المشاهدة مرتبطة بمحتوى آخر'})
                # A first heartbeat may be delayed by a temporary network failure.
                started_at=previous['started_at'] if previous else now-seconds
                if seconds>max(0,now-started_at)+5:return self.send_json(400,{'error':'وقت المشاهدة يتجاوز مدة الجلسة'})
                previous_seconds=previous['seconds'] if previous else 0
                seconds=max(seconds,previous_seconds);delta=seconds-previous_seconds
                con.execute('INSERT INTO watch_sessions(user_id,session_id,video_id,category_id,seconds,started_at,updated_at,library_type,collection_category_id,collection_playlist_id) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET seconds=MAX(watch_sessions.seconds,excluded.seconds),updated_at=excluded.updated_at',(user['id'],session_id,video_id,category_id,seconds,started_at,now,library_type,collection_category_id,collection_playlist_id))
                if delta>0:
                    con.execute('INSERT INTO watch_time_daily(user_id,session_id,video_id,category_id,watch_date,seconds,library_type,collection_category_id,collection_playlist_id,channel_id,channel_title,video_title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,session_id,watch_date) DO UPDATE SET seconds=watch_time_daily.seconds+excluded.seconds,video_id=excluded.video_id,category_id=excluded.category_id,library_type=excluded.library_type,collection_category_id=excluded.collection_category_id,collection_playlist_id=excluded.collection_playlist_id,channel_id=excluded.channel_id,channel_title=excluded.channel_title,video_title=excluded.video_title',(user['id'],session_id,video_id,category_id,watch_date.isoformat(),delta,library_type,collection_category_id,collection_playlist_id,channel_id,channel_title,video_title))
                    if channel_title:
                        con.execute("INSERT INTO video_identity(video_id,title,channel_title,fetched_at) VALUES(?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE video_identity.title END,channel_title=CASE WHEN excluded.channel_title<>'' THEN excluded.channel_title ELSE video_identity.channel_title END",(video_id,video_title,channel_title,now))
                con.commit()
                limit_minutes=0;watched_today=0;category_name=None;limit_category_id=None
                if library_type=='video' and category_id and user.get('usage_limits_enabled'):
                    category=con.execute('SELECT name,COALESCE(daily_limit_minutes,0) AS daily_limit_minutes FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
                    if category:
                        category_name=category['name'];limit_minutes=int(category['daily_limit_minutes']);limit_category_id=category_id
                        watched_today=con.execute("SELECT COALESCE(SUM(seconds),0) FROM watch_time_daily WHERE user_id=? AND library_type='video' AND category_id=? AND watch_date=?",(user['id'],category_id,watch_date.isoformat())).fetchone()[0]
            finally:con.close()
            return self.send_json(200,{'ok':True,'seconds':seconds,'category_id':limit_category_id,'category_name':category_name,'limit_minutes':limit_minutes,'watched_today':watched_today})
        if path=='/api/me/videos/watched':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_id=str(data.get('video_id','')).strip();watched=1 if data.get('watched') else 0
            if not video_id:return self.send_json(400,{'error':'الفيديو غير صالح'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE');cur=con.execute('UPDATE user_videos SET watched=? WHERE user_id=? AND video_id=?',(watched,user['id'],video_id))
                if cur.rowcount and watched:
                    con.execute('INSERT INTO user_completed_videos(user_id,video_id,title,channel_title,category_id,category_name,completed_at) SELECT v.user_id,v.video_id,v.title,v.channel_title,v.category_id,c.name,? FROM user_videos v JOIN user_categories c ON c.id=v.category_id WHERE v.user_id=? AND v.video_id=? ON CONFLICT(user_id,video_id) DO UPDATE SET completed_at=excluded.completed_at',(int(time.time()),user['id'],video_id))
                elif cur.rowcount:
                    con.execute('DELETE FROM user_watch_history WHERE user_id=? AND video_id=?',(user['id'],video_id))
                    con.execute('DELETE FROM user_completed_videos WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.commit()
            finally:con.close()
            if not cur.rowcount:return self.send_json(404,{'error':'الفيديو غير موجود'})
            return self.send_json(200,{'ok':True,'watched':bool(watched)})
        if path=='/api/me/videos/remove-watched':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_ids=data.get('video_ids')
            if (not isinstance(video_ids,list) or not 1<=len(video_ids)<=100
                or any(not isinstance(value,str) or not value or len(value)>100 for value in video_ids)
                or len(set(video_ids))!=len(video_ids)):
                return self.send_json(400,{'error':'اختر من 1 إلى 100 فيديو مشاهدة لإزالتها'})
            placeholders=','.join('?' for _ in video_ids)
            con=db();rows=con.execute('SELECT v.video_id FROM user_videos v WHERE v.user_id=? AND (v.watched=1 OR EXISTS(SELECT 1 FROM user_watch_history h WHERE h.user_id=v.user_id AND h.video_id=v.video_id) OR EXISTS(SELECT 1 FROM user_completed_videos c WHERE c.user_id=v.user_id AND c.video_id=v.video_id)) AND v.video_id IN ('+placeholders+')',(user['id'],*video_ids)).fetchall();con.close()
            if len(rows)!=len(video_ids):
                return self.send_json(409,{'error':'تغيّرت الفيديوهات المحددة. حدّث المكتبة وراجع اختيارك قبل الإزالة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                # Recheck watched status inside the deletion transaction.
                rows=con.execute('SELECT v.video_id FROM user_videos v WHERE v.user_id=? AND (v.watched=1 OR EXISTS(SELECT 1 FROM user_watch_history h WHERE h.user_id=v.user_id AND h.video_id=v.video_id) OR EXISTS(SELECT 1 FROM user_completed_videos c WHERE c.user_id=v.user_id AND c.video_id=v.video_id)) AND v.video_id IN ('+placeholders+')',(user['id'],*video_ids)).fetchall()
                if len(rows)!=len(video_ids):return self.send_json(409,{'error':'تغيّرت الفيديوهات المحددة. حدّث المكتبة وحاول مجددًا'})
                con.execute('INSERT INTO user_video_decisions(user_id,video_id,action,decided_at) SELECT user_id,video_id,\'record\',? FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+') ON CONFLICT(user_id,video_id) DO UPDATE SET action=excluded.action,decided_at=excluded.decided_at',(int(time.time()),user['id'],*video_ids))
                con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids))
                removed=con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).rowcount
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':removed})
        if path=='/api/me/videos/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_id=str(data.get('video_id','')).strip();action=data.get('action')
            if not video_id or len(video_id)>100:return self.send_json(400,{'error':'الفيديو غير صالح'})
            if action not in ('record','discard'):
                return self.send_json(400,{'error':'اختر مشاهدة أو إزالة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                video=con.execute('SELECT v.*,c.name AS category_name FROM user_videos v JOIN user_categories c ON c.id=v.category_id WHERE v.user_id=? AND v.video_id=?',(user['id'],video_id)).fetchone()
                if not video:return self.send_json(404,{'error':'الفيديو غير موجود في مكتبتك'})
                now=int(time.time())
                if action=='record':
                    con.execute('INSERT INTO user_completed_videos(user_id,video_id,title,channel_title,category_id,category_name,completed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,video_id) DO UPDATE SET title=excluded.title,channel_title=excluded.channel_title,category_id=excluded.category_id,category_name=excluded.category_name,completed_at=excluded.completed_at',(user['id'],video_id,video['title'],video['channel_title'],video['category_id'],video['category_name'],now))
                else:
                    con.execute('DELETE FROM user_completed_videos WHERE user_id=? AND video_id=?',(user['id'],video_id))
                if action=='record':
                    con.execute('INSERT INTO user_watch_history(user_id,video_id,title,channel_title,category_id,category_name,published_at,watched_at,thumbnail_url,video_url,duration_seconds,completed) VALUES(?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(user_id,video_id) DO UPDATE SET title=excluded.title,channel_title=excluded.channel_title,category_id=excluded.category_id,category_name=excluded.category_name,published_at=excluded.published_at,watched_at=excluded.watched_at,thumbnail_url=excluded.thumbnail_url,video_url=excluded.video_url,duration_seconds=excluded.duration_seconds,completed=1',(user['id'],video_id,video['title'],video['channel_title'],video['category_id'],video['category_name'],video['published_at'],now,video['thumbnail_url'],video['video_url'],video['duration_seconds']))
                else:
                    con.execute('DELETE FROM user_watch_history WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.execute('UPDATE user_videos SET watched=0 WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.execute('INSERT INTO user_video_decisions(user_id,video_id,action,decided_at) VALUES(?,?,?,?) ON CONFLICT(user_id,video_id) DO UPDATE SET action=excluded.action,decided_at=excluded.decided_at',(user['id'],video_id,action,now))
                con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'removed':True,'recorded':action=='record'})
        if path=='/api/me/videos/watch-later':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            if not user.get('watch_later_enabled'):return self.send_json(400,{'error':'إعداد «المشاهدة لاحقًا» غير مفعّل'})
            video_id=str(data.get('video_id','')).strip();flag=1 if data.get('watch_later') else 0
            if not video_id or len(video_id)>100:return self.send_json(400,{'error':'الفيديو غير صالح'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                if not con.execute('SELECT 1 FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id)).fetchone():
                    return self.send_json(404,{'error':'الفيديو غير موجود في مكتبتك'})
                con.execute('UPDATE user_videos SET watch_later=? WHERE user_id=? AND video_id=?',(flag,user['id'],video_id))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'watch_later':bool(flag)})
        if path=='/api/me/watch-history/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_id=str(data.get('video_id','')).strip()
            if not video_id or len(video_id)>100:return self.send_json(400,{'error':'الفيديو غير صالح'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                removed=con.execute('DELETE FROM user_watch_history WHERE user_id=? AND video_id=?',(user['id'],video_id)).rowcount
                if removed:con.execute('UPDATE user_videos SET watched=0 WHERE user_id=? AND video_id=?',(user['id'],video_id))
                con.commit()
            finally:con.close()
            if not removed:return self.send_json(404,{'error':'المشاهدة غير موجودة في السجل'})
            return self.send_json(200,{'ok':True,'removed':True})
        if path=='/api/me/watch-history/clear':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                count=con.execute('SELECT COUNT(*) FROM user_watch_history WHERE user_id=?',(user['id'],)).fetchone()[0]
                con.execute('UPDATE user_videos SET watched=0 WHERE user_id=? AND video_id IN (SELECT video_id FROM user_watch_history WHERE user_id=?)',(user['id'],user['id']))
                con.execute('DELETE FROM user_watch_history WHERE user_id=?',(user['id'],))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':count})
        if path=='/api/me/sync':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            now=int(time.time())
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                row=con.execute('SELECT last_sync_at FROM user_sync_state WHERE user_id=?',(user['id'],)).fetchone()
                last_sync_at=int(row['last_sync_at']) if row else 0
                if last_sync_at and now-last_sync_at<SYNC_MIN_INTERVAL:
                    con.rollback()
                    return self.send_json(200,{'added':[],'count':0,'errors':[],'skipped':True,'last_sync_at':last_sync_at,'next_sync_at':last_sync_at+SYNC_MIN_INTERVAL})
                con.execute('INSERT INTO user_sync_state(user_id,last_sync_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_sync_at=excluded.last_sync_at',(user['id'],now))
                con.commit()
            finally:con.close()
            con=db();channels=con.execute('SELECT ch.*,cat.name AS category FROM user_channels ch JOIN user_categories cat ON cat.id=ch.category_id WHERE ch.user_id=?',(user['id'],)).fetchall();playlists=con.execute('SELECT pl.*,cat.name AS category FROM user_playlists pl JOIN user_categories cat ON cat.id=pl.category_id WHERE pl.user_id=?',(user['id'],)).fetchall();con.close()
            added=[];errors=[]
            for ch in channels:
                try:
                    cursor=ch['last_video_id']
                    if not cursor:
                        local=db();known=local.execute('SELECT v.video_id FROM user_video_sources s JOIN user_videos v ON v.user_id=s.user_id AND v.video_id=s.video_id WHERE s.user_id=? AND s.source_type=\'channel\' AND s.source_id=? ORDER BY v.published_at DESC LIMIT 1',(user['id'],str(ch['id']))).fetchone();local.close()
                        cursor=known['video_id'] if known else ''
                    checkpoint=ch['last_video_published_at'] or datetime.fromtimestamp(ch['created_at'],tz=datetime.now().astimezone().tzinfo).isoformat()
                    since_timestamp=timestamp_epoch(checkpoint) or ch['created_at']
                    _,feed_items,next_cursor=youtube_feed(ch['youtube_channel_id'],since_timestamp,cursor)
                    latest_published=checkpoint
                    short_ids=youtube_channel_short_ids(ch['youtube_channel_id'],(video['video_id'] for video in feed_items)) if ch['ignore_shorts'] and feed_items else set()
                    for video in feed_items:
                        published_timestamp=timestamp_epoch(video['published_at'])
                        if published_timestamp is None:continue
                        if published_timestamp<=ch['created_at']:continue
                        latest_published=later_timestamp(latest_published,video['published_at'])
                        if video['video_id'] in short_ids:continue
                        local=db();excluded=local.execute('SELECT 1 FROM user_video_decisions WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if excluded:local.close();continue
                        exists=local.execute('SELECT duration_seconds FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if exists:
                            synced_duration=normalize_duration(video.get('duration_seconds'))
                            if synced_duration and not exists['duration_seconds']:
                                local.execute('UPDATE user_videos SET duration_seconds=? WHERE user_id=? AND video_id=? AND (duration_seconds IS NULL OR duration_seconds<=0)',(synced_duration,user['id'],video['video_id']));local.commit()
                            local.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'channel',str(ch['id']),int(time.time())));local.commit();local.close();continue
                        local.close()
                        duration=ensure_video_duration(video)
                        local=db();local.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,created_at,duration_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],ch['youtube_channel_id'],video['channel_title'],ch['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],int(time.time()),duration));local.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'channel',str(ch['id']),int(time.time())));local.commit();local.close();added.append({'title':video['title'],'category':ch['category']})
                    if (next_cursor and next_cursor!=cursor) or latest_published!=checkpoint:
                        local=db();local.execute('UPDATE user_channels SET last_video_id=?,last_video_published_at=? WHERE id=? AND user_id=?',(next_cursor or cursor,latest_published,ch['id'],user['id']));local.commit();local.close()
                except Exception as exc:errors.append({'channel':ch['title'],'error':sync_source_error(exc)})
            for playlist in playlists:
                try:
                    # Playlist order/count and upload dates do not identify new membership.
                    # Reconcile the full membership against the baseline captured on add.
                    con=db()
                    seen_video_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_playlist_seen WHERE user_id=? AND playlist_id=?',(user['id'],playlist['id']))}
                    existing_video_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_videos WHERE user_id=?',(user['id'],))}
                    known_video_ids=seen_video_ids | existing_video_ids
                    con.close()
                    _,feed_items,total_count=youtube_playlist_feed(playlist['youtube_playlist_id'],1,True,known_video_ids=known_video_ids)
                    checkpoint=playlist['last_video_published_at'] or datetime.fromtimestamp(playlist['created_at'],tz=datetime.now().astimezone().tzinfo).isoformat()
                    latest_published=checkpoint
                    for video in feed_items:
                        observed_at=int(time.time())
                        latest_published=later_timestamp(latest_published,video['published_at'])
                        con=db();excluded=con.execute('SELECT 1 FROM user_video_decisions WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if excluded:
                            con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.commit();con.close();continue
                        con.close()
                        # Baseline and removed videos need no per-item DB connection.
                        if video['video_id'] in seen_video_ids and video['video_id'] not in existing_video_ids:continue
                        con=db();seen=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND playlist_id=? AND video_id=?',(user['id'],playlist['id'],video['video_id'])).fetchone();exists=con.execute('SELECT duration_seconds FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if seen:
                            synced_duration=normalize_duration(video.get('duration_seconds'))
                            if exists and synced_duration and not exists['duration_seconds']:
                                con.execute('UPDATE user_videos SET duration_seconds=? WHERE user_id=? AND video_id=? AND (duration_seconds IS NULL OR duration_seconds<=0)',(synced_duration,user['id'],video['video_id']))
                            if exists:con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),int(time.time())));con.commit()
                            con.close();continue
                        if exists:
                            synced_duration=normalize_duration(video.get('duration_seconds'))
                            if synced_duration and not exists['duration_seconds']:
                                con.execute('UPDATE user_videos SET duration_seconds=? WHERE user_id=? AND video_id=? AND (duration_seconds IS NULL OR duration_seconds<=0)',(synced_duration,user['id'],video['video_id']))
                            con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();continue
                        con.close()
                        duration=ensure_video_duration(video)
                        con=db();con.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,created_at,duration_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],video['channel_id'] or playlist['youtube_playlist_id'],video['channel_title'] or playlist['title'],playlist['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],observed_at,duration));con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();added.append({'title':video['title'],'category':playlist['category']})
                    con=db();con.execute('UPDATE user_playlists SET last_item_count=?,last_video_published_at=? WHERE id=? AND user_id=?',(total_count,latest_published,playlist['id'],user['id']));con.commit();con.close()
                except Exception as exc:errors.append({'channel':playlist['title'],'error':sync_source_error(exc)})
            try:
                con=db();con.execute('INSERT INTO user_sync_state(user_id,last_sync_at,last_sync_added,last_sync_errors) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_sync_added=excluded.last_sync_added,last_sync_errors=excluded.last_sync_errors',(user['id'],now,len(added),json.dumps(errors,ensure_ascii=False)));con.commit();con.close()
            except Exception:pass
            return self.send_json(200,{'added':added,'count':len(added),'errors':errors,'skipped':False,'last_sync_at':now,'next_sync_at':now+SYNC_MIN_INTERVAL})
        return self.send_json(404,{'error':'غير موجود'})

if __name__=='__main__':
    db().close()
    start_identity_backfill()
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
