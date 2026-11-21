"""K8-PLUS pilot: only the addressed thread; no model, cache or send calls.

Fully config-driven (no hardcoded IDs): set GCHAT_PILOT_SPACE,
GCHAT_PILOT_BOT_USER and GCHAT_PILOT_APP_SA in the environment (e.g. .env)
to bind the pilot to your space/bot. With any of them unset the pilot is
inert, so importing this module is always safe.
"""
import os
import re

SPACE = os.environ.get('GCHAT_PILOT_SPACE', '')
BOT = os.environ.get('GCHAT_PILOT_BOT_USER', '')
APP = os.environ.get('GCHAT_PILOT_APP_SA', '')
# Per-thread marker of the newest quoted human message (incremental context:
# repeat mentions quote only the delta, keeping per-turn token cost bounded).
_LAST_SEEN = {}
# Attachment refs already handed to a session once; repeat turns skip them so
# the media list doesn't grow with every mention in the same thread.
_FETCHED_REFS = set()
DENIED = 'アプリのスレッド閲覧権限が拒否されたため、@なし投稿を確認できません。内容は推測しません。'
UNKNOWN = 'スレッド全文を確認できないため、内容は推測しません。'
NO_IDENTITY = '専用アプリ名義を確認できないため、読み取りません。'


def eligible(m):
    thread = (m.get('thread') or {}).get('name', '')
    return (bool(re.fullmatch(SPACE + r'/threads/[A-Za-z0-9_-]+', thread))
            and bool(re.fullmatch(SPACE + r'/messages/[A-Za-z0-9_.-]+', m.get('name','')))
            and (m.get('sender') or {}).get('type') == 'HUMAN'
            and 'privateMessageViewer' not in m and not m.get('slashCommand')
            and bool((m.get('argumentText', m.get('text')) or '').strip())
            and any(a.get('type') == 'USER_MENTION' and u.get('type') == 'MENTION'
                    and u.get('user', {}).get('name') == BOT
                    for a in m.get('annotations', []) for u in [a.get('userMention', {})]))


def extract(m, page):
    if not eligible(m):
        return None
    thread = m['thread']['name']
    token = None
    rows_ok = []
    seen = {m['name']}
    try:
        for _ in range(8):
            data = page(thread, token)
            for row in data.get('messages', []):
                name = row.get('name', '')
                if (row.get('thread', {}).get('name') != thread or name in seen
                        or not re.fullmatch(SPACE + r'/messages/[A-Za-z0-9_.-]+', name)
                        or row.get('sender', {}).get('type') != 'HUMAN'
                        or 'privateMessageViewer' in row or not row.get('text')):
                    continue
                seen.add(name)
                rows_ok.append(row)
            token = data.get('nextPageToken')
            if not token:
                break
        else:
            return UNKNOWN
    except PermissionError:
        return DENIED
    except Exception:
        return UNKNOWN
    if not rows_ok:
        return '取得した同一スレッドに、質問以外の人間の本文はありません。'
    # Incremental context (owner decision 2026-11-21): after the first mention
    # in a thread, quote only NEW human messages since the previous turn so
    # per-turn context stays bounded instead of re-attaching the whole excerpt.
    prev = _LAST_SEEN.get(thread)
    if prev:
        def _ts(s):
            import datetime as _dt
            try:
                return _dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
            except Exception:
                return 0.0
        prev_ts = _ts(prev)
        new_rows = [r for r in rows_ok if _ts(r.get('createTime', '')) > prev_ts]
        if not new_rows:
            return '新しい人間の投稿はありません（既読分の文脈はセッションに保持済み）。'
        rows_ok = new_rows
    else:
        # First mention in the thread: quote up to the LAST 100 human
        # messages (owner decision 2026-11-21), char cap below still applies.
        # Quote-replies are covered by the message text itself: Chat embeds
        # the quoted content inline, so an old message quoted from deep in a
        # long thread is visible even when the original is outside the window.
        rows_ok = rows_ok[-100:]
    try:
        def _ts2(s):
            import datetime as _dt
            try:
                return _dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
            except Exception:
                return 0.0
        newest = max(rows_ok, key=lambda r: _ts2(r.get('createTime', '')))
        if _ts2(newest.get('createTime', '')):
            _LAST_SEEN[thread] = newest.get('createTime', '')
    except Exception:
        pass
    # Default: return the WHOLE thread excerpt (owner decision 2026-11-21) —
    # relevance filtering dropped valid messages when the question itself
    # matched best. Extractive only. Neutralize Chat mentions and bound the
    # quoted response.
    import string
    neutral = str.maketrans({c: chr(ord(c)+0xfee0) for c in string.punctuation})
    text = '\n'.join('・' + ' '.join(s.split()).translate(neutral) for s in (r['text'] for r in rows_ok))
    if len(text) > 8000:
        # Over the cap keep the NEWEST part; an old message quoted in the
        # trigger still arrives inline with the quote-reply's own text.
        text = '…（冒頭省略）\n' + text[-8000:]
    return '同じスレッドの人間の投稿（原文抜粋）:\n' + text


def _identity(credentials) -> "str | None":
    """Classify credentials as 'app', 'user' (DWD) or None.

    Only the dedicated bot Service Account is trusted. A token carrying a
    DWD ``subject`` (``google.oauth2.service_account.Credentials`` built
    by dwd.build_dwd_credentials) maps to 'user' and reads history AS the
    delegated person under chat.messages.readonly; the plain app token
    keeps the existing chat.app.messages.readonly path.
    """
    if credentials is None:
        return None
    if getattr(credentials, "service_account_email", None) != APP:
        return None
    if not hasattr(credentials, "with_scopes"):
        return None
    return "user" if getattr(credentials, "_subject", None) else "app"


def answer(m, credentials):
    from google.auth.transport.requests import AuthorizedSession
    kind = _identity(credentials)
    if kind is None:
        return NO_IDENTITY
    if kind == 'user':  # DWD: chat app token as the delegated user.
        scopes = ['https://www.googleapis.com/auth/chat.messages.readonly']
    else:
        scopes = ['https://www.googleapis.com/auth/chat.app.messages.readonly']
    with AuthorizedSession(credentials.with_scopes(scopes)) as session:
        def page(thread, token):
            params = dict(filter='thread.name = ' + thread, pageSize=100,
                          fields='messages(name,thread(name),sender(type),text,createTime,privateMessageViewer),nextPageToken')
            if token:
                params['pageToken'] = token
            r = session.get('https://chat.googleapis.com/v1/' + SPACE + '/messages', params=params,
                            timeout=10, allow_redirects=False)
            if r.status_code in (401,403):
                raise PermissionError
            if r.status_code != 200:
                raise ValueError('list failed')
            return r.json()
        return extract(m, page)


def fetch_attachments(m, credentials, media_urls=None, media_types=None):
    """Best-effort download of the addressed thread's attachments.

    DWD readonly scope provably allows media download (measured 200 on
    2026-11-21). Returns extended (media_urls, media_types) lists of local
    file paths; on any failure returns whatever was collected so far so a
    broken attachment never blocks the text transcript.
    """
    import logging
    import pathlib
    import re as _re
    import tempfile
    from google.auth.transport.requests import AuthorizedSession
    logger = logging.getLogger(__name__)
    kind = _identity(credentials)
    if kind is None:
        return (media_urls or [], media_types or [])
    scopes = ['https://www.googleapis.com/auth/chat.messages.readonly'] if kind == 'user' \
        else ['https://www.googleapis.com/auth/chat.app.messages.readonly']
    thread = (m.get('thread') or {}).get('name', '')
    urls = list(media_urls or [])
    types = list(media_types or [])
    try:
        with AuthorizedSession(credentials.with_scopes(scopes)) as session:
            token = None
            seen_refs = set()
            for _ in range(8):
                params = dict(filter='thread.name = ' + thread, pageSize=100,
                              fields='messages(attachment),nextPageToken')
                if token:
                    params['pageToken'] = token
                r = session.get('https://chat.googleapis.com/v1/' + SPACE + '/messages',
                                params=params, timeout=20)
                if r.status_code != 200:
                    break
                data = r.json()
                for row in data.get('messages', []):
                    for a in (row.get('attachment') or []):
                        ref = (a.get('attachmentDataRef') or {}).get('resourceName')
                        if not ref or ref in seen_refs or ref in _FETCHED_REFS:
                            continue
                        seen_refs.add(ref)
                        safe = _re.sub(r'[^\w.\-\u3040-\u30ff\u4e00-\u9fff]', '_', a.get('contentName') or 'attachment')
                        directory = pathlib.Path(tempfile.gettempdir()) / 'gchat_pilot'
                        directory.mkdir(parents=True, exist_ok=True)
                        path = directory / (thread.rsplit('/', 1)[-1] + '_' + safe)
                        if not path.exists():
                            dl = session.get('https://chat.googleapis.com/v1/media/' + ref + '?alt=media',
                                             timeout=60)
                            if dl.status_code != 200:
                                logger.warning('[GoogleChat] pilot attachment download %s -> %s',
                                               ref, dl.status_code)
                                continue
                            path.write_bytes(dl.content)
                        urls.append(str(path))
                        types.append(a.get('contentType') or 'application/octet-stream')
                token = data.get('nextPageToken')
                if not token:
                    break
    except Exception:
        logger.warning('[GoogleChat] pilot attachment fetch failed', exc_info=True)
    return urls, types
