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
import chatapp_common
from continue_cmd import handle_frontend_command, reset_conversation, list_sessions, extract_ui_messages

st.set_page_config(page_title="Cowork", layout="wide")

@st.cache_resource
def init():
    agent = GeneraticAgent()
    if agent.llmclient is None:
        st.error("⚠️ 未配置任何可用的 LLM 接口，请设置 mykey.py。")
        st.stop()
    else: threading.Thread(target=agent.run, daemon=True).start()
    return agent

agent = init()

if 'autonomous_enabled' not in st.session_state: st.session_state.autonomous_enabled = False
if 'pending_images' not in st.session_state: st.session_state.pending_images = []
if 'pending_images_preview' not in st.session_state: st.session_state.pending_images_preview = []
if 'messages' not in st.session_state: st.session_state.messages = []
if 'current_session_id' not in st.session_state: st.session_state.current_session_id = None

# ====== Sidebar ======
def refresh_session_list():
    sessions = []
    log_dir = os.path.join(script_dir, '..', 'temp', 'model_responses')
    if os.path.isdir(log_dir):
        files = sorted([f for f in os.listdir(log_dir) if f.endswith('.md')],
                       key=lambda f: os.path.getmtime(os.path.join(log_dir, f)), reverse=True)
        for f in files[:30]:
            path = os.path.join(log_dir, f)
            mtime = os.path.getmtime(path)
            try:
                with open(path, 'r', encoding='utf-8') as fh: first_line = fh.readline().strip()
            except: first_line = f
            sessions.append({'id': f, 'path': path, 'time': mtime, 'preview': first_line[:80]})
    return sessions

with st.sidebar:
    st.markdown("### 🤖 工作台")
    current_idx = agent.llm_no
    st.caption(f"LLM: {current_idx}: {agent.get_llm_name()}")
    last_reply_time = st.session_state.get('last_reply_time', 0)
    if last_reply_time > 0:
        st.caption(f"⏱ {int(time.time()) - last_reply_time}秒")
    if st.button("🔄 切换备用链路"):
        agent.next_llm(); st.rerun()
    if st.button("⏹ 强行停止"):
        agent.abort(); st.toast("已发送停止信号"); st.rerun()
    if st.button("🔧 重新注入工具"):
        agent.llmclient.last_tools = ''
        try:
            hist_path = os.path.join(script_dir, '..', 'assets', 'tool_usable_history.json')
            with open(hist_path, 'r', encoding='utf-8') as f: tool_hist = json.load(f)
            agent.llmclient.backend.history.extend(tool_hist)
            st.toast(f"已重新注入工具，追加了 {len(tool_hist)} 条示范记录")
        except Exception as e: st.toast(f"注入失败: {e}")
    st.divider()
    if st.button("开始空闲自主行动"):
        st.session_state.last_reply_time = int(time.time()) - 1800
        st.toast("已将上次回复时间设为1800秒前"); st.rerun()
    if st.session_state.autonomous_enabled:
        if st.button("⏸️ 禁止自主行动"):
            st.session_state.autonomous_enabled = False
            st.toast("⏸️ 已禁止"); st.rerun()
        st.caption("🟢 30分钟后自动工作")
    else:
        if st.button("▶️ 允许自主行动", type="primary"):
            st.session_state.autonomous_enabled = True
            st.toast("✅ 已允许"); st.rerun()
        st.caption("🔴 自主已停")
    st.divider()
    st.markdown("**📋 会话记录**")
    col1, col2 = st.columns([3,1])
    with col1:
        if st.button("🔄 刷新", use_container_width=True): st.rerun()
    with col2:
        if st.button("➕ 新", use_container_width=True):
            st.session_state.messages = [{"role": "assistant", "content": reset_conversation(agent), "time": time.strftime("%Y-%m-%d %H:%M:%S")}]
            st.session_state.last_reply_time = int(time.time())
            st.rerun()
    sessions = refresh_session_list()
    for s in sessions:
        t_str = time.strftime("%m-%d %H:%M", time.localtime(s['time']))
        label = f"{t_str} {s['preview']}"
        if st.button(label, key=f"session_{s['id']}", use_container_width=True):
            history = extract_ui_messages(s['path'])
            if history:
                st.session_state.messages = history
            else:
                try:
                    with open(s['path'], 'r', encoding='utf-8') as f: content = f.read()
                    st.session_state.messages = [{"role": "assistant", "content": content[:2000]}]
                except: st.toast("无法读取会话")
            st.session_state.current_session_id = s['id']
            st.rerun()

# ====== Message fold rendering ======
def fold_turns(text):
    parts = re.split(r'(\*\*LLM Running \(Turn \d+\) \.\.\.\*\*)', text)
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
            _c = re.sub(r'```.*?```|<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
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

# ====== Render messages ======
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        slot = st.empty()
        with slot.container():
            images = msg.get("images", [])
            if images:
                for img in images:
                    if isinstance(img, str) and img.startswith("data:"):
                        st.image(img, width=300)
                    elif isinstance(img, dict) and "data" in img:
                        st.image(img["data"], width=300)
            if msg["role"] == "assistant":
                render_segments(fold_turns(msg["content"]))
            else:
                st.markdown(msg["content"])

# ====== Paste/Drag/Upload ======
from streamlit.components.v1 import html as st_html

_paste_js = """
<script>
(function(){
  if(window.__pasteInited) return;
  window.__pasteInited = true;
  document.addEventListener('paste', function(e){
    var items = e.clipboardData && e.clipboardData.items;
    if(!items) return;
    for(var i=0; i<items.length; i++){
      if(items[i].type.indexOf('image') === 0){
        e.preventDefault();
        var blob = items[i].getAsFile();
        if(!blob) continue;
        var reader = new FileReader();
        reader.onload = function(ev){
          var input = document.getElementById('paste-image-data');
          if(!input){
            input = document.createElement('input');
            input.type = 'hidden';
            input.id = 'paste-image-data';
            document.body.appendChild(input);
          }
          input.value = ev.target.result;
          input.dispatchEvent(new Event('input', {bubbles: true}));
        };
        reader.readAsDataURL(blob);
        break;
      }
    }
  });
  document.addEventListener('dragover', function(e){e.preventDefault()}, false);
  document.addEventListener('drop', function(e){
    e.preventDefault();
    var files = e.dataTransfer.files;
    for(var i=0; i<files.length; i++){
      if(files[i].type.indexOf('image') === 0){
        var reader = new FileReader();
        reader.onload = function(ev){
          var input = document.getElementById('paste-image-data');
          if(!input){
            input = document.createElement('input');
            input.type = 'hidden';
            input.id = 'paste-image-data';
            document.body.appendChild(input);
          }
          input.value = ev.target.result;
          input.dispatchEvent(new Event('input', {bubbles: true}));
        };
        reader.readAsDataURL(files[i]);
      }
    }
  }, false);
})();
</script>
"""
st_html(_paste_js, height=0)

paste_data = st.text_input("", key="paste_image_input", label_visibility="collapsed", placeholder="")

uploaded_files = st.file_uploader(
    "上传图片", type=["png","jpg","jpeg","gif","webp","bmp"],
    accept_multiple_files=True, key="image_uploader", label_visibility="collapsed"
)

pending_images = list(st.session_state.get('pending_images', []))
pending_preview = list(st.session_state.get('pending_images_preview', []))

if paste_data and paste_data.startswith("data:image"):
    pending_images.append(paste_data)
    pending_preview.append(f'<img src="{paste_data}" style="height:60px;border-radius:6px;margin:2px;vertical-align:middle">')
    st.session_state.paste_image_input = ""
    st.session_state.pending_images = pending_images
    st.session_state.pending_images_preview = pending_preview
    st.rerun()

if uploaded_files:
    for f in uploaded_files:
        b64 = base64.b64encode(f.read()).decode("utf-8")
        mime = f.type or "image/png"
        data_url = f"data:{mime};base64,{b64}"
        pending_images.append(data_url)
        pending_preview.append(f'<img src="{data_url}" style="height:60px;border-radius:6px;margin:2px;vertical-align:middle">')
    st.session_state.pending_images = pending_images
    st.session_state.pending_images_preview = pending_preview
    st.rerun()

if pending_preview:
    st.markdown(
        "📎 待发送：" + "".join(pending_preview[-4:]) +
        (f" ...及另{len(pending_preview)-4}张" if len(pending_preview) > 4 else ""),
        unsafe_allow_html=True
    )

# ====== IME + Scroll fix ======
_js_scroll_fix = ("!function(){var p=window.parent;if(p.__sfx)return;p.__sfx=1;"
    "var d=p.document;setInterval(function(){"
    "var m=d.querySelector('section.main');if(!m)return;"
    "var b=m.querySelector('.block-container');if(!b)return;"
    "if(m.scrollHeight>b.scrollHeight+150){"
    "m.style.overflow='hidden';void m.offsetHeight;m.style.overflow=''}"
    "},3000)}()")
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
st_html(f'<script>{_js_scroll_fix};{_js_ime_fix}</script>', height=0)

# ====== Chat input ======
if prompt := st.chat_input("输入任务 (可粘贴/拖拽图片, 或点击📎上传)"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cmd = (prompt or "").strip()
    images_to_send = list(pending_images)

    def _reset_and_rerun():
        st.session_state.last_reply_time = int(time.time())
        st.session_state.pending_images = []
        st.session_state.pending_images_preview = []
        st.rerun()

    if cmd == "/new":
        st.session_state.messages = [{"role": "assistant", "content": reset_conversation(agent), "time": ts}]
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
        _reset_and_rerun()

    msg_entry = {"role": "user", "content": prompt, "time": ts}
    if images_to_send:
        msg_entry["images"] = images_to_send
    st.session_state.messages.append(msg_entry)
    st.session_state.pending_images = []
    st.session_state.pending_images_preview = []

    with st.chat_message("user"):
        if images_to_send:
            for img_data in images_to_send:
                st.image(img_data, width=300)
        st.markdown(prompt)

    with st.chat_message("assistant"):
        frozen = 0; live = st.empty(); response = ''
        CURSOR = ' ▌'
        for response in agent_backend_stream(prompt, images=images_to_send if images_to_send else None):
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
    st.session_state.messages.append({"role": "assistant", "content": response, "time": ts})
    st.session_state.last_reply_time = int(time.time())

if st.session_state.autonomous_enabled:
    st.markdown(f'<div id="last-reply-time" style="display:none">{st.session_state.get("last_reply_time", int(time.time()))}</div>', unsafe_allow_html=True)
