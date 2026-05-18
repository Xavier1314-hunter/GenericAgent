#!/usr/bin/env python3
"""
Hermes Web UI Proxy Server — Python Implementation (aiohttp + python-socketio)
Replaces Node.js BFF (Koa + Socket.IO).
Bridges Vue.js frontend (ga-web-ui/dist/client/) with GeneraticAgent backend.

Endpoints:
  1. Static file serving: / → dist/client/
  2. Socket.IO /chat-run → proxy to GA backend POST /v1/runs, then SSE /v1/runs/{id}/events
  3. REST /api/ga/* → proxy to GA backend (/api/ga/v1/... → /v1/..., /api/ga/... → /api/...)
  4. /health → 200 OK
"""
import os, sys, json, time, re, asyncio, logging, traceback, uuid, shutil, tempfile
from urllib.parse import urlparse, parse_qs, urlencode

import aiohttp
import socketio
try:
    import ptyprocess
except ImportError:
    ptyprocess = None  # Windows: terminal WebSocket unavailable
from aiohttp import web

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR

# Auto-detect static dir: standalone layout (./dist/client/) or dev layout (./temp/ga-web-ui/dist/client/)
_STATIC_CANDIDATES = [
    os.path.join(SCRIPT_DIR, 'dist', 'client'),       # standalone package
    os.path.join(PROJECT_ROOT, 'temp', 'ga-web-ui', 'dist', 'client'),  # dev
    os.path.join(SCRIPT_DIR, '..', 'dist', 'client'), # fallback
]
STATIC_DIR = None
for _c in _STATIC_CANDIDATES:
    if os.path.isdir(_c):
        STATIC_DIR = _c
        break
if STATIC_DIR is None:
    STATIC_DIR = _STATIC_CANDIDATES[0]  # default to first, will show warning
PORT = int(os.environ.get('PROXY_PORT', '18666'))
UPSTREAM_BASE = os.environ.get('GA_UPSTREAM', 'http://localhost:8642')

# Logging
logging.basicConfig(level=logging.INFO, format='[Proxy] %(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('proxy')

# ── Global HTTP Session (shared to prevent connection leak / event loop stall) ──
HTTP_SESSION: aiohttp.ClientSession = None

# ── Mock Responses for Backend 404s ─────────────────────────────────────────
# Frontend expects these endpoints; return mock empty data when backend lacks them.
# ── Comprehensive Mock Data ──────────────────────────────────────────────────
# These ensure the UI is viewable even when the backend is offline.
MOCK_ENDPOINTS = {
    # Skills
    ('GET', '/api/ga/skills'): {'categories': [], 'archived': []},
    ('PUT', '/api/ga/skills/toggle'): {'success': True},
    ('PUT', '/api/ga/skills/pin'): {'success': True},
    # Usage
    ('GET', '/api/ga/usage'): {
        'sessions': [], 'daily': [], 'models': {},
        'total_input_tokens': 0, 'total_output_tokens': 0,
        'total_sessions': 0, 'total_errors': 0,
        'total_cache_read_tokens': 0, 'total_cache_write_tokens': 0,
    },
    ('GET', '/api/ga/sessions/usage'): {
        'sessions': [], 'daily': [], 'models': {},
        'total_input_tokens': 0, 'total_output_tokens': 0,
        'total_sessions': 0, 'total_errors': 0,
        'total_cache_read_tokens': 0, 'total_cache_write_tokens': 0,
    },
    ('GET', '/api/ga/sessions/context-length'): {'context_length': 4096},
    # Sessions list / conversations
    ('GET', '/api/ga/sessions'): {'sessions': [], 'total': 0},
    ('GET', '/api/ga/sessions/conversations'): {'conversations': []},
    ('POST', '/api/ga/sessions/batch-delete'): {'success': True},
    # Memory
    ('GET', '/api/ga/memory'): {'entries': [], 'stats': {}},
    # Config
    ('GET', '/api/ga/config'): {
        'platforms': {}, 'providers': [], 'model': {},
        'session_reset': {}, 'approvals': {},
    },
    ('GET', '/api/ga/config/providers'): {'providers': []},
    ('GET', '/api/ga/config/model'): {'model': None},
    ('GET', '/api/ga/config/credentials'): {'credentials': []},
    # Models
    ('GET', '/api/ga/available-models'): {'groups': []},
    ('GET', '/v1/models'): {'data': []},
    # Logs
    ('GET', '/api/ga/logs'): {'files': []},
    # Gateways
    ('GET', '/api/ga/gateways'): {'gateways': []},
    ('POST', '/api/ga/gateways'): {'success': True},
    # Auth
    ('GET', '/api/auth/status'): {'hasPasswordLogin': False, 'username': None},
    ('GET', '/api/auth/locked-ips'): {'ips': [], 'locks': {}},
    ('DELETE', '/api/auth/locked-ips'): {'count': 0},
    ('POST', '/api/auth/change-password'): {'success': True},
    ('POST', '/api/auth/change-username'): {'success': True},
    ('POST', '/api/auth/setup'): {'success': True},
    # Files
    ('GET', '/api/ga/files/list'): {'files': [], 'path': '/'},
    ('GET', '/api/ga/files/read'): {'content': '', 'path': ''},
    ('POST', '/api/ga/files/write'): {'success': True},
    ('POST', '/api/ga/files/delete'): {'success': True},
    ('POST', '/api/ga/files/rename'): {'success': True},
    ('POST', '/api/ga/files/mkdir'): {'success': True},
    ('POST', '/api/ga/files/copy'): {'success': True},
    # Profiles
    ('GET', '/api/ga/profiles'): {'profiles': []},
    ('GET', '/api/ga/profiles/active'): {'profile': None},
    ('POST', '/api/ga/profiles/import'): {'success': True},
    # Kanban
    ('GET', '/api/ga/kanban'): {'columns': []},
    ('POST', '/api/ga/kanban'): {'success': True},
    ('GET', '/api/ga/kanban/assignees'): {'assignees': []},
    ('POST', '/api/ga/kanban/complete'): {'success': True},
    ('POST', '/api/ga/kanban/unblock'): {'success': True},
    # Jobs
    ('GET', '/api/ga/jobs'): {'jobs': []},
    # Workspace
    ('GET', '/api/ga/workspace/folders'): {'folders': []},
    # Group chat
    ('GET', '/api/ga/group-chat/rooms'): {'rooms': []},
    # Search
    ('GET', '/api/ga/search/sessions'): {'sessions': [], 'total': 0},
    # TTS
    ('GET', '/api/ga/tts'): {'voices': []},
    # Update
    ('GET', '/api/ga/update'): {'current': '', 'latest': ''},
    # Stats
    ('GET', '/api/ga/usage/stats'): {'stats': {}},
    # Weixin
    ('GET', '/api/ga/weixin/qrcode'): {'qrcode': None},
    ('GET', '/api/ga/weixin/qrcode/status'): {'status': 'unknown'},
    ('POST', '/api/ga/weixin/save'): {'success': True},
    # Terminal
    ('GET', '/api/ga/terminal'): {'available': True},
    # Model context
    ('GET', '/api/ga/model-context/'): {'contexts': []},
    # Cron history
    ('GET', '/api/cron-history'): {'entries': []},
    # Download
    ('GET', '/api/ga/download'): {'files': []},
}
# Regex patterns for dynamic paths (ID-specific endpoints)
MOCK_PATTERNS = [
    (re.compile(r'^GET /api/ga/logs/[^/]+$'), {'entries': []}),
    (re.compile(r'^GET /api/ga/gateways/[^/]+/health$'), {
        'gateway': {'profile': 'default', 'status': 'unknown'},
    }),
    (re.compile(r'^(POST|DELETE) /api/ga/gateways/[^/]+$'), {'success': True}),
    (re.compile(r'^GET /api/ga/skills/[^/]+/files$'), {'files': []}),
    (re.compile(r'^GET /api/ga/skills/[^/]+$'), {'content': ''}),
    (re.compile(r'^GET /api/ga/sessions/[^/]+$'), {'session': None}),
    (re.compile(r'^(POST|DELETE) /api/ga/sessions/[^/]+$'), {'success': True}),
    (re.compile(r'^GET /api/ga/sessions/ga/[^/]+$'), {'response': None}),
    (re.compile(r'^GET /api/ga/profiles/[^/]+$'), {'profile': None}),
    (re.compile(r'^(POST|PUT|DELETE) /api/ga/profiles/[^/]+$'), {'success': True}),
    (re.compile(r'^GET /api/ga/jobs/[^/]+$'), {'job': None}),
    (re.compile(r'^POST /api/ga/jobs/[^/]+$'), {'success': True}),
    (re.compile(r'^GET /api/ga/kanban/[^/]+$'), {'column': None}),
    (re.compile(r'^(POST|PUT|DELETE) /api/ga/kanban/[^/]+$'), {'success': True}),
    (re.compile(r'^GET /api/ga/group-chat/rooms/[^/]+$'), {'room': None}),
    (re.compile(r'^POST /api/ga/group-chat/rooms/[^/]+/join/'), {'joined': True}),
    (re.compile(r'^GET /api/ga/config/providers/[^/]+$'), {'provider': None}),
    (re.compile(r'^GET /api/ga/model-context/[^/]+$'), {'context': {}}),
    (re.compile(r'^GET /api/cron-history/[^/]+$'), {'entries': []}),
]


def get_mock_response(method: str, path: str):
    """Return mock JSON if path matches a missing endpoint."""
    key = (method, path)
    if key in MOCK_ENDPOINTS:
        return MOCK_ENDPOINTS[key]
    key_str = f'{method} {path}'
    for pattern, response in MOCK_PATTERNS:
        if pattern.match(key_str):
            return response
    return None


# ── Helpers ─────────────────────────────────────────────────────────────────

def resolve_profile(headers: dict, query: dict) -> str:
    """Resolve profile name from x-ga-profile header or query param."""
    profile = headers.get('x-ga-profile', '') or query.get('profile', 'default')
    if not profile:
        profile = 'default'
    return profile


def rewrite_upstream_path(path: str) -> str:
    """Rewrite /api/ga/v1/... → /v1/..., /api/ga/... → /api/..."""
    if path.startswith('/api/ga/v1/'):
        return '/v1/' + path[len('/api/ga/v1/'):]
    elif path.startswith('/api/ga/'):
        return '/api/' + path[len('/api/ga/'):]
    return path


def build_proxy_headers(headers: dict, upstream: str) -> dict:
    """Build forwarded headers, removing hop-by-hop headers."""
    out = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ('host', 'origin', 'referer', 'connection', 'authorization', 'transfer-encoding'):
            continue
        if v is None:
            continue
        if isinstance(v, list):
            v = v[0]
        out[k] = str(v)
    # Override host
    out['host'] = urlparse(upstream).hostname or 'localhost'
    return out


# ── Socket.IO Namespace: /chat-run ──────────────────────────────────────────

# In-memory run_id → session_id mapping
run_session_map: dict[str, str] = {}
# In-memory session state (ephemeral)
session_states: dict[str, dict] = {}

sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
chat_run_ns = None  # will be set after app creation


class ChatRunNamespace(socketio.AsyncNamespace):
    """Handles /chat-run Socket.IO namespace."""

    async def on_connect(self, sid, environ, auth=None):
        log.info(f'[sio] connect: {sid}')

    async def on_disconnect(self, sid):
        log.info(f'[sio] disconnect: {sid}')

    async def on_run(self, sid, data):
        """Client emit 'run' → proxy to GA backend POST /v1/runs, then stream SSE events."""
        if not isinstance(data, dict):
            await self.emit('run.failed', {'event': 'run.failed', 'session_id': session_id, 'error': 'Invalid data format'}, to=sid)
            return

        session_id = data.get('session_id') or sid
        input_data = data.get('input', '')
        model = data.get('model', '')

        # Build request body for GA backend
        body = {
            'input': input_data,
            'stream': True,
        }
        if model:
            body['model'] = model

        # Resolve upstream
        profile = data.get('profile', 'default')
        upstream = UPSTREAM_BASE

        # Ensure session state
        state = session_states.setdefault(session_id, {
            'is_working': False,
            'run_id': None,
            'abort_controller': None,
            'queue': [],
        })

        # Queue handling: if already working, queue this run
        if state['is_working']:
            state['queue'].append({'sid': sid, 'data': data, 'profile': profile})
            return

        state['is_working'] = True

        try:
            # Step 1: POST /v1/runs to start the run
            run_url = f'{upstream}/v1/runs'
            headers = {'Content-Type': 'application/json'}

            # Get API key if available
            api_key = os.environ.get('GA_API_KEY', '')
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'

            log.info(f'[sio] POST {run_url} (session={session_id})')

            async with HTTP_SESSION.post(run_url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f'[sio] run failed: {resp.status} {text}')
                    await self.emit('run.failed', {
                        'event': 'run.failed',
                        'session_id': session_id,
                        'error': f'Upstream {resp.status}: {text}',
                        'queue_remaining': len(state['queue']),
                    }, to=sid)
                    state['is_working'] = False
                    return

                run_data = await resp.json()
                run_id = run_data.get('run_id')
                if not run_id:
                    log.error(f'[sio] No run_id in response: {run_data}')
                    await self.emit('run.failed', {
                        'event': 'run.failed',
                        'session_id': session_id,
                        'error': 'No run_id in upstream response',
                        'queue_remaining': len(state['queue']),
                    }, to=sid)
                    state['is_working'] = False
                    return

                # Map run_id → session_id
                run_session_map[run_id] = session_id
                state['run_id'] = run_id

                await self.emit('run.started', {
                    'event': 'run.started',
                    'session_id': session_id,
                    'run_id': run_id,
                    'status': run_data.get('status', 'running'),
                    'queue_length': len(state['queue']),
                }, to=sid)

            # Step 2: Stream SSE events from /v1/runs/{run_id}/events
            events_url = f'{upstream}/v1/runs/{run_id}/events'
            log.info(f'[sio] SSE GET {events_url}')

            async with HTTP_SESSION.get(events_url, headers=headers) as sse_resp:
                if sse_resp.status != 200:
                    log.error(f'[sio] SSE failed: {sse_resp.status}')
                    await self.emit('run.failed', {
                        'event': 'run.failed',
                        'session_id': session_id,
                        'error': f'SSE {sse_resp.status}',
                    }, to=sid)
                    state['is_working'] = False
                    return

                # Parse SSE stream
                buffer = ''
                log.info('[sio] Starting SSE read loop...')
                async for chunk in sse_resp.content.iter_chunked(4096):
                    buffer += chunk.decode('utf-8', errors='replace')
                    # Process complete SSE messages (delimited by \n\n)
                    while '\n\n' in buffer:
                        msg_block, buffer = buffer.split('\n\n', 1)
                        await self._process_sse_message(msg_block, sid, session_id, run_id)

                # Process remaining buffer
                if buffer.strip():
                    await self._process_sse_message(buffer, sid, session_id, run_id)

        except asyncio.CancelledError:
            log.info(f'[sio] run cancelled: {run_id}')
            # Optionally abort upstream
            if run_id:
                try:
                    abort_url = f'{upstream}/v1/runs/{run_id}/cancel'
                    async with HTTP_SESSION.post(abort_url, headers=headers): pass
                except Exception:
                    pass
            await self.emit('abort.completed', {'event': 'abort.completed', 'session_id': session_id, 'run_id': run_id}, to=sid)
        except Exception as e:
            log.error(f'[sio] run error: {traceback.format_exc()}')
            await self.emit('run.failed', {
                'event': 'run.failed',
                'session_id': session_id,
                'error': str(e),
            }, to=sid)
        finally:
            state['is_working'] = False
            state['run_id'] = None

            # Dequeue next if any
            if state['queue']:
                next_run = state['queue'].pop(0)
                asyncio.ensure_future(self.on_run(next_run['sid'], next_run['data']))

    async def on_abort(self, sid, data):
        """Client requests abort of current run."""
        session_id = data.get('session_id') if isinstance(data, dict) else sid
        state = session_states.get(session_id)
        if state and state['run_id']:
            run_id = state['run_id']
            log.info(f'[sio] abort requested: run_id={run_id}')
            await self.emit('abort.started', {'event': 'abort.started', 'session_id': session_id, 'run_id': run_id}, to=sid)
            # We'll handle cancellation via the aiohttp session cancellation
            # For now, emit abort.completed
            await self.emit('abort.completed', {'event': 'abort.completed', 'session_id': session_id, 'run_id': run_id}, to=sid)

    async def on_resume(self, sid, data):
        """Client requests to resume a session."""
        session_id = data.get('session_id') if isinstance(data, dict) else sid
        await self.emit('resumed', {
            'session_id': session_id,
            'messages': [],  # Simplified: no persistent message store
        }, to=sid)

    async def _process_sse_message(self, msg_block: str, sid: str, session_id: str, run_id: str):
        """Process a single SSE message block and relay to client."""
        for line in msg_block.split('\n'):
            line = line.strip()
            if not line.startswith('data: '):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            event = data.get('event', '')
            log.info(f'[sio] _process_sse_message: event={event}, data={json.dumps(data)[:200]}')

            # Map upstream events to Socket.IO events
            if event == 'message.delta':
                delta_text = data.get('delta', '')
                # Try to extract text from JSON delta
                if delta_text.strip().startswith('['):
                    try:
                        parsed = json.loads(delta_text)
                        texts = [b.get('text', '') for b in parsed if isinstance(b, dict) and b.get('type') == 'text']
                        delta_text = ''.join(texts)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Accumulate delta into session state for full response
                state = session_states.setdefault(session_id, {})
                state['delta_buffer'] = state.get('delta_buffer', '') + delta_text
                await self.emit('message.delta', {
                    'event': 'message.delta',
                    'session_id': session_id,
                    'run_id': run_id,
                    'delta': delta_text,
                }, to=sid)

            elif event in ('reasoning.delta', 'thinking.delta'):
                text = data.get('text', data.get('delta', ''))
                await self.emit(event, {
                    'event': event,
                    'session_id': session_id,
                    'run_id': run_id,
                    'text': text,
                }, to=sid)

            elif event == 'tool.started':
                await self.emit('tool.started', {
                    'event': 'tool.started',
                    'session_id': session_id,
                    'run_id': run_id,
                    'tool': data.get('tool', data.get('name', '')),
                    'tool_call_id': data.get('tool_call_id', ''),
                }, to=sid)

            elif event == 'tool.completed':
                output = data.get('output', '')
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                await self.emit('tool.completed', {
                    'event': 'tool.completed',
                    'session_id': session_id,
                    'run_id': run_id,
                    'tool': data.get('tool', data.get('name', '')),
                    'tool_call_id': data.get('tool_call_id', ''),
                    'output': output,
                }, to=sid)

            elif event in ('run.completed', 'run.failed'):
                finish_reason = data.get('finish_reason', 'stop' if event == 'run.completed' else 'error')
                usage = data.get('usage', {})
                # Accumulate full response from delta fragments
                state = session_states.get(session_id, {})
                accumulated = state.get('delta_buffer', '')
                await self.emit('run.completed' if event == 'run.completed' else 'run.failed', {
                    'event': event,
                    'session_id': session_id,
                    'run_id': run_id,
                    'response': accumulated,
                    'finish_reason': finish_reason,
                    'usage': usage,
                    'status': 'completed' if event == 'run.completed' else 'failed',
                }, to=sid)
                # Clear buffer after completion
                if session_id in session_states:
                    session_states[session_id]['delta_buffer'] = ''

            elif event == 'compression.started':
                await self.emit('compression.started', {
                    'event': 'compression.started',
                    'session_id': session_id,
                    'run_id': run_id,
                }, to=sid)

            elif event == 'compression.completed':
                await self.emit('compression.completed', {
                    'event': 'compression.completed',
                    'session_id': session_id,
                    'run_id': run_id,
                    'summary': data.get('summary', ''),
                }, to=sid)

            else:
                # Forward unknown events as generic
                await self.emit('run.delta', {
                    'event': event,
                    'session_id': session_id,
                    'run_id': run_id,
                    **data,
                }, to=sid)


# ── REST Proxy ──────────────────────────────────────────────────────────────

async def proxy_rest_handler(request: web.Request) -> web.Response:
    """Proxy /api/ga/* routes to GA backend."""
    path = request.path
    upstream_path = rewrite_upstream_path(path)
    upstream = UPSTREAM_BASE
    url = f'{upstream}{upstream_path}'

    # Rebuild query string
    query = dict(request.query)
    query.pop('token', None)
    if query:
        url += '?' + urlencode(query)

    # Build headers
    headers = build_proxy_headers(dict(request.headers), upstream)
    api_key = os.environ.get('GA_API_KEY', '')
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    # Read body
    body = None
    if request.method not in ('GET', 'HEAD'):
        try:
            body = await request.read()
        except Exception:
            body = None

    log.info(f'[proxy] {request.method} {path} → {upstream_path}')

    try:
        async with HTTP_SESSION.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
                # Build response
                resp_headers = {}
                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl not in ('transfer-encoding', 'connection', 'content-encoding'):
                        resp_headers[k] = v

                resp_body = await resp.read()

                # ── Response Transformations ──
                if resp_body:
                    try:
                        body_str = resp_body.decode('utf-8')
                        body_json = json.loads(body_str)

                        # 1) /api/available-models: rename 'models' → 'groups' AND flatten {id,label} → id string
                        if upstream_path == '/api/available-models' and 'models' in body_json:
                            body_json['groups'] = body_json.pop('models')
                            for group in body_json.get('groups', []):
                                if isinstance(group.get('models'), list):
                                    group['models'] = [
                                        m['id'] if isinstance(m, dict) else m
                                        for m in group['models']
                                    ]
                            log.info(f'[proxy] transformed /api/available-models: models → groups + flattened')

                        # 2) /api/config: inject empty platform sections if missing
                        if upstream_path == '/api/config':
                            platform_keys = [
                                'telegram', 'discord', 'slack', 'whatsapp',
                                'matrix', 'wecom', 'feishu', 'dingtalk',
                                'weixin', 'platforms', 'approvals',
                            ]
                            modified = False
                            for key in platform_keys:
                                if key not in body_json:
                                    body_json[key] = {}
                                    modified = True
                            # Also ensure session_reset if missing
                            if 'session_reset' not in body_json:
                                body_json['session_reset'] = {}
                                modified = True
                            if modified:
                                log.info(f'[proxy] transformed /api/config: injected empty platform sections')

                        # Update resp_body if changed
                        body_str = json.dumps(body_json, ensure_ascii=False)
                        resp_body = body_str.encode('utf-8')
                        # Update content-length
                        resp_headers['Content-Length'] = str(len(resp_body))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

                # ── Backend 404 → Mock fallback ──
                if resp.status == 404:
                    mock_data = get_mock_response(request.method, path)
                    if mock_data is not None:
                        log.info(f'[proxy] 404 → mock response for {request.method} {path}')
                        resp_headers['Content-Type'] = 'application/json'
                        resp_body = json.dumps(mock_data, ensure_ascii=False).encode('utf-8')
                        resp_headers['Content-Length'] = str(len(resp_body))
                        return web.Response(
                            status=200,
                            headers=resp_headers,
                            body=resp_body,
                        )

                return web.Response(
                    status=resp.status,
                    headers=resp_headers,
                    body=resp_body,
                )
    except aiohttp.ClientError as e:
        log.error(f'[proxy] connection error to {upstream}: {e}')
        # Offline mode: try mock data first, fallback to empty JSON
        mock_data = get_mock_response(request.method, path)
        if mock_data is not None:
            log.info(f'[proxy] offline → mock response for {request.method} {path}')
            return web.json_response(mock_data, status=200)
        # Return empty JSON to prevent frontend crash in standalone UI mode
        log.info(f'[proxy] offline → empty response for {request.method} {path}')
        return web.json_response({}, status=200)


# ── Health Check ────────────────────────────────────────────────────────────

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({'status': 'ok', 'service': 'ga-proxy-python'})


# ── Static File Serving ─────────────────────────────────────────────────────

MIME_TYPES = {
    '.html': 'text/html',
    '.js': 'application/javascript',
    '.css': 'text/css',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.woff2': 'font/woff2',
    '.json': 'application/json',
    '.woff': 'font/woff',
    '.ttf': 'font/ttf',
    '.txt': 'text/plain',
    '.map': 'application/json',
}

async def static_handler(request: web.Request) -> web.Response:
    """Serve static files from dist/client/. Falls back to index.html for SPA routing."""
    path = request.path
    if path == '/':
        path = '/index.html'

    filepath = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip('/')))
    if not filepath.startswith(os.path.normpath(STATIC_DIR)):
        return web.Response(status=403, text='Forbidden')

    if os.path.isfile(filepath):
        ext = os.path.splitext(filepath)[1]
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        try:
            data = open(filepath, 'rb').read()
            return web.Response(body=data, content_type=mime)
        except IOError:
            pass

    # SPA fallback: serve index.html
    index_path = os.path.join(STATIC_DIR, 'index.html')
    if os.path.isfile(index_path):
        try:
            data = open(index_path, 'rb').read()
            return web.Response(body=data, content_type='text/html')
        except IOError:
            pass

    return web.Response(status=404, text='Not Found')


# ── Download Handler ──────────────────────────────────────────────────────────
async def download_handler(request: web.Request) -> web.Response:
    """Serve GA-UI-Standalone.zip for download."""
    zip_path = os.path.join(SCRIPT_DIR, 'temp', 'GA-UI-Standalone.zip')
    if not os.path.isfile(zip_path):
        return web.Response(status=404, text='Not Found')
    try:
        data = open(zip_path, 'rb').read()
        return web.Response(
            body=data,
            content_type='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename="GA-UI-Standalone.zip"',
                'Content-Length': str(len(data)),
            }
        )
    except IOError:
        return web.Response(status=500, text='Server Error')


# ── Upload Handler ───────────────────────────────────────────────────────────
# Frontend sends POST /upload with FormData file fields.
# Returns {"files": [{"name": "...", "path": "...", "media_type": "..."}]}

UPLOAD_DIR = os.path.join(SCRIPT_DIR, 'temp', 'uploads')

async def upload_handler(request: web.Request) -> web.Response:
    """Handle file upload from the frontend. Saves files to temp/uploads/."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    reader = await request.multipart()
    files = []
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name != 'file':
            continue
        # Generate unique filename to avoid collisions
        original_name = field.filename or f'upload_{uuid.uuid4().hex[:8]}'
        ext = os.path.splitext(original_name)[1] or ''
        unique_name = f'{uuid.uuid4().hex}{ext}'
        filepath = os.path.join(UPLOAD_DIR, unique_name)
        # Stream write
        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        # Determine media type from extension
        ext_lower = ext.lower()
        media_type = 'application/octet-stream'
        if ext_lower in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
            media_type = f'image/{ext_lower[1:]}' if ext_lower != '.jpg' else 'image/jpeg'
        files.append({
            'name': original_name,
            'path': filepath,
            'media_type': media_type,
        })
        log.info(f'[upload] saved: {original_name} → {filepath}')
    return web.json_response({'files': files})


# ── Terminal WebSocket (ptyprocess) ────────────────────────────────────────

def _find_shell():
    candidates = [os.environ.get('SHELL'), '/bin/zsh', '/bin/bash', '/bin/sh']
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return '/bin/bash'

def _shell_name(shell):
    return shell.split('/')[-1] or 'shell'

def _gen_id():
    import random, string
    ts = hex(int(time.time() * 1000))[2:]
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return ts + rand

class TerminalSession:
    __slots__ = ('id', 'proc', 'shell', 'pid', 'created_at')
    def __init__(self, sid, proc, shell, pid):
        self.id = sid
        self.proc = proc
        self.shell = shell
        self.pid = pid
        self.created_at = time.time()

async def terminal_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler for /api/ga/terminal — multi-session xterm.js via ptyprocess."""
    if ptyprocess is None:
        return web.json_response({'error': 'Terminal not available on this platform'}, status=503)
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    log.info('[terminal] WebSocket connected')

    shell_path = _find_shell()
    sessions: dict[str, TerminalSession] = {}
    active_id: str | None = None
    buf: dict[str, list[str]] = {}

    def _create_session():
        nonlocal active_id
        sid = _gen_id()
        try:
            proc = ptyprocess.PtyProcess.spawn([shell_path])
        except Exception as e:
            log.error('[terminal] spawn failed: %s', e)
            raise
        sess = TerminalSession(sid, proc, shell_path, proc.pid)
        sessions[sid] = sess
        active_id = sid

        # Reader task: forward pty output to ws (only active session)
        async def _reader():
            loop = asyncio.get_event_loop()
            try:
                while True:
                    try:
                        data = await loop.run_in_executor(None, proc.read, 65536)
                    except (EOFError, OSError):
                        break
                    if not data:
                        break
                    text = data.decode('utf-8', errors='replace')
                    if ws.closed:
                        break
                    if active_id == sess.id:
                        try:
                            await ws.send_str(text)
                        except Exception:
                            break
                    else:
                        b = buf.setdefault(sess.id, [])
                        b.append(text)
                        if len(b) > 5000:
                            b[:] = b[-5000:]
                # Process exited
                if not ws.closed:
                    try:
                        await ws.send_json({'type': 'exited', 'id': sess.id, 'exitCode': proc.exitcode or 0})
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                buf.pop(sess.id, None)
                sessions.pop(sess.id, None)

        asyncio.ensure_future(_reader())
        log.info('[terminal] session created: %s (pid %d)', sess.id, sess.pid)
        return sess

    def _write_raw(data: str):
        if active_id and active_id in sessions:
            try:
                sessions[active_id].proc.write(data.encode('utf-8'))
            except Exception:
                pass

    # Create first session
    try:
        first = _create_session()
        await ws.send_json({
            'type': 'created',
            'id': first.id,
            'pid': first.pid,
            'shell': _shell_name(shell_path),
        })
    except Exception as e:
        await ws.send_json({'type': 'error', 'message': f'Failed to create shell: {e}'})
        await ws.close()
        return ws

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            raw = msg.data
            if not raw:
                continue
            # JSON control message (starts with {)
            if raw and raw[0] == '{':
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    _write_raw(raw)
                    continue
                cmd = parsed.get('type')
                if cmd == 'create':
                    try:
                        sess = _create_session()
                        await ws.send_json({
                            'type': 'created',
                            'id': sess.id,
                            'pid': sess.pid,
                            'shell': _shell_name(shell_path),
                        })
                    except Exception as e:
                        await ws.send_json({'type': 'error', 'message': str(e)})
                elif cmd == 'switch':
                    sid = parsed.get('sessionId')
                    if sid in sessions:
                        active_id = sid
                        await ws.send_json({'type': 'switched', 'id': sid})
                        # Flush buffered output
                        b = buf.pop(sid, None)
                        if b:
                            for chunk in b:
                                try:
                                    await ws.send_str(chunk)
                                except Exception:
                                    break
                    else:
                        await ws.send_json({'type': 'error', 'message': 'Session not found'})
                elif cmd == 'close':
                    sid = parsed.get('sessionId')
                    if sid in sessions:
                        try:
                            sessions[sid].proc.close()
                        except Exception:
                            pass
                        sessions.pop(sid, None)
                        buf.pop(sid, None)
                        if active_id == sid:
                            remaining = list(sessions.keys())
                            active_id = remaining[0] if remaining else None
                        log.info('[terminal] session closed: %s', sid)
                elif cmd == 'resize':
                    if active_id and active_id in sessions:
                        cols = max(1, parsed.get('cols', 0))
                        rows = max(1, parsed.get('rows', 0))
                        try:
                            sessions[active_id].proc.setwinsize(rows, cols)
                        except Exception:
                            pass
                else:
                    _write_raw(raw)
            else:
                _write_raw(raw)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            log.error('[terminal] ws error: %s', ws.exception())

    # Cleanup on disconnect
    for s in list(sessions.values()):
        try:
            s.proc.close()
        except Exception:
            pass
    sessions.clear()
    buf.clear()
    log.info('[terminal] WebSocket disconnected, all sessions cleaned up')
    return ws


# ── App Creation ────────────────────────────────────────────────────────────

async def _init_session(app):
    """Initialize global HTTP session on app startup."""
    global HTTP_SESSION
    HTTP_SESSION = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        connector=aiohttp.TCPConnector(limit=50, limit_per_host=20, enable_cleanup_closed=True),
    )
    log.info('[session] Global HTTP session created')

async def _cleanup_session(app):
    """Close global HTTP session on app shutdown."""
    global HTTP_SESSION
    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()
        log.info('[session] Global HTTP session closed')

def create_app() -> web.Application:
    app = web.Application()

    # Global session lifecycle
    app.on_startup.append(_init_session)
    app.on_cleanup.append(_cleanup_session)

    # Socket.IO
    sio.attach(app)

    # Register Socket.IO /chat-run namespace
    global chat_run_ns
    chat_run_ns = ChatRunNamespace('/chat-run')
    sio.register_namespace(chat_run_ns)

    # Routes
    app.router.add_get('/health', health_handler)

    # Download endpoint: server GA-UI-Standalone.zip
    app.router.add_get('/download/ga-ui-standalone', download_handler)

    # Upload endpoint (must be before static catch-all)
    app.router.add_post('/upload', upload_handler)

    # REST proxy routes (catches /api/ga/*, /api/*, and /v1/*)
    if ptyprocess is not None:
        app.router.add_get('/api/ga/terminal', terminal_ws_handler)
    app.router.add_route('*', '/api/ga/{tail:.*}', proxy_rest_handler)
    app.router.add_route('*', '/api/{tail:.*}', proxy_rest_handler)
    app.router.add_route('*', '/v1/{tail:.*}', proxy_rest_handler)

    # Static files (catch-all)
    app.router.add_get('/{tail:.*}', static_handler)

    return app


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(os.path.join(SCRIPT_DIR, 'temp'), exist_ok=True)

    if not os.path.isdir(STATIC_DIR):
        log.warning(f'Static dir not found: {STATIC_DIR}')
        log.warning(f'Create symlink or copy ga-web-ui/dist/client to {STATIC_DIR}')

    log.info(f'Starting proxy on http://0.0.0.0:{PORT}/')
    log.info(f'Static: {STATIC_DIR}')
    log.info(f'Upstream: {UPSTREAM_BASE}')

    app = create_app()
    web.run_app(app, host='0.0.0.0', port=PORT, access_log=None)