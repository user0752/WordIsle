/**
 * assistant.js —— 词小屿（全局智能助手）前端逻辑
 * =================================================
 * 职责：悬浮挂件（拖动/位置记忆/状态反馈）+ 对话面板（SSE 流式 + rAF 打字机平滑渲染、
 * 快捷问题/场景感知、操作确认卡片、会话持久化恢复、清空会话、停止生成、
 * 智能滚动跟随、面板拖拽调宽高（记忆尺寸）/最大化、回答复制）。
 *
 * 使用方式（在 index.html 的 setup 中）：
 *   import { createAssistant } from '/static/js/assistant.js'
 *   const assistant = createAssistant({ api, apiStream, getPage, goPage })
 *   return { ..., assistant }
 */

const { reactive, nextTick } = window.Vue

const STORE_POS = 'wordisle.assistant.pos.v1'
const STORE_OPEN = 'wordisle.assistant.open.v1'
const STORE_UNREAD = 'wordisle.assistant.unread.v1'
const STORE_SIZE = 'wordisle.assistant.size.v1'
const STORE_MAX = 'wordisle.assistant.max.v1'
const STORE_SOUND = 'wordisle.assistant.sound.v1'

// 面板尺寸边界（px）
const PANEL_MIN_W = 320, PANEL_MAX_W = 860
const PANEL_MIN_H = 380

// 场景感知快捷问题：按页面动态推荐（F-206）
const QUICK_QUESTIONS = {
  home: ['怎么快速上手？', '有哪些功能？'],
  words: ['怎么批量导入单词？', '今天该复习哪些词？'],
  import: ['怎么从文章提取单词？', '批量导入会重复吗？'],
  single: ['单点深耕是什么？怎么用？', '记忆卡片能配音吗？'],
  scenes: ['场景聚汇是什么？', '怎么给单词检测场景？'],
  compile: ['批量编译能编几个词？', '生成的故事去哪看？'],
  video: ['视频编译大概要多久？', '视频在哪看？'],
  polysemy: ['熟词僻意是什么？', '怎么批量检测多义词？'],
  morphemes: ['构词拆解是什么？', '怎么构建词根树？'],
  review: ['今天该复习哪些词？', '怎么标记治愈上岸？'],
  healed: ['治愈图鉴是什么？', '怎么撤回治愈？'],
  history: ['历史记录能搜什么？', '怎么收藏结果？'],
  usage: ['怎么看我的用量？', '游客有什么限制？'],
  dashboard: ['反馈看板是什么？'],
  settings: ['怎么切换模型？', '发音音色在哪设置？'],
}

function _safeStore(key, def) {
  try { return JSON.parse(localStorage.getItem(key) || 'null') ?? def } catch (_) { return def }
}
function _saveStore(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)) } catch (_) {}
}

let _seq = 0
function _uid() { return `m${++_seq}_${Date.now()}` }

// 后端 created_at 形如 "YYYY-MM-DD HH:MM:SS"（本地时间）→ 时间戳；非法时回退当前
function _parseTs(s) {
  const t = s ? new Date(String(s).replace(' ', 'T')).getTime() : NaN
  return Number.isFinite(t) ? t : Date.now()
}
function _dayKey(ts) {
  const d = new Date(ts)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}
function _dayLabel(ts) {
  const d = new Date(ts), now = new Date()
  const midnight = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((midnight(now) - midnight(d)) / 86400000)
  if (diff === 0) return '今天'
  if (diff === 1) return '昨天'
  const y = d.getFullYear() !== now.getFullYear() ? d.getFullYear() + '年' : ''
  return `${y}${d.getMonth() + 1}月${d.getDate()}日`
}

// ---------- Markdown 轻量渲染（先转义再替换，杜绝 XSS） ----------
function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

export function md2html(text) {
  const esc = _escapeHtml(text)
  // 代码块
  let html = esc.replace(/```([\s\S]*?)```/g, (_, code) =>
    `<pre class="cxy-code"><code>${code}</code></pre>`)
  // 行内代码
  html = html.replace(/`([^`\n]+)`/g, '<code class="cxy-inline-code">$1</code>')
  // **加粗**
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  // 列表（无序 "- " / 有序 "1. "）：把连续列表行聚合成一个 <ul>/<ol>
  html = html.replace(/(?:^|\n)[ \t]*([-*] |\d+[.、)][\t ]+)[^\n]+(?:\n[ \t]*(?:[-*] |\d+[.、)][\t ]+)[^\n]+)*/g, (block) => {
    const lines = block.split('\n').map(l => l.trim()).filter(Boolean)
    const ordered = lines.length > 0 && /^\d+[.、)]/.test(lines[0])
    const items = lines.map(l => `<li>${l.replace(/^(?:[-*] |\d+[.、)][\t ]+)/, '')}</li>`).join('')
    const tag = ordered ? 'ol' : 'ul'
    return `<${tag} class="cxy-list">${items}</${tag}>`
  })
  // 换行：连续两个以上 → 段落间距；单个 → <br>
  html = html.replace(/\n{2,}/g, '<br class="cxy-p">').replace(/\n/g, '<br>')
  return html
}

export function createAssistant({ api, apiStream, getPage, goPage }) {
  const pos = _safeStore(STORE_POS, { right: 24, bottom: 24 })

  const state = reactive({
    // 挂件
    open: _safeStore(STORE_OPEN, false),
    pos,
    dragging: false,
    unread: _safeStore(STORE_UNREAD, 0),
    unreadSnippet: localStorage.getItem('wordisle.assistant.snippet.v1') || '',
    configured: true,       // LLM 是否已配置（/api/assistant/status）
    // 面板
    busy: false,
    input: '',
    messages: [],
    quick: [],
    canStop: false,
    // 会话
    loadingHistory: false,
    // 面板尺寸/最大化（尺寸持久化，最大化记忆）
    size: _safeStore(STORE_SIZE, null),
    maximized: _safeStore(STORE_MAX, false),
    atBottom: true,         // 消息流是否贴底（贴底才自动跟随滚动）
    // 本轮新增：建议追问 / 评分 / 朗读 / 语音输入 / 打字音效
    suggestions: [],        // 最后一条助手回答的建议追问（done 事件携带）
    soundOn: _safeStore(STORE_SOUND, false),
    recording: false,       // 语音输入进行中
    speakingMsg: null,      // 正在朗读的消息（用于高亮/停止）
    canSpeak: 'speechSynthesis' in window,
    canListen: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
    // 内部
    controller: null,
    recognizer: null,       // SpeechRecognition 实例
    streamMsg: null,        // 当前流式渲染中的助手消息
  })

  // ---------- 生命周期 ----------
  async function init() {
    updateQuick()
    window.addEventListener('resize', _clampPanel)
    try {
      const st = await api('/api/assistant/status', {}, { withLoading: false })
      state.configured = !!(st && st.configured)
    } catch (_) { state.configured = false }
  }

  // ---------- 快捷问题（场景感知） ----------
  function updateQuick() {
    state.quick = QUICK_QUESTIONS[getPage()] || QUICK_QUESTIONS.home || []
  }
  function askQuick(q) {
    state.input = q
    send()
  }

  // ---------- 挂件拖拽（位移 < 5px 视为点击展开） ----------
  function onPointerDown(e) {
    if (state.dragging || e.button !== 0) return
    const startX = e.clientX, startY = e.clientY
    let moved = false
    const el = e.currentTarget
    el.setPointerCapture && el.setPointerCapture(e.pointerId)

    const move = (ev) => {
      const dx = ev.clientX - startX, dy = ev.clientY - startY
      if (Math.abs(dx) + Math.abs(dy) >= 5) moved = true
      if (!moved) return
      state.dragging = true
      const vw = window.innerWidth, vh = window.innerHeight
      const w = el.offsetWidth || 52, h = el.offsetHeight || 52
      let right = Math.min(Math.max(vw - ev.clientX - w / 2, 6), vw - w - 6)
      let bottom = Math.min(Math.max(vh - ev.clientY - h / 2, 6), vh - h - 6)
      state.pos.right = Math.round(right)
      state.pos.bottom = Math.round(bottom)
      _saveStore(STORE_POS, state.pos)
    }
    const up = (ev) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      el.releasePointerCapture && el.releasePointerCapture(ev.pointerId)
      state.dragging = false
      if (!moved) togglePanel()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // ---------- 面板开合 ----------
  function togglePanel() {
    state.open = !state.open
    _saveStore(STORE_OPEN, state.open)
    if (state.open) {
      clearUnread()
      if (state.messages.length === 0) loadHistory()
      scrollBottom(true)
    }
  }
  function closePanel() {
    state.open = false
    _saveStore(STORE_OPEN, state.open)
  }
  function clearUnread() {
    state.unread = 0
    state.unreadSnippet = ''
    try { localStorage.removeItem('wordisle.assistant.unread.v1'); localStorage.removeItem('wordisle.assistant.snippet.v1') } catch (_) {}
  }

  // ---------- 面板尺寸：拖拽左下角调宽高 + 最大化（记忆尺寸） ----------
  function panelStyle() {
    const s = _ensureSize()
    return { width: s.w + 'px', height: s.h + 'px' }
  }
  function _ensureSize() {
    if (!state.size) {
      state.size = {
        w: Math.min(400, Math.max(PANEL_MIN_W, window.innerWidth - 48)),
        h: Math.max(PANEL_MIN_H, Math.round(window.innerHeight * 0.62)),
      }
    }
    return state.size
  }
  function _clampPanel() {
    if (!state.size) return
    const vw = window.innerWidth, vh = window.innerHeight
    state.size.w = Math.round(Math.min(Math.max(state.size.w, PANEL_MIN_W), Math.min(PANEL_MAX_W, vw - 24)))
    state.size.h = Math.round(Math.min(Math.max(state.size.h, PANEL_MIN_H), vh - 24))
  }
  function onResizeDown(e) {
    if (state.maximized || e.button !== 0) return
    e.preventDefault()
    const s = _ensureSize()
    const startX = e.clientX, startY = e.clientY
    const startW = s.w, startH = s.h
    const handle = e.currentTarget
    handle.setPointerCapture && handle.setPointerCapture(e.pointerId)
    document.body && (document.body.style.userSelect = 'none')

    const move = (ev) => {
      const vw = window.innerWidth, vh = window.innerHeight
      s.w = Math.round(Math.min(Math.max(startW + (startX - ev.clientX), PANEL_MIN_W), Math.min(PANEL_MAX_W, vw - 24)))
      s.h = Math.round(Math.min(Math.max(startH + (startY - ev.clientY), PANEL_MIN_H), vh - 24))
      _saveStore(STORE_SIZE, s)
    }
    const up = (ev) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      handle.releasePointerCapture && handle.releasePointerCapture(ev.pointerId)
      document.body && (document.body.style.userSelect = '')
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  function toggleMax() {
    state.maximized = !state.maximized
    _saveStore(STORE_MAX, state.maximized)
    scrollBottom(true)
  }

  // ---------- 智能滚动：贴底自动跟随；上翻时不打扰，提供「回到底部」 ----------
  function onMessagesScroll(e) {
    const el = e.target
    state.atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }
  async function scrollBottom(force = false) {
    if (!force && !state.atBottom) return
    await nextTick()
    const el = document.querySelector('.cxy-messages')
    if (el) {
      el.scrollTop = el.scrollHeight
      state.atBottom = true
    }
  }
  function jumpToBottom() { scrollBottom(true) }

  // ---------- 会话：历史恢复 / 清空 ----------
  async function loadHistory() {
    state.loadingHistory = true
    try {
      const res = await api('/api/assistant/history', {}, { withLoading: false })
      state.messages = (res.items || []).filter(m => m.content && m.content.trim()).map(m => ({
        id: _uid(),
        role: m.role === 'user' ? 'user' : 'assistant',
        kind: 'chat',
        text: m.content,
        ts: _parseTs(m.created_at),
        streaming: false,
        navigate: null, confirm: null, queryData: null, error: null, executed: null,
      }))
      if (state.messages.length === 0) {
        pushMsg({ role: 'assistant', kind: 'chat', text: '你好呀，我是词小屿 👋 词屿的贴身向导。想了解某个功能怎么用，或想让我帮你查词，直接说就行～',
          streaming: false })
      }
      scrollBottom(true)
    } catch (e) {
      pushMsg({ role: 'assistant', kind: 'chat', text: '会话记录读取失败：' + (e.message || '网络错误'), streaming: false, error: true })
    } finally {
      state.loadingHistory = false
    }
  }

  async function clearConversation() {
    try {
      await api('/api/assistant/conversation', { method: 'DELETE' }, { withLoading: false })
      state.messages = []
      pushWelcome()
    } catch (e) {
      pushMsg({ role: 'assistant', kind: 'chat', text: '清空失败：' + (e.message || '网络错误'), streaming: false, error: true })
    }
  }
  function pushWelcome() {
    if (state.messages.length === 0) {
      pushMsg({ role: 'assistant', kind: 'chat', text: '你好呀，我是词小屿 👋 词屿的贴身向导。想了解某个功能怎么用，或想让我帮你查词，直接说就行～', streaming: false })
    }
  }

  function pushMsg(partial) {
    const m = Object.assign({
      id: _uid(), role: 'assistant', kind: 'chat', text: '',
      ts: Date.now(), rating: null,
      streaming: false, navigate: null, confirm: null, queryData: null,
      errorChunk: null, executed: null, executing: false, stepLabel: '', copied: false,
    }, partial)
    state.messages.push(m)
    scrollBottom()
    return m
  }

  // ---------- 发送 / 流式接收（rAF 打字机平滑） ----------
  // SSE 增量（可能突发大块）先入缓冲，rAF 每帧按积压量自适应取字渲染：
  // 积压越多打得越快，既平滑又不会在长回答时拖尾；结束后排空再收尾。
  function _createTyper(m, onDrained) {
    let pending = ''
    let raf = 0
    let closed = false
    let cancelled = false
    function frame() {
      raf = 0
      if (cancelled) return
      if (pending) {
        const n = Math.max(2, Math.ceil(pending.length / 6))
        m.text += pending.slice(0, n)
        pending = pending.slice(n)
        if (state.soundOn) _tick()
        scrollBottom()
        raf = requestAnimationFrame(frame)
      } else if (closed && onDrained) {
        onDrained()
      }
    }
    function schedule() { if (!raf && !cancelled) raf = requestAnimationFrame(frame) }
    return {
      push(t) { if (t && !cancelled) { pending += t; schedule() } },
      close() { closed = true; if (!pending && !raf && !cancelled) frame() },
      flush() {
        if (pending && !cancelled) { m.text += pending; pending = '' }
        if (raf) { cancelAnimationFrame(raf); raf = 0 }
        if (closed && onDrained) onDrained()
      },
      cancel() {
        cancelled = true
        if (raf) { cancelAnimationFrame(raf); raf = 0 }
        pending = ''
      },
    }
  }

  async function send() {
    const text = state.input.trim()
    if (!text || state.busy) return
    state.input = ''
    _resetInputHeight()
    pushMsg({ role: 'user', kind: 'chat', text })
    state.atBottom = true
    _dispatch(text)
  }

  // 建议 chip 点击：直接作为新一轮提问发出
  function askSuggest(q) {
    if (state.busy || !q) return
    state.input = ''
    pushMsg({ role: 'user', kind: 'chat', text: q })
    state.atBottom = true
    _dispatch(q)
  }

  // 重新生成：找最后一条用户提问，删掉其后的助手消息后原样重发
  function regenerate() {
    if (state.busy) return
    let idx = -1
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'user') { idx = i; break }
    }
    if (idx < 0) return
    const text = state.messages[idx].text
    state.messages.splice(idx + 1)
    state.suggestions = []
    state.atBottom = true
    _dispatch(text)
  }

  // 实际发起流式请求（send / askSuggest / regenerate 共用）
  async function _dispatch(text) {
    state.suggestions = []
    const m = pushMsg({ role: 'assistant', kind: 'chat', text: '', streaming: true })
    state.streamMsg = m
    state.busy = true
    state.canStop = true

    const controller = new AbortController()
    state.controller = controller
    let gotContent = false
    let finalized = false
    let pendingSuggests = null
    const finish = () => {
      if (finalized) return
      finalized = true
      // 建议追问等打字动画收尾后再亮出，避免打断打字观感
      if (pendingSuggests && pendingSuggests.length) state.suggestions = pendingSuggests
      finalize(m, controller, gotContent)
    }
    const typer = _createTyper(m, finish)

    try {
      await apiStream('/api/assistant/chat',
        { method: 'POST', body: JSON.stringify({ message: text, page: getPage() }) },
        {
          onStep: (p) => { m.stepLabel = (p && p.label) || '' },  // 意图识别等分步反馈
          onTool: (p) => handleTool(p, m),
          onResult: (p) => {
            gotContent = true
            typer.push((p && p.text) || '')
          },
          onDone: (p) => { pendingSuggests = (p && p.suggests) || null; typer.close() },
          onError: (p) => {
            m.errorChunk = (p && p.msg) || '出错了，请稍后再试'
            typer.close()
          },
        },
        { signal: controller.signal, withLoading: false })
      typer.close()
    } catch (e) {
      if (e && e.name === 'AbortError') {
        typer.cancel()
        m.text = gotContent ? m.text + '（已停止生成）' : '已停止生成'
      } else {
        m.errorChunk = (e && e.message) || '网络异常，请稍后再试'
        typer.close()
      }
      finish()
    }
    if (!finalized) typer.close()
  }

  function handleTool(p, m) {
    if (!p) return
    if (p.tool === 'navigate') {
      m.navigate = { page: (p.args || {}).page, label: p.human_readable || '' }
      scrollBottom()
      return
    }
    if (p.confirm_required) {
      m.confirm = { tool: p.tool, args: p.args || {}, human: p.human_readable || {}, executing: false }
      scrollBottom()
      return
    }
    // 查询工具 → 结构化结果卡片（数据在后端已执行）
    m.queryData = { tool: p.tool, args: p.args || {}, human: p.human_readable || '', data: p.data || {} }
    scrollBottom()
  }

  function finalize(m, controller, gotContent) {
    m.streaming = false
    state.streamMsg = null
    state.busy = false
    state.canStop = false
    state.controller = null
    if (controller) controller = null
    if (!gotContent && !m.errorChunk && !m.confirm && !m.queryData && m.text.trim() === '') {
      m.errorChunk = '（没有收到回复）'
    }
    // 面板关闭时：红点 + 气泡摘要
    if (!state.open) {
      const snippet = (m.text || m.errorChunk || '').trim()
      if (snippet && snippet !== '…') {
        state.unread += 1
        state.unreadSnippet = snippet.slice(0, 40)
        _saveStore(STORE_UNREAD, state.unread)
        try { localStorage.setItem('wordisle.assistant.snippet.v1', state.unreadSnippet) } catch (_) {}
      }
    }
    scrollBottom()
  }

  function stop() {
    if (state.controller) state.controller.abort()
  }

  // ---------- 输入：Enter 发送 / Shift+Enter 换行（含中文输入法组合保护）/ 自动伸缩 ----------
  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault()
      send()
    }
  }
  function autoGrow(e) {
    const el = e.target
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 120) + 'px'
  }
  function _resetInputHeight() {
    const el = document.querySelector('.cxy-input')
    if (el) el.style.height = ''
  }

  // ---------- 回答复制 ----------
  async function copyMsg(m) {
    const txt = m.text || ''
    if (!txt) return
    let ok = false
    try {
      await navigator.clipboard.writeText(txt)
      ok = true
    } catch (_) {
      try {
        const ta = document.createElement('textarea')
        ta.value = txt
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        ok = document.execCommand('copy')
        ta.remove()
      } catch (_) { ok = false }
    }
    if (ok) {
      m.copied = true
      setTimeout(() => { m.copied = false }, 1600)
    }
  }

  // ---------- 会话日期分段（今天 / 昨天 / M月D日，跨年带年份） ----------
  function segments() {
    const out = []
    let cur = null, curKey = null
    for (const m of state.messages) {
      const ts = m.ts || Date.now()
      const k = _dayKey(ts)
      if (k !== curKey) {
        cur = { key: k, label: _dayLabel(ts), items: [] }
        out.push(cur)
        curKey = k
      }
      cur.items.push(m)
    }
    return out
  }

  // 是否为最后一条助手消息（重新生成按钮只在该条上出现）
  function isLastAi(m) {
    const msgs = state.messages
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'ai') return msgs[i] === m
    }
    return false
  }

  // ---------- 点赞点踩（同向再点 = 取消，后端 toggle） ----------
  async function rate(m, rating) {
    if (!m || !m.text || state.busy) return
    const i = state.messages.indexOf(m)
    let question = ''
    for (let j = i - 1; j >= 0; j--) {
      if (state.messages[j].role === 'user') { question = state.messages[j].text; break }
    }
    try {
      const res = await api('/api/assistant/feedback', {
        method: 'POST',
        body: JSON.stringify({ question, answer: m.text, rating }),
      }, { withLoading: false })
      if (res && res.ok) m.rating = (m.rating === rating) ? null : rating
    } catch (_) { /* 静默失败：不打断对话 */ }
  }

  // ---------- 朗读（Web Speech API，再点停止） ----------
  function _plainText(md) {
    return String(md || '')
      .replace(/```[\s\S]*?```/g, '，代码块，')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/^[\s]*[-*] /gm, '')
      .replace(/^[\s]*\d+[.、)] /gm, '')
      .replace(/[#>*]/g, '')
      .trim()
  }
  function speak(m) {
    if (!state.canSpeak || !m.text) return
    if (state.speakingMsg === m) { stopSpeak(); return }
    stopSpeak()
    const u = new SpeechSynthesisUtterance(_plainText(m.text))
    u.lang = 'zh-CN'
    u.onend = u.onerror = () => { state.speakingMsg = null }
    state.speakingMsg = m
    speechSynthesis.speak(u)
  }
  function stopSpeak() {
    if (window.speechSynthesis) speechSynthesis.cancel()
    state.speakingMsg = null
  }

  // ---------- 语音输入（SpeechRecognition，不支持时入口隐藏） ----------
  function toggleListen() {
    if (state.recording) { stopListen(); return }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) return
    const rec = new SR()
    rec.lang = 'zh-CN'
    rec.interimResults = true
    rec.continuous = false
    const base = state.input
    rec.onresult = (e) => {
      let t = ''
      for (const r of e.results) t += r[0].transcript
      state.input = (base ? base + ' ' : '') + t
    }
    rec.onend = () => { state.recording = false; state.recognizer = null }
    rec.onerror = () => { state.recording = false; state.recognizer = null }
    state.recognizer = rec
    state.recording = true
    try { rec.start() } catch (_) { state.recording = false }
  }
  function stopListen() {
    if (state.recognizer) { try { state.recognizer.stop() } catch (_) {} }
    state.recording = false
  }

  // ---------- 打字机音效（WebAudio 短 tick，默认关闭，header 开关记忆） ----------
  let _audioCtx = null
  let _lastTick = 0
  function _tick() {
    const now = performance.now()
    if (now - _lastTick < 45) return   // 节流：最快约 22 次/秒
    _lastTick = now
    try {
      _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)()
      if (_audioCtx.state === 'suspended') _audioCtx.resume()
      const t = _audioCtx.currentTime
      const osc = _audioCtx.createOscillator()
      const gain = _audioCtx.createGain()
      osc.type = 'triangle'
      osc.frequency.value = 1900
      gain.gain.setValueAtTime(0.035, t)
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.04)
      osc.connect(gain).connect(_audioCtx.destination)
      osc.start(t)
      osc.stop(t + 0.045)
    } catch (_) {}
  }
  function toggleSound() {
    state.soundOn = !state.soundOn
    _saveStore(STORE_SOUND, state.soundOn)
    if (state.soundOn) _tick()
  }

  // ---------- 会话导出（Markdown 下载，按日期分段组织） ----------
  function exportChat() {
    if (state.messages.length === 0) return
    const p2 = (x) => String(x).padStart(2, '0')
    const now = new Date()
    const lines = ['# 词小屿会话导出', '', `- 导出时间：${now.toLocaleString('zh-CN')}`, '']
    for (const seg of segments()) {
      lines.push(`## ${seg.label}`, '')
      for (const m of seg.items) {
        const who = m.role === 'user' ? '🧑 我' : '🏝️ 词小屿'
        lines.push(`**${who}**：`, '', (m.text || '（无文本内容）'), '')
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `词小屿会话-${now.getFullYear()}${p2(now.getMonth() + 1)}${p2(now.getDate())}-${p2(now.getHours())}${p2(now.getMinutes())}.md`
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 3000)
  }

  // ---------- 操作确认卡片：执行由前端调真实 API（不经过 LLM） ----------
  async function confirmAction(m) {
    if (!m.confirm || m.confirm.executing) return
    const card = m.confirm
    card.executing = true
    try {
      if (card.tool === 'add_words') {
        const willAdd = (card.human && card.human.will_add) || []
        const res = await api('/api/words/import', {
          method: 'POST', body: JSON.stringify({ words: willAdd }),
        })
        const imported = res.imported || 0, duplicated = res.duplicated || 0
        m.executed = `已添加 ${imported} 个单词` + (duplicated ? `，跳过已有 ${duplicated} 个` : '')
        m.confirm = null
      } else if (card.tool === 'delete_word') {
        const word = (card.human && card.human.word) || ''
        const found = await api(`/api/words?search=${encodeURIComponent(word)}&page_size=20`)
        const row = (found.items || []).find(r => r.word === word)
        if (!row) {
          m.executed = `词库中找不到「${word}」，无需删除`
        } else {
          await api(`/api/words/${row.id}`, { method: 'DELETE' })
          m.executed = `已删除「${word}」（在单词库删除后仍可 10 秒内撤销）`
        }
        m.confirm = null
      } else {
        m.executed = '已完成'
        m.confirm = null
      }
    } catch (e) {
      m.executed = '执行失败：' + (e.message || '网络错误')
      m.confirm = null
    } finally {
      scrollBottom()
    }
  }

  function cancelAction(m) {
    if (m.confirm) m.confirm = null
  }

  return {
    state,
    init, send, stop, togglePanel, closePanel,
    onPointerDown, askQuick, loadHistory, clearConversation,
    confirmAction, cancelAction, md2html,
    updateQuick, goPage,
    panelStyle, onResizeDown, toggleMax,
    onMessagesScroll, scrollBottom, jumpToBottom,
    onInputKey, autoGrow, copyMsg,
    // 本轮新增：分段 / 建议 / 重新生成 / 评分 / 朗读 / 语音输入 / 音效 / 导出
    segments, isLastAi, askSuggest, regenerate, rate,
    speak, stopSpeak, toggleListen, stopListen,
    toggleSound, exportChat,
    quickOf: () => state.quick,
  }
}
