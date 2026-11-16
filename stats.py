# Statistics aggregation engine for the video and playlist libraries.
# Pure helpers: the caller owns the SQLite connection and session checks.
import re
from datetime import datetime, date, timedelta

EPOCH = datetime(1970, 1, 1)
PERIOD_LABELS = {
    '7d': 'آخر ٧ أيام',
    '30d': 'آخر ٣٠ يومًا',
    '90d': 'آخر ٩٠ يومًا',
    'month': 'هذا الشهر',
    'year': 'هذا العام',
    'all': 'كل الوقت',
}


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().lower()


def day_epoch(day):
    return int((datetime(day.year, day.month, day.day) - EPOCH).total_seconds())


def local_date(timestamp, offset):
    return (EPOCH + timedelta(seconds=int(timestamp or 0) + int(offset or 0))).date()


def local_day(timestamp, offset):
    return local_date(timestamp, offset).isoformat()


def period(period_key, today, offset):
    key = str(period_key or '30d').strip().lower()
    if key not in PERIOD_LABELS:
        key = '30d'
    if not isinstance(today, date):
        today = date.today()
    if key == 'all':
        start = date(1970, 1, 1)
    elif key == 'year':
        start = date(today.year, 1, 1)
    elif key == 'month':
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=int(key[:-1]) - 1)
    if start > today:
        start = today
    days = (today - start).days + 1
    previous = None
    if key != 'all':
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
        previous = {
            'start': prev_start.isoformat(),
            'end': prev_end.isoformat(),
            'start_ts': day_epoch(prev_start) - offset,
            'end_ts': day_epoch(prev_end + timedelta(days=1)) - offset - 1,
        }
    return {
        'key': key,
        'label': PERIOD_LABELS[key],
        'start': start.isoformat(),
        'end': today.isoformat(),
        'days': days,
        'start_ts': day_epoch(start) - offset,
        'end_ts': day_epoch(today + timedelta(days=1)) - offset - 1,
        'prev': previous,
    }


def public_period(value):
    previous = value.get('prev') or {}
    return {
        'key': value['key'],
        'label': value['label'],
        'start': value['start'],
        'end': value['end'],
        'days': value['days'],
        'prev_start': previous.get('start'),
        'prev_end': previous.get('end'),
    }


def calendar_payload(con, uid, library, month_key, today, offset, created_at):
    try:
        year, month = [int(part) for part in str(month_key or '').split('-')[:2]]
        first = date(year, month, 1)
    except (TypeError, ValueError):
        first = today.replace(day=1)
    current_month = today.replace(day=1)
    if first > current_month:
        first = current_month
    earliest = local_date(created_at, offset).replace(day=1) if created_at else first
    if first < earliest:
        first = earliest
    next_first = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    last = min(next_first - timedelta(days=1), today)
    days = {}
    cursor = first
    while cursor <= last:
        days[cursor.isoformat()] = {'date': cursor.isoformat(), 'seconds': 0, 'recorded': 0}
        cursor += timedelta(days=1)
    table = 'user_collection_completed' if library == 'collection' else 'user_completed_videos'
    condition = "library_type='collection'" if library == 'collection' else "library_type='video'"
    start_ts = day_epoch(first) - offset
    end_ts = day_epoch(last + timedelta(days=1)) - offset - 1
    for row in con.execute("SELECT watch_date,SUM(seconds) AS seconds FROM watch_time_daily WHERE user_id=? AND " + condition + " AND watch_date BETWEEN ? AND ? GROUP BY watch_date", (uid, first.isoformat(), last.isoformat())):
        if row['watch_date'] in days:
            days[row['watch_date']]['seconds'] = int(row['seconds'] or 0)
    for row in con.execute('SELECT completed_at FROM ' + table + ' WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, start_ts, end_ts)):
        key = local_day(row['completed_at'], offset)
        if key in days:
            days[key]['recorded'] += 1
    return {
        'month': first.strftime('%Y-%m'),
        'start': first.isoformat(),
        'end': last.isoformat(),
        'today': today.isoformat(),
        'earliest': earliest.strftime('%Y-%m'),
        'days': list(days.values()),
    }


def fill_trend(daily, start, end):
    """Return a continuous daily series so the chart keeps an honest time axis."""
    if not daily:
        return []
    first_data = date.fromisoformat(min(daily))
    first = first_data if start.year <= 1970 else start
    if first > end:
        first = end
    if (end - first).days > 730:
        first = end - timedelta(days=730)
    cursor = first
    while cursor <= end:
        daily.setdefault(cursor.isoformat(), {'date': cursor.isoformat(), 'seconds': 0, 'videos': 0, 'recorded': 0})
        cursor += timedelta(days=1)
    return [daily[key] for key in sorted(daily)]


def lean_entry(entry):
    if not entry:
        return None
    return {
        'key': entry.get('key'),
        'id': entry.get('id'),
        'playlist_id': entry.get('playlist_id'),
        'name': entry.get('name') or entry.get('title'),
        'seconds': int(entry.get('seconds') or 0),
        'videos': int(entry.get('videos') or 0),
        'recorded': int(entry.get('recorded') or 0),
        'library_videos': int(entry.get('library_videos') or entry.get('total_videos') or 0),
    }

def video_stats(con, uid, created_at, period_obj, offset, calendar_month, today):
    start = date.fromisoformat(period_obj['start'])
    end = date.fromisoformat(period_obj['end'])
    previous = period_obj.get('prev')

    categories = {}
    for row in con.execute('SELECT id,name,daily_limit_minutes FROM user_categories WHERE user_id=?', (uid,)):
        categories[row['id']] = {'name': row['name'], 'daily_limit_minutes': int(row['daily_limit_minutes'] or 0)}

    def category_name(cid):
        return (categories.get(cid) or {}).get('name') or 'بدون تصنيف'

    videos = {}
    for row in con.execute('SELECT video_id,title,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,duration_seconds FROM user_videos WHERE user_id=?', (uid,)):
        videos[row['video_id']] = dict(row)

    known = {}
    for row in con.execute('SELECT youtube_channel_id,title,category_id FROM user_channels WHERE user_id=?', (uid,)):
        known[row['youtube_channel_id']] = {'id': row['youtube_channel_id'], 'title': row['title'], 'category_id': row['category_id']}
    known_by_title = {}
    for entry in known.values():
        title_key = norm(entry['title'])
        if title_key:
            known_by_title[title_key] = entry

    completed = {}
    for row in con.execute('SELECT video_id,title,channel_title,category_id,completed_at FROM user_completed_videos WHERE user_id=?', (uid,)):
        completed[row['video_id']] = dict(row)

    history = {}
    for row in con.execute('SELECT video_id,title,channel_title,category_id,duration_seconds FROM user_watch_history WHERE user_id=?', (uid,)):
        history[row['video_id']] = dict(row)

    identity = {}
    for row in con.execute("SELECT video_id,title,channel_title FROM video_identity WHERE channel_title<>''"):
        identity[row['video_id']] = dict(row)

    daily_channels = {}
    for row in con.execute("SELECT video_id,watch_date,channel_id,channel_title,video_title FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? AND (COALESCE(channel_title,'')<>'' OR COALESCE(channel_id,'')<>'') ORDER BY watch_date", (uid, start.isoformat(), end.isoformat())):
        daily_channels[row['video_id']] = {'channel_id': row['channel_id'] or '', 'channel_title': row['channel_title'] or '', 'title': row['video_title'] or ''}

    daily_categories = {}
    for row in con.execute("SELECT video_id,category_id,SUM(seconds) AS seconds FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? AND category_id IS NOT NULL GROUP BY video_id,category_id", (uid, start.isoformat(), end.isoformat())):
        bucket = daily_categories.setdefault(row['video_id'], {})
        bucket[row['category_id']] = bucket.get(row['category_id'], 0) + int(row['seconds'] or 0)

    def resolve(video_id):
        meta = videos.get(video_id) or {}
        done = completed.get(video_id) or {}
        hist = history.get(video_id) or {}
        daily = daily_channels.get(video_id) or {}
        ident = identity.get(video_id) or {}
        youtube_id = meta.get('channel_id') or daily.get('channel_id') or ''
        raw_channel = (meta.get('channel_title') or done.get('channel_title') or hist.get('channel_title')
                       or daily.get('channel_title') or ident.get('channel_title') or '')
        channel = known.get(youtube_id) or known_by_title.get(norm(raw_channel)) or {}
        channel_title = channel.get('title') or raw_channel or 'قناة غير معروفة'
        if channel:
            channel_key = 'id:' + channel['id']
        elif raw_channel:
            channel_key = 't:' + norm(raw_channel)
        else:
            channel_key = 'unknown'
        cid = meta.get('category_id')
        if cid is None:
            cid = done.get('category_id')
        if cid is None:
            cid = hist.get('category_id')
        if cid is None:
            bucket = daily_categories.get(video_id)
            if bucket:
                cid = max(bucket, key=bucket.get)
        duration = meta.get('duration_seconds')
        if duration is None:
            duration = hist.get('duration_seconds')
        return {
            'video_id': video_id,
            'title': meta.get('title') or done.get('title') or hist.get('title') or daily.get('title') or ident.get('title') or 'فيديو',
            'channel_key': channel_key,
            'channel_name': channel_title,
            'channel_category_id': channel.get('category_id'),
            'category_id': cid,
            'category_name': category_name(cid),
            'duration_seconds': int(duration) if duration not in (None, '') else None,
            'thumbnail_url': meta.get('thumbnail_url') or 'https://i.ytimg.com/vi/' + video_id + '/hqdefault.jpg',
            'video_url': meta.get('video_url') or 'https://www.youtube.com/watch?v=' + video_id,
            'published_at': meta.get('published_at') or '',
        }

    actual = {}
    for row in con.execute("SELECT video_id,SUM(seconds) AS seconds,MIN(watch_date) AS first_date,MAX(watch_date) AS last_date,COUNT(DISTINCT watch_date) AS active_days FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? AND seconds>0 GROUP BY video_id", (uid, start.isoformat(), end.isoformat())):
        actual[row['video_id']] = dict(row)

    recorded = {}
    for row in con.execute('SELECT video_id,completed_at,category_id FROM user_completed_videos WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, period_obj['start_ts'], period_obj['end_ts'])):
        recorded[row['video_id']] = dict(row)

    previous_seconds = previous_videos = previous_recorded = 0
    if previous:
        row = con.execute("SELECT COALESCE(SUM(seconds),0) AS value FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ?", (uid, previous['start'], previous['end'])).fetchone()
        previous_seconds = int(row['value'] or 0)
        row = con.execute("SELECT COUNT(DISTINCT video_id) AS value FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? AND seconds>0", (uid, previous['start'], previous['end'])).fetchone()
        previous_videos = int(row['value'] or 0)
        row = con.execute('SELECT COUNT(*) AS value FROM user_completed_videos WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, previous['start_ts'], previous['end_ts'])).fetchone()
        previous_recorded = int(row['value'] or 0)

    rows = {}
    for video_id, entry in actual.items():
        item = resolve(video_id)
        item.update(seconds=int(entry['seconds'] or 0), first_date=entry['first_date'] or '', last_date=entry['last_date'] or '', active_days=int(entry['active_days'] or 0), recorded_at=None, recorded_date='')
        rows[video_id] = item
    for video_id, entry in recorded.items():
        item = rows.get(video_id)
        if item is None:
            item = resolve(video_id)
            item.update(seconds=0, first_date='', last_date='', active_days=0)
            rows[video_id] = item
        item['recorded_at'] = int(entry['completed_at'] or 0)
        item['recorded_date'] = local_day(entry['completed_at'], offset)
        if entry.get('category_id') is not None and item.get('category_id') is None:
            item['category_id'] = entry['category_id']
            item['category_name'] = category_name(entry['category_id'])
    for item in rows.values():
        item['last_activity'] = max(item.get('last_date') or '', item.get('recorded_date') or '')

    total_seconds = sum(item['seconds'] for item in rows.values())
    actual_count = len(actual)
    recorded_count = len(recorded)
    durations = [item for item in rows.values() if item['seconds'] > 0 and item['duration_seconds'] and item['duration_seconds'] > 0]
    completed_by_watch = sum(1 for item in durations if item['seconds'] >= 0.85 * item['duration_seconds'])
    completion_rate = round(completed_by_watch * 100 / len(durations)) if durations else 0
    average_seconds = int(total_seconds / actual_count) if actual_count else 0

    channels = {}
    for item in rows.values():
        key = item['channel_key']
        entry = channels.get(key)
        if entry is None:
            entry = {'key': key, 'name': item['channel_name'], 'category_id': item['channel_category_id'] if item['channel_category_id'] is not None else item['category_id'], 'known_category_id': item['channel_category_id'], 'seconds': 0, 'videos': 0, 'recorded': 0, 'active_days': 0, 'last_date': '', 'first_date': '', 'library_videos': 0, 'library_watched': 0, 'weights': {}}
            channels[key] = entry
        entry['seconds'] += item['seconds']
        if item['seconds'] > 0:
            entry['videos'] += 1
        if item['recorded_at']:
            entry['recorded'] += 1
        entry['active_days'] += item['active_days']
        if item['last_activity'] > entry['last_date']:
            entry['last_date'] = item['last_activity']
        if item['first_date'] and (not entry['first_date'] or item['first_date'] < entry['first_date']):
            entry['first_date'] = item['first_date']
        if item.get('category_id') is not None:
            entry['weights'][item['category_id']] = entry['weights'].get(item['category_id'], 0) + max(1, item['seconds'])
    for video_id, meta in videos.items():
        item = resolve(video_id)
        key = item['channel_key']
        entry = channels.get(key)
        if entry is None:
            entry = {'key': key, 'name': item['channel_name'], 'category_id': item['channel_category_id'] if item['channel_category_id'] is not None else item['category_id'], 'known_category_id': item['channel_category_id'], 'seconds': 0, 'videos': 0, 'recorded': 0, 'active_days': 0, 'last_date': '', 'first_date': '', 'library_videos': 0, 'library_watched': 0, 'weights': {}}
            channels[key] = entry
        entry['library_videos'] += 1
        if video_id in completed or video_id in history:
            entry['library_watched'] += 1
        if meta.get('category_id') is not None:
            entry['weights'][meta['category_id']] = entry['weights'].get(meta['category_id'], 0) + 1
    for entry in channels.values():
        if entry['known_category_id'] is not None:
            entry['category_id'] = entry['known_category_id']
        elif entry['weights']:
            entry['category_id'] = max(entry['weights'], key=entry['weights'].get)
        else:
            entry['category_id'] = None
        entry['category_name'] = category_name(entry['category_id'])
        entry.pop('known_category_id', None)
        entry.pop('weights', None)
        entry['unwatched'] = max(0, entry['library_videos'] - entry['library_watched'])
        entry['has_activity'] = bool(entry['seconds'] or entry['recorded'])

    known_keys = {'id:' + entry['id'] for entry in known.values() if entry['id']}

    def library_channel(entry):
        return bool(entry['library_videos']) or entry['key'] in known_keys

    in_library = [entry for entry in channels.values() if entry['has_activity'] and library_channel(entry)]
    removed_seconds = sum(int(entry['seconds'] or 0) for entry in channels.values() if entry['has_activity'] and not library_channel(entry))
    items = sorted(in_library, key=lambda entry: (-entry['seconds'], -entry['videos'], entry['name']))[:120]

    cat = {}
    for cid, info in categories.items():
        cat[cid] = {'id': cid, 'name': info['name'], 'daily_limit_minutes': info['daily_limit_minutes'], 'seconds': 0, 'recorded': 0, 'prev_seconds': 0, 'last_date': '', 'library_videos': 0, 'watched_total': 0, 'unwatched': 0, 'channels': 0, 'playlists': 0}
    uncategorized = {'id': None, 'name': 'بدون تصنيف', 'daily_limit_minutes': 0, 'seconds': 0, 'recorded': 0, 'prev_seconds': 0, 'last_date': '', 'library_videos': 0, 'watched_total': 0, 'unwatched': 0, 'channels': 0, 'playlists': 0}
    library_keys = {entry['key'] for entry in channels.values() if library_channel(entry)}
    for item in rows.values():
        if item['channel_key'] not in library_keys:
            continue
        entry = cat.get(item['category_id']) if item['category_id'] is not None else uncategorized
        if entry is None:
            entry = uncategorized
        entry['seconds'] += item['seconds']
        if item['recorded_at']:
            entry['recorded'] += 1
        if item['last_activity'] > entry['last_date']:
            entry['last_date'] = item['last_activity']
    for video_id, meta in videos.items():
        entry = cat.get(meta.get('category_id')) or uncategorized
        entry['library_videos'] += 1
        if video_id in completed or video_id in history:
            entry['watched_total'] += 1
        else:
            entry['unwatched'] += 1
    for entry in channels.values():
        if entry['has_activity'] and library_channel(entry):
            target = cat.get(entry['category_id']) if entry['category_id'] is not None else uncategorized
            if target is not None:
                target['channels'] += 1
    for row in con.execute('SELECT category_id,COUNT(*) AS value FROM user_playlists WHERE user_id=? GROUP BY category_id', (uid,)):
        if row['category_id'] in cat:
            cat[row['category_id']]['playlists'] = int(row['value'] or 0)
    for row in con.execute('SELECT category_id,COUNT(*) AS value FROM user_completed_videos WHERE user_id=? GROUP BY category_id', (uid,)):
        if row['category_id'] in cat:
            cat[row['category_id']]['watched_total'] = max(cat[row['category_id']]['watched_total'], int(row['value'] or 0))
    if previous:
        for row in con.execute("SELECT category_id,COALESCE(SUM(seconds),0) AS value FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? GROUP BY category_id", (uid, previous['start'], previous['end'])):
            target = cat.get(row['category_id']) if row['category_id'] is not None else uncategorized
            if target is not None:
                target['prev_seconds'] = int(row['value'] or 0)
    cat_list = list(cat.values())
    if uncategorized['seconds'] or uncategorized['recorded'] or uncategorized['library_videos']:
        cat_list.append(uncategorized)
    cat_list.sort(key=lambda entry: (-entry['seconds'], -entry['library_videos'], entry['name']))

    daily = {}
    for row in con.execute("SELECT watch_date,SUM(seconds) AS value,COUNT(DISTINCT CASE WHEN seconds>0 THEN video_id END) AS videos FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date BETWEEN ? AND ? GROUP BY watch_date", (uid, start.isoformat(), end.isoformat())):
        daily[row['watch_date']] = {'date': row['watch_date'], 'seconds': int(row['value'] or 0), 'videos': int(row['videos'] or 0), 'recorded': 0}
    for entry in recorded.values():
        key = local_day(entry['completed_at'], offset)
        daily.setdefault(key, {'date': key, 'seconds': 0, 'videos': 0, 'recorded': 0})['recorded'] += 1
    trend = fill_trend(daily, start, end)

    active = [entry for entry in channels.values() if entry['has_activity'] and library_channel(entry)]
    top_channel = max(active, key=lambda entry: (entry['seconds'], entry['videos'])) if active else None
    top_count = max(active, key=lambda entry: (entry['videos'], entry['seconds'])) if active else None
    top_category = max([entry for entry in cat_list if entry['seconds'] > 0], key=lambda entry: entry['seconds'], default=None)
    busiest = max(daily.values(), key=lambda entry: entry['seconds'], default=None)

    review = []
    today_iso = today.isoformat()
    ordered = sorted(channels.values(), key=lambda entry: (-entry['library_videos'], entry['name']))
    for entry in ordered:
        if len(review) >= 3 or entry['library_videos'] < 2:
            continue
        reason = ''
        if not entry['has_activity']:
            reason = 'لديك ' + str(entry['library_videos']) + ' فيديو في مكتبتك ولم تشاهد منها شيئًا في هذه الفترة'
        elif entry['last_date'] and (today - date.fromisoformat(entry['last_date'])).days >= 60:
            reason = 'آخر مشاهدة من هذه القناة منذ أكثر من ٦٠ يومًا'
        elif entry['library_videos'] >= 5 and entry['library_watched'] / entry['library_videos'] < 0.3:
            reason = 'شاهدت ' + str(entry['library_watched']) + ' من ' + str(entry['library_videos']) + ' فيديو متاح'
        if reason:
            review.append({'key': entry['key'], 'name': entry['name'], 'reason': reason, 'library_videos': entry['library_videos'], 'unwatched': entry['unwatched'], 'last_date': entry['last_date']})

    library_total = len(videos)
    watched_ever = len(set(completed) | set(history))
    unwatched = max(0, library_total - sum(1 for video_id in videos if video_id in completed or video_id in history))
    followed_playlists = int(con.execute('SELECT COUNT(*) AS value FROM user_playlists WHERE user_id=?', (uid,)).fetchone()['value'] or 0)
    totals = {
        'actual_seconds': total_seconds,
        'recorded': recorded_count,
        'actual_videos': actual_count,
        'avg_seconds': average_seconds,
        'completion_rate': completion_rate,
        'completion_count': completed_by_watch,
        'completion_total': len(durations),
        'library_total': library_total,
        'tracked': library_total + max(0, len(completed) - sum(1 for video_id in completed if video_id in videos)),
        'watched_total': watched_ever,
        'unwatched': unwatched,
        'channels_total': len(active),
        'channels_library': sum(1 for entry in channels.values() if entry['library_videos']),
        'playlists_total': followed_playlists,
        'excluded_seconds': removed_seconds,
        'prev_actual_seconds': previous_seconds,
        'prev_recorded': previous_recorded,
        'prev_actual_videos': previous_videos,
    }
    filters_channels = [{'key': entry['key'], 'name': entry['name'], 'seconds': entry['seconds'], 'videos': entry['videos'], 'recorded': entry['recorded'], 'library_videos': entry['library_videos']} for entry in sorted(channels.values(), key=lambda entry: (-entry['seconds'], entry['name'])) if library_channel(entry)][:200]
    payload = {
        'library': 'video',
        'period': public_period(period_obj),
        'totals': totals,
        'items': items,
        'subitems': [],
        'categories': cat_list,
        'trend': trend,
        'calendar': calendar_payload(con, uid, 'video', calendar_month, today, offset, created_at),
        'insights': {'top_channel': lean_entry(top_channel), 'top_count': lean_entry(top_count), 'top_category': lean_entry(top_category), 'busiest_day': busiest, 'review': review},
        'filters': {'channels': filters_channels, 'categories': [{'id': entry['id'], 'name': entry['name'], 'seconds': entry['seconds'], 'recorded': entry['recorded'], 'library_videos': entry['library_videos']} for entry in cat_list], 'playlists': []},
        'account_created_at': int(created_at or 0),
    }
    ctx = {'rows': rows, 'videos': videos, 'completed': completed, 'history': history, 'resolve': resolve, 'channels': channels, 'cat_list': cat_list, 'categories': categories, 'library_keys': library_keys}
    return payload, ctx

def collection_stats(con, uid, created_at, period_obj, offset, calendar_month, today):
    start = date.fromisoformat(period_obj['start'])
    end = date.fromisoformat(period_obj['end'])
    previous = period_obj.get('prev')

    categories = {}
    for row in con.execute('SELECT id,name FROM user_collection_categories WHERE user_id=?', (uid,)):
        categories[row['id']] = row['name']

    def category_name(cid):
        return categories.get(cid) or 'بدون تصنيف'

    playlists = {}
    for row in con.execute('SELECT p.id,p.title,p.channel_title,p.thumbnail_url,p.category_id,p.added_at,c.name AS category FROM user_collection_playlists p LEFT JOIN user_collection_categories c ON c.id=p.category_id WHERE p.user_id=?', (uid,)):
        entry = dict(row)
        entry['added_date'] = local_day(row['added_at'], offset) if row['added_at'] else ''
        playlists[row['id']] = entry

    current = {}
    for row in con.execute('SELECT v.playlist_id,v.video_id,v.title,v.channel_title,v.thumbnail_url,v.video_url,v.duration_seconds FROM user_collection_videos v JOIN user_collection_playlists p ON p.id=v.playlist_id AND p.user_id=v.user_id WHERE v.user_id=?', (uid,)):
        current[(row['playlist_id'], row['video_id'])] = dict(row)

    history = {}
    for row in con.execute('SELECT playlist_id,video_id,title,channel_title,thumbnail_url,video_url,duration_seconds FROM user_collection_history WHERE user_id=?', (uid,)):
        history[(row['playlist_id'], row['video_id'])] = dict(row)

    completed = {}
    for row in con.execute('SELECT playlist_id,video_id,category_id,completed_at FROM user_collection_completed WHERE user_id=?', (uid,)):
        completed[(row['playlist_id'], row['video_id'])] = dict(row)

    identity = {}
    for row in con.execute("SELECT video_id,title,channel_title FROM video_identity WHERE channel_title<>''"):
        identity[row['video_id']] = dict(row)

    actual = {}
    for row in con.execute("SELECT collection_playlist_id AS playlist_id,video_id,SUM(seconds) AS seconds,MIN(watch_date) AS first_date,MAX(watch_date) AS last_date,COUNT(DISTINCT watch_date) AS active_days FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ? AND seconds>0 AND collection_playlist_id IS NOT NULL GROUP BY collection_playlist_id,video_id", (uid, start.isoformat(), end.isoformat())):
        actual[(row['playlist_id'], row['video_id'])] = dict(row)

    recorded = {}
    for row in con.execute('SELECT playlist_id,video_id,completed_at FROM user_collection_completed WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, period_obj['start_ts'], period_obj['end_ts'])):
        recorded[(row['playlist_id'], row['video_id'])] = dict(row)

    total_seconds = int(con.execute("SELECT COALESCE(SUM(seconds),0) AS value FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ?", (uid, start.isoformat(), end.isoformat())).fetchone()['value'] or 0)
    played_videos = int(con.execute("SELECT COUNT(DISTINCT video_id) AS value FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ? AND seconds>0", (uid, start.isoformat(), end.isoformat())).fetchone()['value'] or 0)
    previous_seconds = previous_videos = previous_recorded = 0
    if previous:
        row = con.execute("SELECT COALESCE(SUM(seconds),0) AS value FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ?", (uid, previous['start'], previous['end'])).fetchone()
        previous_seconds = int(row['value'] or 0)
        row = con.execute("SELECT COUNT(DISTINCT video_id) AS value FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ? AND seconds>0", (uid, previous['start'], previous['end'])).fetchone()
        previous_videos = int(row['value'] or 0)
        row = con.execute('SELECT COUNT(*) AS value FROM user_collection_completed WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, previous['start_ts'], previous['end_ts'])).fetchone()
        previous_recorded = int(row['value'] or 0)

    def resolve(playlist_id, video_id):
        key = (playlist_id, video_id)
        cur = current.get(key) or {}
        hist = history.get(key) or {}
        playlist = playlists.get(playlist_id) or {}
        ident = identity.get(video_id) or {}
        title = cur.get('title') or hist.get('title') or ident.get('title') or 'فيديو من القائمة'
        channel_title = cur.get('channel_title') or hist.get('channel_title') or playlist.get('channel_title') or ident.get('channel_title') or 'قناة غير معروفة'
        duration = cur.get('duration_seconds')
        if duration is None:
            duration = hist.get('duration_seconds')
        category_id = playlist.get('category_id')
        return {
            'playlist_id': playlist_id,
            'playlist_title': playlist.get('title') or hist.get('playlist_title') or 'قائمة محذوفة',
            'video_id': video_id,
            'title': title,
            'channel_name': channel_title,
            'channel_key': 't:' + norm(channel_title),
            'category_id': category_id,
            'category_name': category_name(category_id),
            'duration_seconds': int(duration) if duration not in (None, '') else None,
            'thumbnail_url': cur.get('thumbnail_url') or hist.get('thumbnail_url') or 'https://i.ytimg.com/vi/' + video_id + '/hqdefault.jpg',
            'video_url': cur.get('video_url') or hist.get('video_url') or 'https://www.youtube.com/watch?v=' + video_id,
        }

    rows = {}
    for key, entry in actual.items():
        item = resolve(*key)
        item.update(seconds=int(entry['seconds'] or 0), first_date=entry['first_date'] or '', last_date=entry['last_date'] or '', active_days=int(entry['active_days'] or 0), recorded_at=None, recorded_date='')
        rows[key] = item
    for key, entry in recorded.items():
        item = rows.get(key)
        if item is None:
            item = resolve(*key)
            item.update(seconds=0, first_date='', last_date='', active_days=0)
            rows[key] = item
        item['recorded_at'] = int(entry['completed_at'] or 0)
        item['recorded_date'] = local_day(entry['completed_at'], offset)
    for item in rows.values():
        item['last_activity'] = max(item.get('last_date') or '', item.get('recorded_date') or '')

    playlist_aggs = {}

    def playlist_entry(playlist_id):
        entry = playlist_aggs.get(playlist_id)
        if entry is None:
            playlist = playlists.get(playlist_id) or {}
            entry = {
                'key': str(playlist_id),
                'playlist_id': playlist_id,
                'title': playlist.get('title') or 'قائمة محذوفة',
                'channel_title': playlist.get('channel_title') or 'YouTube',
                'thumbnail_url': playlist.get('thumbnail_url') or '',
                'category_id': playlist.get('category_id'),
                'category_name': category_name(playlist.get('category_id')),
                'added_at': int(playlist.get('added_at') or 0),
                'added_date': playlist.get('added_date') or '',
                'seconds': 0,
                'videos': 0,
                'recorded': 0,
                'current_videos': 0,
                'completed_total': 0,
                'total_videos': 0,
                'unwatched': 0,
                'last_date': '',
                'channel_set': set(),
                'channels': [],
                'channel_count': 0,
            }
            playlist_aggs[playlist_id] = entry
        return entry

    for key, item in rows.items():
        entry = playlist_entry(item['playlist_id'])
        entry['seconds'] += item['seconds']
        if item['seconds'] > 0:
            entry['videos'] += 1
        if item['recorded_at']:
            entry['recorded'] += 1
        if item['last_activity'] > entry['last_date']:
            entry['last_date'] = item['last_activity']
        if item['channel_name']:
            entry['channel_set'].add(item['channel_name'])
    for key, row in current.items():
        if key[0] in playlists:
            entry = playlist_entry(key[0])
            entry['current_videos'] += 1
            if row.get('channel_title'):
                entry['channel_set'].add(row['channel_title'])
    for key, row in history.items():
        if key[0] in playlists and row.get('channel_title'):
            playlist_entry(key[0])['channel_set'].add(row['channel_title'])
    for key in completed:
        if key[0] in playlists:
            playlist_entry(key[0])['completed_total'] += 1
    for entry in playlist_aggs.values():
        entry['total_videos'] = entry['current_videos'] + entry['completed_total']
        entry['unwatched'] = entry['current_videos']
        entry['channel_count'] = len(entry['channel_set'])
        entry['channels'] = sorted(entry['channel_set'])[:12]
        entry.pop('channel_set', None)
        entry['completed_share'] = round(entry['completed_total'] * 100 / entry['total_videos']) if entry['total_videos'] else 0
        entry['has_activity'] = bool(entry['seconds'] or entry['recorded'])
        entry['last_activity'] = entry['last_date'] or entry['added_date']

    channel_aggs = {}

    def channel_entry(name):
        channel_name = name or 'قناة غير معروفة'
        key = 't:' + norm(channel_name)
        entry = channel_aggs.get(key)
        if entry is None:
            entry = {'key': key, 'name': channel_name, 'seconds': 0, 'videos': 0, 'recorded': 0, 'last_date': '', 'playlist_set': set(), 'playlists': [], 'playlist_count': 0, 'library_videos': 0}
            channel_aggs[key] = entry
        return entry

    for item in rows.values():
        if item['playlist_id'] not in playlists:
            continue
        entry = channel_entry(item['channel_name'])
        entry['seconds'] += item['seconds']
        if item['seconds'] > 0:
            entry['videos'] += 1
        if item['recorded_at']:
            entry['recorded'] += 1
        entry['playlist_set'].add(item['playlist_id'])
        if item['last_activity'] > entry['last_date']:
            entry['last_date'] = item['last_activity']
    for key, row in current.items():
        entry = channel_entry(row.get('channel_title'))
        entry['library_videos'] += 1
        entry['playlist_set'].add(key[0])
    for entry in channel_aggs.values():
        weights = {}
        for playlist_id in entry['playlist_set']:
            category_id = (playlists.get(playlist_id) or {}).get('category_id')
            if category_id is not None:
                weights[category_id] = weights.get(category_id, 0) + 1
        entry['category_id'] = max(weights, key=weights.get) if weights else None
        entry['category_name'] = categories.get(entry['category_id']) or 'بدون تصنيف'
        entry['playlist_count'] = len(entry['playlist_set'])
        entry['playlists'] = sorted(entry['playlist_set'])
        entry.pop('playlist_set', None)
        entry['has_activity'] = bool(entry['seconds'] or entry['recorded'])

    cat = {}
    for cid, name in categories.items():
        cat[cid] = {'id': cid, 'name': name, 'seconds': 0, 'recorded': 0, 'prev_seconds': 0, 'last_date': '', 'playlists': 0, 'videos_total': 0, 'completed': 0, 'channels': 0}
    for row in con.execute("SELECT w.collection_category_id AS cid,COALESCE(SUM(w.seconds),0) AS value FROM watch_time_daily w JOIN user_collection_playlists p ON p.id=w.collection_playlist_id AND p.user_id=w.user_id WHERE w.user_id=? AND w.library_type='collection' AND w.watch_date BETWEEN ? AND ? GROUP BY w.collection_category_id", (uid, start.isoformat(), end.isoformat())):
        if row['cid'] in cat:
            cat[row['cid']]['seconds'] = int(row['value'] or 0)
    for row in con.execute("SELECT c.category_id AS category_id,COUNT(*) AS value FROM user_collection_completed c JOIN user_collection_playlists p ON p.id=c.playlist_id AND p.user_id=c.user_id WHERE c.user_id=? AND c.completed_at BETWEEN ? AND ? GROUP BY c.category_id", (uid, period_obj['start_ts'], period_obj['end_ts'])):
        if row['category_id'] in cat:
            cat[row['category_id']]['recorded'] = int(row['value'] or 0)
    if previous:
        for row in con.execute("SELECT collection_category_id AS cid,COALESCE(SUM(seconds),0) AS value FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ? GROUP BY collection_category_id", (uid, previous['start'], previous['end'])):
            if row['cid'] in cat:
                cat[row['cid']]['prev_seconds'] = int(row['value'] or 0)
    for entry in playlist_aggs.values():
        if entry['playlist_id'] not in playlists:
            continue
        target = cat.get(entry['category_id'])
        if target is None:
            continue
        target['playlists'] += 1
        target['videos_total'] += entry['total_videos']
        target['completed'] += entry['completed_total']
        if entry['last_activity'] > target['last_date']:
            target['last_date'] = entry['last_activity']
        target['channels'] += entry['channel_count']
    cat_list = sorted(cat.values(), key=lambda entry: (-entry['seconds'], -entry['videos_total'], entry['name']))

    daily = {}
    for row in con.execute("SELECT watch_date,SUM(seconds) AS value,COUNT(DISTINCT CASE WHEN seconds>0 THEN video_id END) AS videos FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date BETWEEN ? AND ? GROUP BY watch_date", (uid, start.isoformat(), end.isoformat())):
        daily[row['watch_date']] = {'date': row['watch_date'], 'seconds': int(row['value'] or 0), 'videos': int(row['videos'] or 0), 'recorded': 0}
    for entry in recorded.values():
        key = local_day(entry['completed_at'], offset)
        daily.setdefault(key, {'date': key, 'seconds': 0, 'videos': 0, 'recorded': 0})['recorded'] += 1
    trend = fill_trend(daily, start, end)

    kept_playlists = [entry for entry in playlist_aggs.values() if entry['playlist_id'] in playlists]
    total_videos = sum(entry['total_videos'] for entry in kept_playlists)
    completed_total = sum(entry['completed_total'] for entry in kept_playlists)
    current_total = sum(entry['current_videos'] for entry in kept_playlists)
    completion_rate = round(completed_total * 100 / total_videos) if total_videos else 0
    average_seconds = int(total_seconds / played_videos) if played_videos else 0

    active = [entry for entry in kept_playlists if entry['has_activity']]
    top_playlist = max(active, key=lambda entry: (entry['seconds'], entry['videos'])) if active else None
    active_channels = [entry for entry in channel_aggs.values() if entry['has_activity']]
    top_channel = max(active_channels, key=lambda entry: (entry['seconds'], entry['videos'])) if active_channels else None
    top_category = max([entry for entry in cat_list if entry['seconds'] > 0], key=lambda entry: entry['seconds'], default=None)
    busiest = max(daily.values(), key=lambda entry: entry['seconds'], default=None)

    review = []
    for entry in sorted(kept_playlists, key=lambda entry: (-entry['total_videos'], entry['title'])):
        if len(review) >= 3 or entry['total_videos'] < 3:
            continue
        reason = ''
        if entry['completed_total'] and entry['completed_share'] < 35:
            reason = 'أكملت ' + str(entry['completed_total']) + ' من ' + str(entry['total_videos']) + ' فيديو فقط'
        elif not entry['has_activity'] and entry['added_date'] and (today - date.fromisoformat(entry['added_date'])).days >= 30:
            reason = 'القائمة في مكتبتك منذ أكثر من ٣٠ يومًا ولم تشاهد منها شيئًا'
        if reason:
            review.append({'key': entry['key'], 'name': entry['title'], 'reason': reason, 'total_videos': entry['total_videos'], 'completed': entry['completed_total'], 'last_date': entry['last_date']})

    items = sorted(kept_playlists, key=lambda entry: (-entry['seconds'], -entry['recorded'], entry['title']))[:120]
    subitems = sorted(active_channels, key=lambda entry: (-entry['seconds'], -entry['videos'], entry['name']))[:120]
    totals = {
        'actual_seconds': total_seconds,
        'recorded': len(recorded),
        'actual_videos': played_videos,
        'avg_seconds': average_seconds,
        'completion_rate': completion_rate,
        'completion_count': completed_total,
        'completion_total': total_videos,
        'library_total': total_videos,
        'tracked': total_videos,
        'watched_total': completed_total,
        'unwatched': current_total,
        'channels_total': len(active_channels),
        'channels_library': sum(1 for entry in channel_aggs.values() if entry['library_videos']),
        'playlists_total': len(playlists),
        'excluded_seconds': sum(int(entry['seconds'] or 0) for entry in playlist_aggs.values() if entry['playlist_id'] not in playlists),
        'prev_actual_seconds': previous_seconds,
        'prev_recorded': previous_recorded,
        'prev_actual_videos': previous_videos,
    }
    filters_channels = [{'key': entry['key'], 'name': entry['name'], 'seconds': entry['seconds'], 'videos': entry['videos'], 'recorded': entry['recorded'], 'library_videos': entry['library_videos']} for entry in sorted(channel_aggs.values(), key=lambda entry: (-entry['seconds'], entry['name'])) if entry['has_activity'] or entry['library_videos']][:200]
    filters_playlists = [{'id': entry['playlist_id'], 'title': entry['title'], 'seconds': entry['seconds']} for entry in sorted(kept_playlists, key=lambda entry: (-entry['seconds'], entry['title']))]
    payload = {
        'library': 'collection',
        'period': public_period(period_obj),
        'totals': totals,
        'items': items,
        'subitems': subitems,
        'categories': cat_list,
        'trend': trend,
        'calendar': calendar_payload(con, uid, 'collection', calendar_month, today, offset, created_at),
        'insights': {'top_channel': lean_entry(top_channel), 'top_playlist': lean_entry(top_playlist), 'top_category': lean_entry(top_category), 'busiest_day': busiest, 'review': review},
        'filters': {'channels': filters_channels, 'categories': [{'id': entry['id'], 'name': entry['name'], 'seconds': entry['seconds'], 'recorded': entry['recorded'], 'library_videos': entry['videos_total']} for entry in cat_list], 'playlists': filters_playlists},
        'account_created_at': int(created_at or 0),
    }
    ctx = {'rows': rows, 'current': current, 'history': history, 'completed': completed, 'playlists': playlists, 'playlist_aggs': playlist_aggs, 'channels': channel_aggs, 'categories': categories, 'resolve': resolve}
    return payload, ctx

def _video_out(item):
    return {
        'video_id': item['video_id'],
        'title': item['title'],
        'channel_key': item['channel_key'],
        'channel_name': item['channel_name'],
        'category_id': item.get('category_id'),
        'category_name': item.get('category_name') or 'بدون تصنيف',
        'seconds': int(item.get('seconds') or 0),
        'recorded': bool(item.get('recorded_at')),
        'recorded_date': item.get('recorded_date') or '',
        'last_date': item.get('last_activity') or item.get('last_date') or '',
        'duration_seconds': item.get('duration_seconds'),
        'thumbnail_url': item.get('thumbnail_url') or '',
        'video_url': item.get('video_url') or '',
        'active_days': int(item.get('active_days') or 0),
    }


def _collection_out(item):
    return {
        'video_id': item['video_id'],
        'playlist_id': item['playlist_id'],
        'playlist_title': item.get('playlist_title') or '',
        'title': item['title'],
        'channel_key': item['channel_key'],
        'channel_name': item['channel_name'],
        'category_id': item.get('category_id'),
        'category_name': item.get('category_name') or 'بدون تصنيف',
        'seconds': int(item.get('seconds') or 0),
        'recorded': bool(item.get('recorded_at')),
        'recorded_date': item.get('recorded_date') or '',
        'last_date': item.get('last_activity') or item.get('last_date') or '',
        'duration_seconds': item.get('duration_seconds'),
        'thumbnail_url': item.get('thumbnail_url') or '',
        'video_url': item.get('video_url') or '',
        'active_days': int(item.get('active_days') or 0),
    }


def _breakdown(items):
    acc = {}
    for item in items:
        key = item['channel_key']
        entry = acc.get(key)
        if entry is None:
            entry = {'key': key, 'name': item['channel_name'], 'seconds': 0, 'videos': 0, 'recorded': 0}
            acc[key] = entry
        entry['seconds'] += item['seconds']
        if item['seconds'] > 0:
            entry['videos'] += 1
        if item.get('recorded_at'):
            entry['recorded'] += 1
    return sorted(acc.values(), key=lambda entry: (-entry['seconds'], -entry['videos'], entry['name']))


def _summary(items, library_videos=0, unwatched=0):
    actual = [item for item in items if item['seconds'] > 0]
    durations = [item for item in actual if item.get('duration_seconds') and item['duration_seconds'] > 0]
    completed = sum(1 for item in durations if item['seconds'] >= 0.85 * item['duration_seconds'])
    return {
        'seconds': sum(item['seconds'] for item in items),
        'videos': len(actual),
        'recorded': sum(1 for item in items if item.get('recorded_at')),
        'avg_seconds': int(sum(item['seconds'] for item in actual) / len(actual)) if actual else 0,
        'completion_rate': round(completed * 100 / len(durations)) if durations else 0,
        'last_date': max([item.get('last_activity') or item.get('last_date') or '' for item in items], default=''),
        'library_videos': library_videos,
        'unwatched': unwatched,
    }


def video_detail(con, uid, created_at, period_obj, offset, calendar_month, today, detail_type, key, limit=60):
    _, ctx = video_stats(con, uid, created_at, period_obj, offset, calendar_month, today)
    rows, videos, completed, history = ctx['rows'], ctx['videos'], ctx['completed'], ctx['history']
    resolve, channels, cats = ctx['resolve'], ctx['channels'], ctx['categories']
    matched, available, playlists = [], [], []
    title, subtitle = '', ''
    library_videos = unwatched = 0

    if detail_type == 'channel':
        matched = [item for item in rows.values() if item['channel_key'] == key]
        entry = channels.get(key)
        title = entry['name'] if entry else (matched[0]['channel_name'] if matched else 'قناة')
        subtitle = entry['category_name'] if entry else ''
        library_videos = entry['library_videos'] if entry else 0
        unwatched = entry['unwatched'] if entry else 0
        for video_id in videos:
            item = resolve(video_id)
            if item['channel_key'] == key and video_id not in completed and video_id not in history:
                available.append(item)
        available.sort(key=lambda item: item.get('published_at') or '', reverse=True)
    elif detail_type == 'category':
        cid = None if key in ('none', 'null', '') else int(key)
        matched = [item for item in rows.values() if item['category_id'] == cid and item['channel_key'] in ctx['library_keys']]
        info = cats.get(cid) or {'name': 'بدون تصنيف'}
        title = info['name']
        subtitle = 'تصنيف'
        library_videos = sum(1 for meta in videos.values() if (meta.get('category_id') if cid is not None else None) == cid)
        unwatched = sum(1 for video_id, meta in videos.items() if (meta.get('category_id') if cid is not None else None) == cid and video_id not in completed and video_id not in history)
        for video_id, meta in videos.items():
            if (meta.get('category_id') if cid is not None else None) == cid and video_id not in completed and video_id not in history:
                available.append(resolve(video_id))
        available.sort(key=lambda item: item.get('published_at') or '', reverse=True)
    elif detail_type == 'day':
        try:
            day = date.fromisoformat(key)
        except ValueError:
            raise ValueError('اليوم غير صالح')
        day_start = day_epoch(day) - offset
        day_end = day_epoch(day + timedelta(days=1)) - offset - 1
        matched_map = {}
        for row in con.execute("SELECT video_id,COALESCE(SUM(seconds),0) AS seconds FROM watch_time_daily WHERE user_id=? AND library_type='video' AND watch_date=? AND seconds>0 GROUP BY video_id", (uid, key)):
            item = resolve(row['video_id'])
            item.update(seconds=int(row['seconds'] or 0), first_date=key, last_date=key, active_days=1, recorded_at=None, recorded_date='', last_activity=key)
            matched_map[row['video_id']] = item
        for row in con.execute('SELECT video_id,completed_at,category_id FROM user_completed_videos WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, day_start, day_end)):
            item = matched_map.get(row['video_id'])
            if item is None:
                item = resolve(row['video_id'])
                item.update(seconds=0, first_date='', last_date='', active_days=0, recorded_at=None, recorded_date='')
                matched_map[row['video_id']] = item
                if row['category_id'] is not None and item.get('category_id') is None:
                    item['category_id'] = row['category_id']
                    item['category_name'] = (cats.get(row['category_id']) or {}).get('name') or 'بدون تصنيف'
            item['recorded_at'] = int(row['completed_at'] or 0)
            item['recorded_date'] = key
            item['last_activity'] = key
        matched = list(matched_map.values())
        title = day.strftime('%Y-%m-%d')
        subtitle = 'ملخص اليوم'
    else:
        raise ValueError('نوع التفاصيل غير معروف')

    watched = sorted(matched, key=lambda item: (item.get('last_activity') or '', item.get('seconds') or 0), reverse=True)
    summary = _summary(matched, library_videos, unwatched)
    if detail_type == 'day':
        summary['library_videos'] = summary['videos']
        summary['unwatched'] = 0
    return {
        'library': 'video',
        'type': detail_type,
        'key': key,
        'title': title,
        'subtitle': subtitle,
        'period': public_period(period_obj),
        'summary': summary,
        'watched': [_video_out(item) for item in watched[:limit]],
        'watched_total': len(watched),
        'available': [_video_out(item) for item in available[:limit]],
        'available_total': len(available),
        'channels': _breakdown(matched)[:30],
        'playlists': playlists,
        'channel_key': key if detail_type == 'channel' else None,
    }


def collection_detail(con, uid, created_at, period_obj, offset, calendar_month, today, detail_type, key, limit=60):
    _, ctx = collection_stats(con, uid, created_at, period_obj, offset, calendar_month, today)
    rows, current, completed = ctx['rows'], ctx['current'], ctx['completed']
    playlists, playlist_aggs, channels = ctx['playlists'], ctx['playlist_aggs'], ctx['channels']
    resolve, cats = ctx['resolve'], ctx['categories']
    matched, available, related_playlists = [], [], []
    title, subtitle = '', ''
    library_videos = unwatched = 0

    def available_from(playlist_ids):
        result = []
        for (playlist_id, video_id), row in current.items():
            if playlist_id in playlist_ids and (playlist_id, video_id) not in completed:
                result.append(resolve(playlist_id, video_id))
        return result

    if detail_type == 'playlist':
        playlist_id = int(key)
        entry = playlist_aggs.get(playlist_id)
        if entry is None:
            raise ValueError('قائمة التشغيل غير موجودة')
        matched = [item for item in rows.values() if item['playlist_id'] == playlist_id]
        available = available_from({playlist_id})
        available.sort(key=lambda item: item.get('title') or '')
        title = entry['title']
        subtitle = entry['category_name']
        library_videos = entry['total_videos']
        unwatched = entry['unwatched']
        related_playlists = []
    elif detail_type == 'channel':
        matched = [item for item in rows.values() if item['channel_key'] == key and item['playlist_id'] in playlists]
        entry = channels.get(key)
        title = entry['name'] if entry else (matched[0]['channel_name'] if matched else 'قناة')
        playlist_ids = set(item['playlist_id'] for item in matched) | set(entry['playlists'] if entry else [])
        subtitle = str(len(playlist_ids)) + ' قائمة تشغيل'
        library_videos = entry['library_videos'] if entry else 0
        available = available_from(playlist_ids)
        related_playlists = [playlist_aggs[playlist_id] for playlist_id in playlist_ids if playlist_id in playlist_aggs]
    elif detail_type == 'category':
        cid = int(key)
        matched = [item for item in rows.values() if item['category_id'] == cid and item['playlist_id'] in playlists]
        title = cats.get(cid) or 'بدون تصنيف'
        subtitle = 'تصنيف'
        playlist_ids = set(entry['playlist_id'] for entry in playlist_aggs.values() if entry['category_id'] == cid and entry['playlist_id'] in playlists)
        available = available_from(playlist_ids)
        related_playlists = [playlist_aggs[playlist_id] for playlist_id in playlist_ids if playlist_id in playlist_aggs]
        library_videos = sum(entry['total_videos'] for entry in related_playlists)
        unwatched = sum(entry['unwatched'] for entry in related_playlists)
    elif detail_type == 'day':
        try:
            day = date.fromisoformat(key)
        except ValueError:
            raise ValueError('اليوم غير صالح')
        day_start = day_epoch(day) - offset
        day_end = day_epoch(day + timedelta(days=1)) - offset - 1
        matched_map = {}
        for row in con.execute("SELECT collection_playlist_id AS playlist_id,video_id,COALESCE(SUM(seconds),0) AS seconds FROM watch_time_daily WHERE user_id=? AND library_type='collection' AND watch_date=? AND seconds>0 AND collection_playlist_id IS NOT NULL GROUP BY collection_playlist_id,video_id", (uid, key)):
            item = resolve(row['playlist_id'], row['video_id'])
            item.update(seconds=int(row['seconds'] or 0), first_date=key, last_date=key, active_days=1, recorded_at=None, recorded_date='', last_activity=key)
            matched_map[(row['playlist_id'], row['video_id'])] = item
        for row in con.execute('SELECT playlist_id,video_id,completed_at FROM user_collection_completed WHERE user_id=? AND completed_at BETWEEN ? AND ?', (uid, day_start, day_end)):
            pair = (row['playlist_id'], row['video_id'])
            item = matched_map.get(pair)
            if item is None:
                item = resolve(*pair)
                item.update(seconds=0, first_date='', last_date='', active_days=0, recorded_at=None, recorded_date='')
                matched_map[pair] = item
            item['recorded_at'] = int(row['completed_at'] or 0)
            item['recorded_date'] = key
            item['last_activity'] = key
        matched = list(matched_map.values())
        title = day.strftime('%Y-%m-%d')
        subtitle = 'ملخص اليوم'
    else:
        raise ValueError('نوع التفاصيل غير معروف')

    watched = sorted(matched, key=lambda item: (item.get('last_activity') or '', item.get('seconds') or 0), reverse=True)
    summary = _summary(matched, library_videos, unwatched)
    return {
        'library': 'collection',
        'type': detail_type,
        'key': key,
        'title': title,
        'subtitle': subtitle,
        'period': public_period(period_obj),
        'summary': summary,
        'watched': [_collection_out(item) for item in watched[:limit]],
        'watched_total': len(watched),
        'available': [_collection_out(item) for item in available[:limit]],
        'available_total': len(available),
        'channels': _breakdown(matched)[:30],
        'playlists': [{'playlist_id': entry['playlist_id'], 'title': entry['title'], 'seconds': entry['seconds'], 'total_videos': entry['total_videos'], 'completed': entry['completed_total'], 'category_name': entry['category_name']} for entry in sorted(related_playlists, key=lambda entry: (-entry['seconds'], entry['title']))],
        'channel_key': key if detail_type == 'channel' else None,
    }


def video_history(con, uid, created_at, period_obj, offset, calendar_month, today, query='', category='all', channel='all', page=1, limit=40):
    _, ctx = video_stats(con, uid, created_at, period_obj, offset, calendar_month, today)
    resolve = ctx['resolve']
    rows_map = ctx['rows']
    sql = "SELECT h.video_id,h.title,h.channel_title,h.category_id,COALESCE(c.name,h.category_name) AS category_name,h.published_at,h.watched_at,COALESCE(v.thumbnail_url,h.thumbnail_url) AS thumbnail_url,COALESCE(v.video_url,h.video_url) AS video_url,COALESCE(v.duration_seconds,h.duration_seconds) AS duration_seconds FROM user_watch_history h LEFT JOIN user_categories c ON c.id=h.category_id LEFT JOIN user_videos v ON v.user_id=h.user_id AND v.video_id=h.video_id WHERE h.user_id=? AND h.watched_at BETWEEN ? AND ?"
    params = [uid, period_obj['start_ts'], period_obj['end_ts']]
    if category == 'none':
        sql += ' AND h.category_id IS NULL'
    elif category not in ('', 'all', None):
        sql += ' AND h.category_id=?'
        params.append(int(category))
    sql += ' ORDER BY h.watched_at DESC'
    needle = norm(query)
    items = []
    for row in con.execute(sql, params):
        item = resolve(row['video_id'])
        actual = rows_map.get(row['video_id']) or {}
        entry = _video_out(item)
        entry.update(title=row['title'] or item['title'], channel_name=item['channel_name'], category_id=row['category_id'], category_name=row['category_name'] or item['category_name'], seconds=int(actual.get('seconds') or 0), recorded=True, last_date=local_day(row['watched_at'], offset), watched_at=int(row['watched_at'] or 0), watched_date=local_day(row['watched_at'], offset), duration_seconds=row['duration_seconds'], thumbnail_url=row['thumbnail_url'] or item['thumbnail_url'], video_url=row['video_url'] or item['video_url'])
        if channel not in ('', 'all', None) and entry['channel_key'] != channel:
            continue
        if needle and needle not in norm(entry['title']) and needle not in norm(entry['channel_name']):
            continue
        items.append(entry)
    total = len(items)
    page = max(1, int(page or 1))
    limit = max(10, min(100, int(limit or 40)))
    start = (page - 1) * limit
    return {'library': 'video', 'items': items[start:start + limit], 'total': total, 'page': page, 'limit': limit, 'has_more': start + limit < total}


def collection_history(con, uid, created_at, period_obj, offset, calendar_month, today, query='', category='all', channel='all', playlist='all', page=1, limit=40):
    _, ctx = collection_stats(con, uid, created_at, period_obj, offset, calendar_month, today)
    resolve = ctx['resolve']
    rows_map = ctx['rows']
    sql = "SELECT h.playlist_id,h.video_id,h.playlist_title,h.title,h.channel_title,h.category_id,COALESCE(c.name,h.category_name) AS category_name,h.published_at,h.watched_at,COALESCE(h.thumbnail_url,v.thumbnail_url) AS thumbnail_url,COALESCE(h.video_url,v.video_url) AS video_url,COALESCE(h.duration_seconds,v.duration_seconds) AS duration_seconds FROM user_collection_history h LEFT JOIN user_collection_categories c ON c.id=h.category_id LEFT JOIN user_collection_videos v ON v.user_id=h.user_id AND v.playlist_id=h.playlist_id AND v.video_id=h.video_id WHERE h.user_id=? AND h.watched_at BETWEEN ? AND ?"
    params = [uid, period_obj['start_ts'], period_obj['end_ts']]
    if category == 'none':
        sql += ' AND h.category_id IS NULL'
    elif category not in ('', 'all', None):
        sql += ' AND h.category_id=?'
        params.append(int(category))
    if playlist not in ('', 'all', None):
        sql += ' AND h.playlist_id=?'
        params.append(int(playlist))
    sql += ' ORDER BY h.watched_at DESC'
    needle = norm(query)
    items = []
    for row in con.execute(sql, params):
        pair = (row['playlist_id'], row['video_id'])
        item = resolve(*pair)
        actual = rows_map.get(pair) or {}
        entry = _collection_out(item)
        entry.update(title=row['title'] or item['title'], playlist_title=row['playlist_title'] or item['playlist_title'], channel_name=item['channel_name'], category_id=row['category_id'], category_name=row['category_name'] or item['category_name'], seconds=int(actual.get('seconds') or 0), recorded=True, last_date=local_day(row['watched_at'], offset), watched_at=int(row['watched_at'] or 0), watched_date=local_day(row['watched_at'], offset), duration_seconds=row['duration_seconds'], thumbnail_url=row['thumbnail_url'] or item['thumbnail_url'], video_url=row['video_url'] or item['video_url'])
        if channel not in ('', 'all', None) and entry['channel_key'] != channel:
            continue
        if needle and needle not in norm(entry['title']) and needle not in norm(entry['channel_name']) and needle not in norm(entry['playlist_title']):
            continue
        items.append(entry)
    total = len(items)
    page = max(1, int(page or 1))
    limit = max(10, min(100, int(limit or 40)))
    start = (page - 1) * limit
    return {'library': 'collection', 'items': items[start:start + limit], 'total': total, 'page': page, 'limit': limit, 'has_more': start + limit < total}


def calendar(con, uid, library, month_key, today, offset, created_at):
    return calendar_payload(con, uid, library, month_key, today, offset, created_at)
