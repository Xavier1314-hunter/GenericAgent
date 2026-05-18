"""
Streamlit WebUI for GenericAgent (Multimodal)
- 支持 Ctrl+V 粘贴截图/图片
- 支持拖拽图片到输入区
- 支持右键复制/截图后直接粘贴
- 保留 /new, /stop, /help, /status, /llm 等命令
"""

import os, sys, subprocess
from urllib.request import urlopen
from urllib.parse import quote
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
try: sys.stdout.reconfigure(errors='replace')
except: pass
try: sys.stderr.reconfigure(errors='replace')
except: pass
script_dir = os.path.dirname(__file__)
sys.path.append(os.path.abspath(os.path.join(script_dir, '..')))
sys.path.append(os.path.abspath(script_dir))

import streamlit as st
import time, json, re, threading, queue, base64, io
from agentmain import GeneraticAgent
import chatapp_common  # activate /continue command (monkey patches GeneraticAgent)
from continue_cmd import handle_frontend_command, reset_conversation, list_sessions, extract_ui_messages

st.set_page_config(page_title="Cowork", layout="wide")

@st.cache_resource
def init():
    agent = GeneraticAgent()
    if agent.llmclient is None:
        st.error("⚠️ 未配置任何可用的 LLM 接口，请设置mykey.py。")
        st.stop()
    else: threading.Thread(target=agent.run, daemon=True).start()
    return agent

agent = init()

# ====== 头像 + 标题区域 ======
if 'avatar_b64' not in st.session_state:
    st.session_state.avatar_b64 = None  # None=使用默认Logo

# 默认头像SVG (GA Logo风格)
_DEFAULT_AVATAR_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="40" height="40">
<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#667eea"/><stop offset="100%" style="stop-color:#764ba2"/></linearGradient></defs>
<circle cx="50" cy="50" r="48" fill="url(#g)"/>
<text x="50" y="62" text-anchor="middle" font-size="36" fill="white" font-weight="bold">GA</text>
</svg>'''

avatar_html = st.session_state.avatar_b64 or _DEFAULT_AVATAR_SVG
# 如果是base64图片，包装成img；否则用SVG
if st.session_state.avatar_b64:
    avatar_display = f'<img src="{st.session_state.avatar_b64}" style="width:40px;height:40px;border-radius:50%;object-fit:cover;cursor:pointer;" onclick="document.getElementById(\'avatar-upload-input\').click()" />'
else:
    avatar_display = f'<div style="width:40px;height:40px;cursor:pointer;" onclick="document.getElementById(\'avatar-upload-input\').click()">{_DEFAULT_AVATAR_SVG}</div>'

col1, col2 = st.columns([0.6, 9.4])
with col1:
    st.markdown(f'<div style="display:flex;align-items:center;height:100%;padding-top:8px;">{avatar_display}</div>', unsafe_allow_html=True)
with col2:
    st.title("🖥️ Cowork")

# 隐藏的文件上传器（点击头像触发）
avatar_file = st.file_uploader("更换头像", type=["png","jpg","jpeg","gif","svg"], key="avatar_uploader", label_visibility="collapsed")
if avatar_file is not None:
    b64 = base64.b64encode(avatar_file.read()).decode("utf-8")
    mime = avatar_file.type or "image/png"
    st.session_state.avatar_b64 = f"data:{mime};base64,{b64}"
    st.rerun()

# 头像重置按钮（放在侧边栏或在标题旁小按钮）
if st.session_state.avatar_b64:
    if st.button("↺", key="reset_avatar", help="恢复默认头像"):
        st.session_state.avatar_b64 = None
        st.rerun()

if 'autonomous_enabled' not in st.session_state: st.session_state.autonomous_enabled = False
if 'pending_images' not in st.session_state: st.session_state.pending_images = []
if 'pending_images_preview' not in st.session_state: st.session_state.pending_images_preview = []

@st.fragment
def render_sidebar():
    current_idx = agent.llm_no
    st.caption(f"LLM Core: {current_idx}: {agent.get_llm_name()}", help="点击切换备用链路")
    last_reply_time = st.session_state.get('last_reply_time', 0)
    if last_reply_time > 0:
        st.caption(f"空闲时间：{int(time.time()) - last_reply_time}秒", help="当超过30分钟未收到回复时，系统会自动任务")
    if st.button("切换备用链路"):
        agent.next_llm(); st.rerun(scope="fragment")
    if st.button("强行停止任务"):
        agent.abort(); st.toast("已发送停止信号"); st.rerun()
    if st.button("重新注入工具"):
        agent.llmclient.last_tools = ''
        try:
            hist_path = os.path.join(script_dir, '..', 'assets', 'tool_usable_history.json')
            with open(hist_path, 'r', encoding='utf-8') as f: tool_hist = json.load(f)
            agent.llmclient.backend.history.extend(tool_hist)
            st.toast(f"已重新注入工具，追加了 {len(tool_hist)} 条示范记录")
        except Exception as e: st.toast(f"注入工具示范失败: {e}")
    if st.button("🐱 桌面宠物"):
        kwargs = {'creationflags': 0x08} if sys.platform == 'win32' else {}
        pet_script = os.path.join(script_dir, 'desktop_pet_v2.pyw')
        if not os.path.exists(pet_script): pet_script = os.path.join(script_dir, 'desktop_pet.pyw')
        subprocess.Popen([sys.executable, pet_script], **kwargs)
        def _pet_req(q):
            def _do():
                try: urlopen(f'http://127.0.0.1:41983/?{q}', timeout=2)
                except Exception: pass
            threading.Thread(target=_do, daemon=True).start()
        agent._pet_req = _pet_req
        if not hasattr(agent, '_turn_end_hooks'): agent._turn_end_hooks = {}
        def _pet_hook(ctx):
            parts = [f"Turn {ctx.get('turn','?')}"]
            if ctx.get('summary'): parts.append(ctx['summary'])
            if ctx.get('exit_reason'): parts.append('任务已完成')
            _pet_req(f'msg={quote(chr(10).join(parts))}')
            if ctx.get('exit_reason'): _pet_req('state=idle')
        agent._turn_end_hooks['pet'] = _pet_hook
        st.toast("桌面宠物已启动")
    
    st.divider()
    if st.button("开始空闲自主行动"):
        st.session_state.last_reply_time = int(time.time()) - 1800
        st.toast("已将上次回复时间设为1800秒前"); st.rerun()
    if st.session_state.autonomous_enabled:
        if st.button("⏸️ 禁止自主行动"):
            st.session_state.autonomous_enabled = False
            st.toast("⏸️ 已禁止自主行动"); st.rerun()
        st.caption("🟢 自主行动运行中，会在你离开它30分钟后自动进行")
    else:
        if st.button("▶️ 允许自主行动", type="primary"):
            st.session_state.autonomous_enabled = True
            st.toast("✅ 已允许自主行动"); st.rerun()
        st.caption("🔴 自主行动已停止")
with st.sidebar: render_sidebar()


def fold_turns(text):
    """Return list of segments: [{'type':'text','content':...}, {'type':'fold','title':...,'content':...}]"""
    parts = re.split(r'(\**LLM Running \(Turn \d+\) \.\.\.\*\**)', text)
    if len(parts) < 4: return [{'type': 'text', 'content': text}]
    segments = []
    if parts[0].strip(): segments.append({'type': 'text', 'content': parts[0]})
    turns = []
    for i in range(1, len(parts), 2):
        marker = parts[i]
        content = parts[i+1] if i+1 < len(parts) else ''
        turns.append((marker, content))
    for idx, (marker, content) in enumerate(turns):
        if idx < len(turns) - 1:
            _c = re.sub(r'```.*?```|', '', content, flags=re.DOTALL)
            matches = re.findall(r'<summary>\s*((?:(?!<summary>).)*?)\s*</summary>', _c, re.DOTALL)
            if matches:
                title = matches[0].strip()
                title = title.split('\n')[0]
                if len(title) > 50: title = title[:50] + '...'
            else: title = marker.strip('*')
            segments.append({'type': 'fold', 'title': title, 'content': content})
        else: segments.append({'type': 'text', 'content': marker + content})
    return segments


def render_segments(segments, suffix=''):
    for seg in segments:
        if seg['type'] == 'fold':
            with st.expander(seg['title'], expanded=False): st.markdown(seg['content'])
        else:
            st.markdown(seg['content'] + suffix)


def agent_backend_stream(prompt, images=None):
    display_queue = agent.put_task(prompt, source="user", images=images or [])
    response = ''
    try:
        while True:
            try: item = display_queue.get(timeout=1)
            except queue.Empty:
                yield response
                continue
            if 'next' in item:
                response = item['next']; yield response
            if 'done' in item:
                yield item['done']; break
    finally: agent.abort()


def img_to_base64(img_bytes, mime="image/png"):
    """图片 bytes → data URI"""
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def render_msg_content(msg):
    """渲染消息，支持 markdown + data URI 图片"""
    content = msg.get("content", "")
    images = msg.get("images", [])
    if not images:
        st.markdown(content)
        return
    # 有图片：先展示图片再展示文字
    for img in images:
        if isinstance(img, str) and img.startswith("data:"):
            st.image(img, width=300)
        elif isinstance(img, dict) and "data" in img:
            mime = img.get("mime", "image/png")
            data = img_to_base64(img["data"] if isinstance(img["data"], bytes) else img["data"].encode(), mime)
            st.image(data, width=300)
    if content:
        st.markdown(content)


# ====== 消息历史展示 ======
if "messages" not in st.session_state: st.session_state.messages = []
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        slot = st.empty()
        with slot.container():
            if msg["role"] == "assistant":
                render_segments(fold_turns(msg["content"]))
            else:
                render_msg_content(msg)


# ====== 前端 JS 注入：Ctrl+V / 拖拽 / 右键粘贴图片 ======
from streamlit.components.v1 import html as _embed_html

_js_img_handler = r"""
<script>
(function(){
if(window.__stappImgInit) return;
window.__stappImgInit = true;

// 读取已有图片
function readPendingIds() {
  var el = document.getElementById('stapp-pending-img');
  if(!el) return [];
  try { return JSON.parse(el.value || '[]'); } catch(e) { return []; }
}

// 存储图片base64到隐藏textarea（分块存）
function storeImage(dataUrl) {
  var id = 'img_' + Date.now() + '_' + Math.random().toString(36).slice(2,8);
  var el = document.createElement('textarea');
  el.id = 'stapp-img-' + id;
  el.value = dataUrl;
  el.style.display = 'none';
  document.body.appendChild(el);
  // 更新索引
  var idx = readPendingIds();
  idx.push(id);
  var idxEl = document.getElementById('stapp-pending-img') || (function(){
    var e = document.createElement('input');
    e.id = 'stapp-pending-img';
    e.type = 'hidden';
    document.body.appendChild(e);
    return e;
  })();
  idxEl.value = JSON.stringify(idx);
  idxEl.dispatchEvent(new Event('change', {bubbles:true}));
}

// 处理从剪贴板读取图片
function handlePaste(e) {
  var items = (e.clipboardData || window.clipboardData).items;
  if(!items) return;
  for(var i=0; i<items.length; i++){
    if(items[i].type.indexOf('image') !== -1){
      e.preventDefault();
      e.stopPropagation();
      var blob = items[i].getAsFile();
      var reader = new FileReader();
      reader.onload = function(ev){
        storeImage(ev.target.result);
        // 触发输入框提示
        var ta = document.querySelector('textarea[data-testid="stChatInputTextArea"]');
        if(ta) {
          ta.placeholder = '📷 图片已接收，输入文字或直接发送...';
          ta.focus();
        }
      };
      reader.readAsDataURL(blob);
      break;
    }
  }
}

// 处理拖拽
function handleDrop(e) {
  var files = e.dataTransfer.files;
  if(!files || files.length === 0) return;
  var hasImage = false;
  for(var i=0; i<files.length; i++){
    if(files[i].type.indexOf('image') !== -1){
      hasImage = true;
      var reader = new FileReader();
      reader.onload = function(ev){
        storeImage(ev.target.result);
      };
      reader.readAsDataURL(files[i]);
    }
  }
  if(hasImage){
    e.preventDefault();
    e.stopPropagation();
    var ta = document.querySelector('textarea[data-testid="stChatInputTextArea"]');
    if(ta) ta.placeholder = '📷 图片已接收，输入文字或直接发送...';
  }
}

// 阻止默认拖拽
function preventDefault(e){ e.preventDefault(); e.stopPropagation(); }

// 在chat输入区挂载监听
function attachListeners() {
  var ta = document.querySelector('textarea[data-testid="stChatInputTextArea"]');
  if(!ta) return;
  if(ta.dataset.imgInited) return;
  ta.dataset.imgInited = '1';
  
  document.addEventListener('paste', handlePaste, true);
  ta.addEventListener('paste', handlePaste, true);
  
  document.addEventListener('drop', handleDrop, true);
  ta.addEventListener('drop', handleDrop, true);
  document.addEventListener('dragover', preventDefault, true);
  ta.addEventListener('dragover', preventDefault, true);
  document.addEventListener('dragenter', preventDefault, true);
  ta.addEventListener('dragenter', preventDefault, true);
}

// 轮询等待chat输入框
attachListeners();
var obs = new MutationObserver(function(){
  attachListeners();
});
obs.observe(document.body, {childList:true, subtree:true});

// 额外：右键菜单中也支持"粘贴图片"（系统级截图工具如Snipaste）
console.log('[Cowork] 多模态图片注入已就绪 (Ctrl+V/拖拽/右键粘贴)');
})();
</script>
"""

# 隐藏元素用于JS与Streamlit通信
_embed_html(_js_img_handler, height=0)

# ====== 悬浮的 file_uploader 和手动发送按钮 ======
_upload_col1, _upload_col2, _upload_col3 = st.columns([1, 1, 10])
with _upload_col1:
    uploaded_files = st.file_uploader(
        "📎", type=["png", "jpg", "jpeg", "gif", "webp", "bmp"],
        accept_multiple_files=True, key="img_uploader",
        label_visibility="collapsed",
        help="选择图片（也支持 Ctrl+V / 拖拽）"
    )
with _upload_col2:
    clear_btn = st.button("🗑️", help="清除待发送的图片", key="clear_img_btn")

if clear_btn:
    st.session_state.pending_images = []
    st.session_state.pending_images_preview = []
    st.rerun()

# 处理 file_uploader 图片
if uploaded_files:
    for f in uploaded_files:
        bytes_data = f.getvalue()
        mime = f.type or "image/png"
        b64 = base64.b64encode(bytes_data).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"
        # 去重检查
        if data_uri not in st.session_state.pending_images:
            st.session_state.pending_images.append(data_uri)
            st.session_state.pending_images_preview.append(data_uri)

# ====== 读取 JS 注入的图片（通过隐藏input通信） ======
# Streamlit 不能直接读隐藏 input，用一个折中方案：每轮 rerun 时通过 JS 渲染到可见元素
# 但更稳妥：用 st.markdown 展示已缓存的图片，同时检测是否有新图片
# 注：由于 Streamlit 的架构，JS 注入的图片需要在下一轮 rerun 才能被后端读取
# 我们选择：JS 写入 st.session_state 不现实（跨域），用 file_uploader 互通
# 更优方案：JS 注入图片后触发一次虚拟键盘事件，让 st.chat_input 提交。
# 但 chat_input 只提交文字。所以：在 chat_input 提交时，我们在 session_state 中检查是否有待发送的图片

# 显示待发送图片预览
pending = st.session_state.pending_images
pending_preview = st.session_state.pending_images_preview
if pending_preview:
    st.markdown("**📷 待发送图片:** " + " ".join(
        f"<img src='{url}' style='height:60px;border-radius:6px;margin:2px;vertical-align:middle'>"
        for url in pending_preview[-4:]  # 最多显示4张
    ), unsafe_allow_html=True)
    if len(pending_preview) > 4:
        st.caption(f"...及另 {len(pending_preview)-4} 张")

# ====== 聊天输入与滚动修复 ======
# Scroll-height ghost fix
_js_scroll_fix = ("!function(){var p=window.parent;if(p.__sfx)return;p.__sfx=1;"
    "var d=p.document;setInterval(function(){"
    "var m=d.querySelector('section.main');if(!m)return;"
    "var b=m.querySelector('.block-container');if(!b)return;"
    "if(m.scrollHeight>b.scrollHeight+150){"
    "m.style.overflow='hidden';void m.offsetHeight;m.style.overflow=''}"
    "},3000)}()")
# IME composition fix (macOS only)
_js_ime_fix = ("" if os.name == 'nt' else
    "!function(){if(window.parent.__imeFix)return;window.parent.__imeFix=1;"
    "var d=window.parent.document,c=0;"
    "d.addEventListener('compositionstart',()=>c=1,!0);"
    "d.addEventListener('compositionend',()=>c=0,!0);"
    "function f(){d.querySelectorAll('textarea[data-testid=stChatInputTextArea]')"
    ".forEach(t=>{t.__imeFix||(t.__imeFix=1,t.addEventListener('keydown',e=>{"
    "e.key==='Enter'&&!e.shiftKey&&(e.isComposing||c||e.keyCode===229)&&"
    "(e.stopImmediatePropagation(),e.preventDefault())},!0))})}"
    "f();new MutationObserver(f).observe(d.body,{childList:1,subtree:1})}()")
_embed_html(f'<script>{_js_scroll_fix};{_js_ime_fix}</script>', height=0)


# ====== 主消息处理 ======
if prompt := st.chat_input("any task? 📎 Ctrl+V/拖拽贴图"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cmd = (prompt or "").strip()
    
    # 收集待发送图片
    submit_images = list(st.session_state.pending_images)
    
    def _reset_and_rerun():
        st.session_state.streaming = False
        st.session_state.stopping = False
        st.session_state.display_queue = None
        st.session_state.partial_response = ""
        st.session_state.reply_ts = ""
        st.session_state.current_prompt = ""
        st.session_state.last_reply_time = int(time.time())
        st.rerun()
    
    if cmd == "/new":
        st.session_state.messages = [{"role": "assistant", "content": reset_conversation(agent), "time": ts}]
        st.session_state.pending_images = []
        st.session_state.pending_images_preview = []
        _reset_and_rerun()
    if cmd.startswith("/name"):
        st.session_state.messages = list(st.session_state.messages) + \
            [{"role": "user", "content": cmd, "time": ts},
             {"role": "assistant", "content": handle_frontend_command(agent, cmd), "time": ts}]
        _reset_and_rerun()

    if cmd.startswith("/continue"):
        m = re.match(r'/continue\s+(\d+)\s*$', cmd.strip())
        sessions = list_sessions(exclude_pid=os.getpid()) if m else []
        idx = int(m.group(1)) - 1 if m else -1
        target = sessions[idx][0] if 0 <= idx < len(sessions) else None
        result = handle_frontend_command(agent, cmd)
        history = extract_ui_messages(target) if target and result.startswith('✅') else None
        tail = [{"role": "assistant", "content": result, "time": ts}]
        if history:
            st.session_state.messages = history + tail
        else:
            st.session_state.messages = list(st.session_state.messages) + \
                [{"role": "user", "content": cmd, "time": ts}] + tail
        st.session_state.pending_images = []
        st.session_state.pending_images_preview = []
        _reset_and_rerun()
    
    # 构造用户消息
    user_msg = {"role": "user", "content": prompt, "time": ts}
    if submit_images:
        user_msg["images"] = submit_images
        user_msg["content"] = prompt or "📷 [图片]"
    
    st.session_state.messages.append(user_msg)
    if hasattr(agent, '_pet_req') and not cmd.startswith('/'): agent._pet_req('state=walk')
    
    # 清空待发送图片
    st.session_state.pending_images = []
    st.session_state.pending_images_preview = []
    
    with st.chat_message("user"):
        if submit_images:
            st.markdown("**📷 图片:**")
            cols = st.columns(min(len(submit_images), 4))
            for i, img_url in enumerate(submit_images):
                with cols[i % 4]:
                    st.image(img_url, width=200)
        if prompt:
            st.markdown(prompt)
    
    with st.chat_message("assistant"):
        frozen = 0; live = st.empty(); response = ''
        CURSOR = ' ▌'
        for response in agent_backend_stream(prompt, images=submit_images):
            segs = fold_turns(response)
            n_done = max(0, len(segs) - 1)
            while frozen < n_done:
                with live.container(): render_segments([segs[frozen]])
                live = st.empty(); frozen += 1
            with live.container(): render_segments([segs[-1]], suffix=CURSOR)
        segs = fold_turns(response)
        for i in range(frozen, len(segs)):
            with live.container(): render_segments([segs[i]])
            if i < len(segs) - 1: live = st.empty()
    st.session_state.messages.append({"role": "assistant", "content": response})
    st.session_state.last_reply_time = int(time.time())

if st.session_state.autonomous_enabled:
    st.markdown(f"""<div id="last-reply-time" style="display:none">{st.session_state.get('last_reply_time', int(time.time()))}</div>""", unsafe_allow_html=True)