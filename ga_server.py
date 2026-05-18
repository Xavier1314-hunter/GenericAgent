"""
GA REST API Server - 兼容 proxy_server.py 的 /v1/runs SSE 协议
监听 8642 端口，将 Web 请求转化为 GeneraticAgent.put_task() 调用
"""
import sys, os, json, uuid, asyncio, threading, logging, traceback, time, sqlite3, hashlib, queue
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web
from agentmain import GeneraticAgent


# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('GA_DB_PATH', os.path.join(SCRIPT_DIR, 'temp', 'ga_data.db'))
GA_API_KEY = os.environ.get('GA_API_KEY', '')  # If set, enables token auth

# Logging
logging.basicConfig(level=logging.INFO, format='[GA] %(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ga-server')

# ── Agent Instance ──────────────────────────────────────────────────────────
agent = GeneraticAgent()
agent.daemon = True  # Allow thread exit

# Start background run loop
_agent_thread = threading.Thread(target=agent.run, daemon=True, name='ga-agent-run')
_agent_thread.start()
log.info('Agent run loop started via put_task()')

# ── SQLite Database ─────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_conn = None
_conn_lock = threading.Lock()

def get_db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_db()
    return _conn

def _init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'web',
            model TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            last_active REAL NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            run_id TEXT,
            created_at REAL NOT NULL,
            attachments_json TEXT DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);
        CREATE TABLE IF NOT EXISTS memory (
            key TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL
        );
    """)
    conn.commit()

def db_save_user_message(session_id: str, content: str, run_id: str = '', attachments_json: str = '') -> None:
    conn = get_db()
    with _conn_lock:
        conn.execute(
            'INSERT INTO messages (session_id, role, content, run_id, created_at, attachments_json) VALUES (?, ?, ?, ?, ?, ?)',
            (session_id, 'user', content, run_id, time.time(), attachments_json)
        )
        # Increment & ensure session exists
        conn.execute('UPDATE sessions SET message_count = message_count + 1, last_active = ? WHERE id = ?',
                     (time.time(), session_id))
        conn.commit()

def db_save_assistant_message(session_id: str, content: str, run_id: str) -> None:
    conn = get_db()
    with _conn_lock:
        conn.execute(
            'INSERT INTO messages (session_id, role, content, run_id, created_at) VALUES (?, ?, ?, ?, ?)',
            (session_id, 'assistant', content, run_id, time.time())
        )
        conn.execute('UPDATE sessions SET message_count = message_count + 1, last_active = ? WHERE id = ?',
                     (time.time(), session_id))
        conn.commit()

def db_update_assistant_message(session_id: str, content: str, run_id: str) -> None:
    """Update the last assistant message for a run_id (streaming deltas)."""
    conn = get_db()
    with _conn_lock:
        row = conn.execute(
            'SELECT id, content FROM messages WHERE session_id = ? AND role = ? AND run_id = ? ORDER BY id DESC LIMIT 1',
            (session_id, 'assistant', run_id)
        ).fetchone()
        if row:
            conn.execute('UPDATE messages SET content = ? WHERE id = ?', (content, row['id']))
        else:
            conn.execute(
                'INSERT INTO messages (session_id, role, content, run_id, created_at) VALUES (?, ?, ?, ?, ?)',
                (session_id, 'assistant', content, run_id, time.time())
            )
        conn.commit()

def db_ensure_session(session_id: str, title: str = '', model: str = '', started_at: float = None) -> None:
    conn = get_db()
    now = started_at or time.time()
    with _conn_lock:
        row = conn.execute('SELECT id FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if not row:
            conn.execute(
                'INSERT INTO sessions (id, title, source, model, started_at, last_active, message_count) VALUES (?, ?, ?, ?, ?, ?, 0)',
                (session_id, title, 'web', model, now, now)
            )
        else:
            conn.execute('UPDATE sessions SET last_active = ?, title = ? WHERE id = ?', (now, title, session_id))
        conn.commit()

def db_get_messages(session_id: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        'SELECT role, content, run_id, created_at, attachments_json FROM messages WHERE session_id = ? ORDER BY id ASC',
        (session_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def db_get_session_detail(session_id: str) -> dict | None:
    """Fetch a single session with its messages."""
    conn = get_db()
    row = conn.execute(
        'SELECT id, title, source, model, started_at, last_active, message_count FROM sessions WHERE id = ?',
        (session_id,)
    ).fetchone()
    if not row:
        return None
    s = dict(row)
    s['messages'] = db_get_messages(session_id)
    return s


def db_get_sessions() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        'SELECT id, title, source, model, started_at, last_active, message_count FROM sessions ORDER BY last_active DESC'
    ).fetchall()
    return [dict(r) for r in rows]

def db_get_memory(key: str) -> str:
    conn = get_db()
    row = conn.execute('SELECT content FROM memory WHERE key = ?', (key,)).fetchone()
    return row[0] if row else ''

def db_save_memory(key: str, content: str) -> None:
    conn = get_db()
    now = time.time()
    with _conn_lock:
        conn.execute(
            'INSERT OR REPLACE INTO memory (key, content, mtime) VALUES (?, ?, ?)',
            (key, content, now)
        )
        conn.commit()


# ── Token Auth Middleware ───────────────────────────────────────────────────
@web.middleware
async def token_auth_middleware(request: web.Request, handler):
    if not GA_API_KEY:
        return await handler(request)
    path = request.path
    if path in ('/', '/health') or path.startswith('/v1/'):
        return await handler(request)
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        if token == GA_API_KEY or hashlib.sha256(token.encode()).hexdigest() == GA_API_KEY:
            return await handler(request)
    return web.json_response(
        {'error': {'message': 'Unauthorized: invalid or missing API key'}},
        status=401
    )


# ── In-memory session state (legacy compat) ─────────────────────────────────
sessions: dict[str, dict] = {}

def auto_title(input_text: str, session_id: str = '') -> str:
    text = input_text.strip()[:80]
    if len(input_text) > 80:
        return text + '...'
    return text

def _save_sessions():
    try:
        path = os.path.join(SCRIPT_DIR, 'temp', '.ga_sessions.json')
        data = {
            sid: {k: s[k] for k in ('id', 'title', 'source', 'model', 'started_at', 'ended_at', 'last_active', 'message_count', 'preview') if k in s}
            for sid, s in sessions.items()
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f'save sessions failed: {e}')

def _load_sessions():
    try:
        path = os.path.join(SCRIPT_DIR, 'temp', '.ga_sessions.json')
        with open(path) as f:
            data = json.load(f)
        for sid, s in data.items():
            sessions[sid] = s
    except (FileNotFoundError, json.JSONDecodeError):
        pass


# ── SSE Stream Buffer ───────────────────────────────────────────────────────
_stream_buffers: dict[str, dict] = {}

def _consume_queue(session_id: str, run_id: str, q: queue.Queue):
    """
    Background thread: consumes display_queue from put_task().
    Fills _stream_buffers[run_id] with text/reasoning/done.
    """
    buf = _stream_buffers.get(run_id)
    if not buf:
        log.error(f'[consume] no buffer for run_id={run_id}')
        return

    log.info(f'[consume] started: session={session_id} run_id={run_id}')
    full_text = ''
    try:
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                # Check if still alive
                if buf.get('cancelled'):
                    log.info(f'[consume] cancelled: run_id={run_id}')
                    break
                continue

            if 'done' in item:
                full_text = item['done']
                buf['text'] = full_text
                buf['done'] = True
                log.info(f'[consume] done: run_id={run_id} len={len(full_text)}')
                # Save to SQLite
                if full_text:
                    db_save_assistant_message(session_id, full_text, run_id)
                break
            elif 'next' in item:
                full_text = item['next']
                buf['text'] = full_text
                buf['last_updated'] = time.time()
            elif 'error' in item:
                log.error(f'[consume] error: {item["error"]}')
                buf['done'] = True
                break
    except Exception as e:
        log.error(f'[consume] exception: {traceback.format_exc()}')
        buf['text'] = full_text or str(e)
        buf['done'] = True


# ── API Handlers ────────────────────────────────────────────────────────────

async def handle_post_run(request: web.Request) -> web.Response:
    """
    POST /v1/runs
    {"input": "...", "stream": true, "session_id": "..."}
    → Starts a run, returns run_id
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    input_text = body.get('input', '')
    session_id = body.get('session_id', '')
    model = body.get('model', '')
    
    # Handle array input from frontend (text + images format)
    # e.g. [{"type":"text","text":"hello"}, {"type":"image","name":"x.png","path":"/tmp/..."}]
    attachments_json = ''
    if isinstance(input_text, list):
        # Save original content blocks as attachments_json before flattening
        attachments_json = json.dumps(input_text, ensure_ascii=False)
        text_parts = []
        for item in input_text:
            if isinstance(item, dict) and item.get('type') == 'text':
                text_parts.append(item.get('text', ''))
            elif isinstance(item, dict) and item.get('type') == 'image':
                img_path = item.get('path', '')
                if img_path and os.path.exists(img_path):
                    try:
                        from memory.ocr_utils import ocr_image
                        ocr_result = ocr_image(img_path)
                        ocr_text = ocr_result.get('text', '').strip()
                        if ocr_text:
                            text_parts.append(f"[截图内容]:\n{ocr_text}")
                            log.info(f'[ocr] extracted {len(ocr_text)} chars from {img_path}')
                    except Exception as e:
                        log.warning(f'[ocr] failed on {img_path}: {e}')
        input_text = '\n'.join(text_parts)
    
    if not input_text:
        return web.json_response({'error': 'input is required'}, status=400)
    
    if not session_id:
        session_id = str(uuid.uuid4())
    
    now = time.time()
    
    # Ensure session
    title = auto_title(input_text, session_id)
    db_ensure_session(session_id, title=title, model=model, started_at=now)
    
    if session_id not in sessions:
        sessions[session_id] = {
            'id': session_id,
            'source': 'web',
            'model': model or 'MiniMax-M2.7',
            'title': title,
            'preview': input_text.strip()[:200],
            'started_at': now,
            'ended_at': None,
            'last_active': now,
            'message_count': 1,
            'tool_call_count': 0,
        }
        log.info(f'[session] created: {session_id} title="{title}"')
    else:
        s = sessions[session_id]
        s['last_active'] = now
        s['message_count'] += 1
    _save_sessions()
    
    run_id = str(uuid.uuid4())
    
    # Store user message
    db_save_user_message(session_id, input_text, run_id, attachments_json)
    
    # Initialize stream buffer
    _stream_buffers[run_id] = {
        'session_id': session_id,
        'text': '',
        'reasoning': '',
        'done': False,
        'last_updated': now,
        'tool_call_count': 0,
    }
    
    log.info(f'[run] start: run_id={run_id} session={session_id}')
    
    # Submit task to agent via put_task()
    try:
        display_queue = agent.put_task(query=input_text, source='web')
        # Start consumer thread
        t = threading.Thread(
            target=_consume_queue,
            args=(session_id, run_id, display_queue),
            daemon=True,
            name=f'consume-{run_id[:8]}'
        )
        t.start()
    except Exception as e:
        log.error(f'[run] put_task failed: {traceback.format_exc()}')
        _stream_buffers.pop(run_id, None)
        return web.json_response({'error': str(e)}, status=500)

    return web.json_response({
        'run_id': run_id,
        'status': 'running',
        'session_id': session_id,
    })


async def handle_get_events(request: web.Request) -> web.Response:
    """
    GET /v1/runs/{run_id}/events
    SSE stream of run events
    """
    run_id = request.match_info.get('run_id', '')
    buf = _stream_buffers.get(run_id)
    
    if not buf:
        return web.json_response({'error': 'Run not found'}, status=404)
    
    log.info(f'[sse] start: run_id={run_id}')
    
    async def event_stream():
        # Send initial run.started
        yield f'data: {json.dumps({"event": "run.started", "run_id": run_id, "status": "running", "session_id": buf["session_id"]})}\n\n'
        
        last_text_len = 0
        start_time = time.time()
        
        while not buf['done']:
            current_text = buf['text']
            
            if len(current_text) > last_text_len:
                delta = current_text[last_text_len:]
                last_text_len = len(current_text)
                yield f'data: {json.dumps({"event": "message.delta", "run_id": run_id, "delta": json.dumps([{"type": "text", "text": delta}]) })}\n\n'
            
            # Timeout after 5 minutes
            if time.time() - start_time > 300:
                log.warning(f'[sse] timeout: run_id={run_id}')
                break
            
            await asyncio.sleep(0.05)
        
        # Flush remaining
        if len(buf['text']) > last_text_len:
            delta = buf['text'][last_text_len:]
            yield f'data: {json.dumps({"event": "message.delta", "run_id": run_id, "delta": json.dumps([{"type": "text", "text": delta}]) })}\n\n'
        
        # Send completion
        finish_reason = 'stop' if buf['done'] else 'timeout'
        yield f'data: {json.dumps({"event": "run.completed", "run_id": run_id, "status": "completed", "finish_reason": finish_reason})}\n\n'
        
        # Update session preview
        s = sessions.get(buf['session_id'])
        if s and buf['text']:
            s['preview'] = buf['text'].strip()[:200]
            _save_sessions()
        
        # Cleanup
        _stream_buffers.pop(run_id, None)
        log.info(f'[sse] done: run_id={run_id}')
    
    resp = web.StreamResponse(status=200, reason='OK', headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    })
    await resp.prepare(request)
    
    try:
        async for chunk in event_stream():
            await resp.write(chunk.encode('utf-8'))
            await asyncio.sleep(0)
    except (ConnectionResetError, ConnectionAbortedError):
        log.info(f'[sse] client disconnected: run_id={run_id}')
    except asyncio.CancelledError:
        pass
    
    return resp


async def handle_cancel_run(request: web.Request) -> web.Response:
    """POST /v1/runs/{run_id}/cancel"""
    run_id = request.match_info.get('run_id', '')
    buf = _stream_buffers.pop(run_id, None)
    if buf:
        buf['cancelled'] = True
        buf['done'] = True
        log.info(f'[run] cancelled: run_id={run_id}')
    return web.json_response({'run_id': run_id, 'status': 'cancelled'})


async def handle_get_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions - list all sessions"""
    try:
        result = db_get_sessions()
        return web.json_response(result)
    except Exception as e:
        log.error(f'Error listing sessions: {e}')
        return web.json_response([], status=200)


async def handle_get_session(request: web.Request) -> web.Response:
    """GET /api/sessions/{session_id} - detailed session with messages"""
    session_id = request.match_info.get('session_id', '')
    if not session_id:
        return web.json_response({'error': 'session_id required'}, status=400)
    try:
        detail = db_get_session_detail(session_id)
        if not detail:
            return web.json_response({'session': None}, status=404)
        return web.json_response({'session': detail})
    except Exception as e:
        log.error(f'Error getting session detail: {e}')
        return web.json_response({'session': None}, status=200)


async def handle_get_session_messages(request: web.Request) -> web.Response:
    """GET /api/sessions/{session_id}/messages - get all messages"""
    session_id = request.match_info.get('session_id', '')
    if not session_id:
        return web.json_response({'error': 'session_id required'}, status=400)
    try:
        messages = db_get_messages(session_id)
        return web.json_response(messages)
    except Exception as e:
        log.error(f'Error getting messages: {e}')
        return web.json_response([], status=200)


# ── Legacy API Handlers ─────────────────────────────────────────────────────

async def handle_api_files_list(request: web.Request) -> web.Response:
    return web.json_response([])

async def handle_api_context_length(request: web.Request) -> web.Response:
    return web.json_response({'length': 128000})

async def handle_api_update(request: web.Request) -> web.Response:
    return web.json_response({'status': 'ok'})

async def handle_api_available_models(request: web.Request) -> web.Response:
    """前端初始化需要：列出可用模型"""
    models = [
        {"provider": "minimax", "label": "MiniMax", "models": [
            {"id": "MiniMax-M2.7", "label": "MiniMax-M2.7"}
        ]},
        {"provider": "openai", "label": "OpenAI", "models": [
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4o-mini", "label": "GPT-4o-mini"}
        ]},
        {"provider": "claude", "label": "Anthropic", "models": [
            {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"}
        ]}
    ]
    return web.json_response({"models": models})

async def handle_api_config(request: web.Request) -> web.Response:
    """前端初始化需要：系统配置"""
    config = {
        "display": {"streaming": True, "show_cost": True, "compact": True},
        "agent": {"max_turns": 50, "gateway_timeout": 120},
        "memory": {"memory_enabled": True, "memory_char_limit": 8000},
        "privacy": {"redact_pii": False}
    }
    return web.json_response(config)

async def handle_api_profiles(request: web.Request) -> web.Response:
    """前端初始化需要：用户配置列表"""
    profiles = [
        {"name": "default", "active": True, "model": "MiniMax-M2.7", "gateway": "", "alias": "Default"}
    ]
    return web.json_response({"profiles": profiles})

async def handle_get_memory(request: web.Request) -> web.Response:
    """GET /api/memory — 返回所有记忆分区"""
    keys = ['memory', 'user', 'soul']
    data = {}
    for k in keys:
        data[k] = db_get_memory(k)
        row = get_db().execute('SELECT mtime FROM memory WHERE key = ?', (k,)).fetchone()
        data[f'{k}_mtime'] = row[0] if row else None
    return web.json_response(data)

async def handle_save_memory(request: web.Request) -> web.Response:
    """POST /api/memory — 保存单个分区"""
    body = await request.json()
    section = body.get('section', '')
    content = body.get('content', '')
    if section not in ('memory', 'user', 'soul'):
        return web.json_response({'error': 'Invalid section'}, status=400)
    db_save_memory(section, content)
    return web.json_response({'success': True})

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({'status': 'ok', 'service': 'ga-server'})


# ── App ─────────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application(middlewares=[token_auth_middleware])
    _load_sessions()
    
    # v1 endpoints
    app.router.add_post('/v1/runs', handle_post_run)
    app.router.add_get('/v1/runs/{run_id}/events', handle_get_events)
    app.router.add_post('/v1/runs/{run_id}/cancel', handle_cancel_run)
    
    # API endpoints
    app.router.add_get('/api/sessions', handle_get_sessions)
    app.router.add_get('/api/sessions/{session_id}', handle_get_session)
    app.router.add_get('/api/sessions/{session_id}/messages', handle_get_session_messages)
    
    # Legacy compat
    app.router.add_get('/api/files/list', handle_api_files_list)
    app.router.add_get('/api/context-length', handle_api_context_length)
    app.router.add_post('/api/update', handle_api_update)
    
    # Frontend required endpoints (UI won't load without these)
    app.router.add_get('/api/available-models', handle_api_available_models)
    app.router.add_get('/api/config', handle_api_config)
    app.router.add_get('/api/profiles', handle_api_profiles)
    
    # Memory persistence (frontend MemoryView)
    app.router.add_get('/api/memory', handle_get_memory)
    app.router.add_post('/api/memory', handle_save_memory)
    
    # Health
    app.router.add_get('/health', health_handler)
    
    log.info('GA Server starting on http://0.0.0.0:8642')
    return app


def main():
    os.makedirs(os.path.join(SCRIPT_DIR, 'temp'), exist_ok=True)
    get_db()
    log.info(f'DB path: {DB_PATH}')
    if GA_API_KEY:
        log.info('Token auth: enabled')
    else:
        log.info('Token auth: disabled (set GA_API_KEY to enable)')
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=8642, handle_signals=True)


if __name__ == '__main__':
    main()