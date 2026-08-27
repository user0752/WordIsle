/**
 * assistant.js —— 词小屿（全局智能助手）前端逻辑
 * =================================================
 * 职责：悬浮挂件（拖动/位置记忆/状态反馈）+ 对话面板（SSE 流式渲染、
 * 快捷问题/场景感知、操作确认卡片、会话持久化恢复、清空会话、停止生成）。
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
    return `\n<${tag} class="cxy-list">${items}</${tag}>`
  })
  // 换行 → <br>
  html = html.replace(/\n+/g, '<br>')
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
    // 内部
    controller: null,
    streamMsg: null,        // 当前流式渲染中的助手消息
  })

  // ---------- 生命周期 ----------
  async function init() {
    updateQuick()
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
      scrollBottom()
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
        streaming: false,
        navigate: null, confirm: null, queryData: null, error: null, executed: null,
      }))
      if (state.messages.length === 0) {
        pushMsg({ role: 'assistant', kind: 'chat', text: '你好呀，我是词小屿 👋 词屿的贴身向导。想了解某个功能怎么用，或想让我帮你查词，直接说就行～',
          streaming: false })
      }
      scrollBottom()
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
      streaming: false, navigate: null, confirm: null, queryData: null,
      errorChunk: null, executed: null, executing: false, stepLabel: '',
    }, partial)
    state.messages.push(m)
    scrollBottom()
    return m
  }

  async function scrollBottom() {
    await nextTick()
    const el = document.querySelector('.cxy-messages')
    if (el) el.scrollTop = el.scrollHeight
  }

  // ---------- 发送 / 流式接收 ----------
  async function send() {
    const text = state.input.trim()
    if (!text || state.busy) return
    state.input = ''
    pushMsg({ role: 'user', kind: 'chat', text })

    const m = pushMsg({ role: 'assistant', kind: 'chat', text: '', streaming: true })
    state.streamMsg = m
    state.busy = true
    state.canStop = true

    const controller = new AbortController()
    state.controller = controller
    let gotContent = false
    let finalized = false
    const _finish = (c, gc) => { if (!finalized) { finalized = true; finalize(m, c, gc) } }

    try {
      await apiStream('/api/assistant/chat',
        { method: 'POST', body: JSON.stringify({ message: text, page: getPage() }) },
        {
          onStep: (p) => { m.stepLabel = (p && p.label) || '' },  // 意图识别等分步反馈
          onTool: (p) => handleTool(p, m),
          onResult: (p) => {
            gotContent = true
            m.text += (p && p.text) || ''
            scrollBottom()   // 流式过程中随时滚到底部，保证可见新内容
          },
          onDone: () => _finish(controller, gotContent),
          onError: (p) => {
            m.errorChunk = (p && p.msg) || '出错了，请稍后再试'
            _finish(controller, gotContent)
          },
        },
        { signal: controller.signal, withLoading: false })
    } catch (e) {
      if (e && e.name === 'AbortError') {
        m.text = gotContent ? m.text + '（已停止生成）' : '已停止生成'
      } else {
        m.errorChunk = (e && e.message) || '网络异常，请稍后再试'
      }
      _finish(controller, gotContent)
    }
  }

  function handleTool(p, m) {
    if (!p) return
    if (p.tool === 'navigate') {
      m.navigate = { page: (p.args || {}).page, label: p.human_readable || '' }
      return
    }
    if (p.confirm_required) {
      m.confirm = { tool: p.tool, args: p.args || {}, human: p.human_readable || {}, executing: false }
      return
    }
    // 查询工具 → 结构化结果卡片（数据在后端已执行）
    m.queryData = { tool: p.tool, args: p.args || {}, human: p.human_readable || '', data: p.data || {} }
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
    scrollBottom,
    quickOf: () => state.quick,
  }
}