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
    return ('同じスレッドの人間の投稿（原文抜粋）:\n' + text
            + '\n\n（表示メモ: Chatの返信はMarkdown対応。過去発言の一部を引用して答えるときは '
              '`> 引用文` のブロック引用を1〜3行・必要なら発言者名付きで使ってよい。'
              '使用は状況判断に任せる。全メッセージ単位の引用返信はAPIの quotedMessageMetadata で可能。'
              'ファイルや画像・動画を添付して返すときは gchat-attach-out スキルの'
              'アップロードスクリプトでDriveに置いてリンクを返せる。）')


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


def _tmp_cleanup() -> None:
    """Delete downloaded attachment files older than the TTL (default 7 days).

    Owner decision 2026-11-22: attachments are consumed by the agent turn and
    re-downloaded on demand (``path.exists()`` check below), so old files are
    pure disk pressure. GCHAT_PILOT_TMP_TTL_HOURS overrides the default 168.
    """
    import logging
    import pathlib
    import tempfile
    import time as _time
    _logger = logging.getLogger(__name__)
    try:
        ttl_hours = float(os.environ.get("GCHAT_PILOT_TMP_TTL_HOURS", "168") or "168")
    except ValueError:
        ttl_hours = 168.0
    if ttl_hours <= 0:
        return
    directory = pathlib.Path(tempfile.gettempdir()) / "gchat_pilot"
    if not directory.is_dir():
        return
    cutoff = _time.time() - ttl_hours * 3600.0
    try:
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        _logger.warning("[GoogleChat] pilot tmp cleanup failed", exc_info=True)


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
        _tmp_cleanup()
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


# Mainstream-style attachment pre-processing (2026-11-21, owner decision:
# "みんながやってるやり方を参考に" — ChatGPT/OpenAI style = text layer + page
# images passed directly to the vision model, no OCR engine in between).
# Limits keep the turn cheap: a video turn once ran 21 min / 46 iterations
# because the agent extracted frames itself; pre-extracting here bounds it.
_PDF_TEXT_CAP = 3500
_PDF_PAGES_WITH_TEXT = 2
_PDF_PAGES_NO_TEXT = 4
_PDF_ZOOM = 2.0
_VIDEO_FRAMES = 3


def prepare_attachment_media(paths, types):
    """Best-effort mainstream pre-processing of downloaded attachments.

    Returns ``(extra_paths, extra_types, extra_text, audio_paths)``:
    - PDF  → extracted text layer (capped) + first pages rendered to PNG
             (images ride the normal inline-vision routing);
    - video→ evenly-spaced preview frames as PNG + an extracted WAV for the
             caller's STT step;
    - anything else / any failure → empty extras (never blocks the turn).
    """
    import logging
    import pathlib
    import subprocess
    logger = logging.getLogger(__name__)
    extra_paths, extra_types, audio_paths = [], [], []
    texts = []
    for path, mtype in zip(paths, types or []):
        p = pathlib.Path(path)
        if not p.is_file():
            continue
        mtype = mtype or ''
        try:
            if mtype == 'application/pdf' or p.suffix.lower() == '.pdf':
                _prepare_pdf(p, extra_paths, extra_types, texts)
            elif mtype.startswith('video/') or p.suffix.lower() == '.mp4':
                _prepare_video(p, extra_paths, extra_types, audio_paths, texts)
        except Exception:
            logger.warning('[GoogleChat] pilot attachment pre-process failed: %s',
                           p.name, exc_info=True)
    text_block = ''
    if texts:
        text_block = ('[添付ファイルの前処理結果（テキスト層抽出）—— 推測で補わず'
                      'この内容を踏まえて答えてください]\n' + '\n---\n'.join(texts))
    return extra_paths, extra_types, text_block, audio_paths


def _ffmpeg_bin(name):
    import shutil
    found = shutil.which(name)
    return found or name


def _prepare_pdf(p, extra_paths, extra_types, texts):
    import pymupdf
    doc = pymupdf.open(str(p))
    text_parts = []
    page_limit = min(doc.page_count, _PDF_PAGES_NO_TEXT)
    try:
        for i in range(min(doc.page_count, 8)):
            t = (doc[i].get_text() or '').strip()
            if t:
                text_parts.append(t)
        has_text = bool(' '.join(text_parts).strip())
        limit = min(doc.page_count, _PDF_PAGES_WITH_TEXT if has_text else _PDF_PAGES_NO_TEXT)
        page_limit = limit
        for i in range(limit):
            pix = doc[i].get_pixmap(matrix=pymupdf.Matrix(_PDF_ZOOM, _PDF_ZOOM))
            out = p.with_name(f'{p.stem}_page{i + 1}.png')
            pix.save(str(out))
            extra_paths.append(str(out))
            extra_types.append('image/png')
        if text_parts:
            joined = '\n\n'.join(text_parts).strip()
            if len(joined) > _PDF_TEXT_CAP:
                joined = joined[:_PDF_TEXT_CAP] + '…（以下省略）'
            texts.append(f'{p.name}（テキスト層あり・{doc.page_count}ページ）:\n{joined}')
        elif doc.page_count:
            texts.append(f'{p.name}（テキスト層なし・{doc.page_count}ページ・ページ画像を同梱）')
    finally:
        doc.close()
    _ = page_limit


def _prepare_video(p, extra_paths, extra_types, audio_paths, texts):
    import json
    import subprocess
    probe = subprocess.run(
        [_ffmpeg_bin('ffprobe'), '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type', '-of', 'json', str(p)],
        capture_output=True, timeout=30)
    duration, has_audio = 0.0, False
    if probe.returncode == 0 and probe.stdout:
        info = json.loads(probe.stdout.decode('utf-8', 'replace') or '{}')
        try:
            duration = float((info.get('format') or {}).get('duration') or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        has_audio = any((s.get('codec_type') == 'audio')
                        for s in info.get('streams') or [])
    base = _ffmpeg_bin('ffmpeg')
    count = _VIDEO_FRAMES if duration > 0 else 1
    for i in range(count):
        seek = max(0.0, min(duration * (i + 0.5) / count if duration > 0 else 0.0,
                            max(0.0, duration - 0.5)))
        out = p.with_name(f'{p.stem}_frame{i + 1}.png')
        r = subprocess.run(
            [base, '-y', '-ss', f'{seek:.2f}', '-i', str(p), '-frames:v', '1',
             '-q:v', '3', str(out)],
            capture_output=True, timeout=30)
        if r.returncode == 0 and out.is_file():
            extra_paths.append(str(out))
            extra_types.append('image/png')
    if has_audio:
        wav = p.with_name(f'{p.stem}_audio.wav')
        r = subprocess.run(
            [base, '-y', '-i', str(p), '-vn', '-ac', '1', '-ar', '16000',
             str(wav)],
            capture_output=True, timeout=60)
        if r.returncode == 0 and wav.is_file() and wav.stat().st_size > 0:
            audio_paths.append(str(wav))
    texts.append(f'{p.name}（動画・{duration:.1f}秒・プレビューフレーム{count}枚を同梱'
                 + ('・音声トラックあり（文字起こしは別添付のaudio.wav）' if has_audio else '・音声なし') + '）')
