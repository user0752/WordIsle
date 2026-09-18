/**
 * captcha.js —— 弹窗式滑块拼图验证组件（极验 GeeTest / 腾讯云同款交互范式）
 * =====================================================================
 * 交互流程（与主流网页验证一致）：
 *   页面点击「获取验证码」→ Captcha.open() 弹出模态框 → 加载拼图背景
 *   → 按住滑块拖动把拼图块对准缺口 → 松手触发 /api/captcha/verify（服务端权威校验）
 *   → 通过：绿色勾选反馈并 resolve({captchaId, x})，调用方随即发送短信
 *   → 失败：抖动提示 + 自动换一张拼图
 *
 * 用法（零依赖，两处模板共用，避免复制粘贴漂移）：
 *   const res = await window.Captcha.open({
 *     getCaptcha: () => fetch('/api/captcha', {cache:'no-store'}).then(r => r.json()),
 *     verify: async (captchaId, x) => {
 *       const r = await fetch('/api/captcha/verify', {method:'POST', ...});
 *       return (await r.json()).ok === true;
 *     },
 *   });
 *   if (res) { /* res = {captchaId, x}，调用 /api/sms/send *\/ }
 *   // res === null 表示用户取消/加载失败
 *
 * 安全模型：
 *   - 目标 x 坐标只存服务端（/api/captcha 不下发答案），松手只校验不消费；
 *   - /api/sms/send 再次消费校验（一次性+verified 标记），无法绕过图形验证直接发短信。
 */
(function (global) {
  'use strict'

  // ---------- DOM（单例，首次 open 时构建，复用于全部页面） ----------
  let root = null
  let busy = false            // 是否有弹窗在展示（防重入）

  const noop = () => {}
  const TPL = `
  <div class="cpt-mask" role="dialog" aria-modal="true" aria-label="安全验证">
    <div class="cpt-panel">
      <div class="cpt-head">
        <span class="cpt-title">安全验证</span>
        <button type="button" class="cpt-close" aria-label="关闭">✕</button>
      </div>
      <div class="cpt-sub">拖动下方滑块，将拼图对准缺口</div>
      <div class="cpt-stage">
        <img class="cpt-bg" alt="拼图背景" draggable="false">
        <img class="cpt-piece" alt="拼图块" draggable="false">
        <button type="button" class="cpt-refresh" title="换一张" aria-label="换一张">↻</button>
        <div class="cpt-loading"><span></span></div>
      </div>
      <div class="cpt-track" aria-hidden="true">
        <div class="cpt-fill"></div>
        <div class="cpt-handle"><i></i></div>
        <div class="cpt-tip">按住滑块，拖动完成拼图</div>
      </div>
      <div class="cpt-msg"></div>
    </div>
  </div>`

  // 组件自带全部样式（注入 <head>，避免依赖具体页面 CSS，两页共用观感一致）
  const STYLE = `
  #wordisle-captcha-root{position:fixed;inset:0;z-index:99999;display:none;}
  #wordisle-captcha-root.cpt-open{display:block;}
  .cpt-mask{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(15,23,32,.55);backdrop-filter:blur(3px);}
  .cpt-panel{box-sizing:border-box;width:340px;max-width:calc(100vw - 28px);background:#fff;border-radius:14px;padding:20px;box-shadow:0 24px 60px rgba(0,0,0,.28);animation:cptIn .24s cubic-bezier(.22,1,.36,1);}
  @keyframes cptIn{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}
  .cpt-head{display:flex;align-items:center;justify-content:space-between;}
  .cpt-title{font-size:16px;font-weight:700;color:#1d2129;}
  .cpt-close{border:none;background:transparent;font-size:15px;color:#86909c;cursor:pointer;width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;}
  .cpt-close:hover{background:#f2f3f5;color:#4e5969;}
  .cpt-sub{font-size:12.5px;color:#86909c;margin:6px 0 12px;}
  .cpt-stage{position:relative;width:min(300px,100%);aspect-ratio:3/1;border-radius:10px;overflow:hidden;background:#eef0f3;}
  .cpt-stage .cpt-bg{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;display:block;}
  .cpt-piece{position:absolute;top:22px;left:0;border-radius:8px;pointer-events:none;user-select:none;filter:drop-shadow(0 3px 6px rgba(0,0,0,.40));}
  .cpt-refresh{position:absolute;right:6px;top:6px;z-index:2;width:26px;height:26px;border:none;border-radius:8px;background:rgba(255,255,255,.9);box-shadow:0 1px 4px rgba(0,0,0,.12);color:#4e5969;font-size:14px;cursor:pointer;opacity:0;transition:opacity .2s;}
  .cpt-ready .cpt-refresh{opacity:1;}
  .cpt-refresh:hover{color:#1d2129;}
  .cpt-loading{position:absolute;inset:0;display:none;align-items:center;justify-content:center;background:#eef0f3;}
  .cpt-loading.cpt-on{display:flex;}
  .cpt-loading span{width:22px;height:22px;border-radius:50%;border:2.5px solid #c9cdd4;border-top-color:#3a7d6a;animation:cptSpin .7s linear infinite;}
  @keyframes cptSpin{to{transform:rotate(360deg)}}
  .cpt-track{position:relative;box-sizing:border-box;width:min(300px,100%);height:40px;margin-top:14px;border-radius:20px;background:#f2f3f5;border:1px solid #e5e6eb;overflow:hidden;}
  .cpt-fill{position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#5bb98c,#3a7d6a);transition:width .08s linear;}
  .cpt-dragging .cpt-fill{background:linear-gradient(90deg,#8fc3e6,#4a9dc9);}
  .cpt-success .cpt-fill{background:linear-gradient(90deg,#52c41a,#389e0d);}
  .cpt-fail .cpt-fill{background:linear-gradient(90deg,#ff7875,#cf1322);}
  .cpt-handle{position:absolute;left:0;top:-1px;width:42px;height:42px;border-radius:50%;background:#fff;border:1px solid #e5e6eb;box-shadow:0 3px 8px rgba(0,0,0,.18);display:flex;align-items:center;justify-content:center;cursor:grab;touch-action:none;z-index:2;}
  .cpt-handle:active{cursor:grabbing;}
  .cpt-handle i{width:14px;height:7px;border-top:2px solid #4e5969;border-bottom:2px solid #4e5969;position:relative;}
  .cpt-handle i::after{content:'';position:absolute;left:50%;top:-1px;transform:translateX(-50%);width:2px;height:9px;background:#4e5969;}
  .cpt-success .cpt-handle{background:#52c41a;border-color:#52c41a;box-shadow:0 3px 8px rgba(82,196,26,.4);}
  .cpt-success .cpt-handle i{opacity:0;}
  .cpt-success .cpt-handle::after{content:'✓';font-size:18px;color:#fff;font-weight:700;}
  .cpt-tip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:13px;color:#86909c;z-index:1;transition:opacity .15s;pointer-events:none;letter-spacing:.5px;}
  .cpt-dragging .cpt-tip{opacity:0;}
  .cpt-fail{animation:cptShake .4s;}
  @keyframes cptShake{20%{transform:translateX(-8px)}40%{transform:translateX(6px)}60%{transform:translateX(-4px)}80%{transform:translateX(3px)}}
  .cpt-msg{min-height:16px;margin-top:8px;font-size:12.5px;color:#86909c;text-align:center;}
  .cpt-msg-ok{color:#389e0d;font-weight:600;}
  .cpt-msg-err{color:#cf1322;}
  body.cpt-lock{overflow:hidden;}
  @media (prefers-reduced-motion:reduce){.cpt-panel,.cpt-fill{animation:none;transition:none;}}`

  function ensureDOM() {
    if (root) return
    if (!document.getElementById('wordisle-captcha-style')) {
      const st = document.createElement('style')
      st.id = 'wordisle-captcha-style'
      st.textContent = STYLE
      document.head.appendChild(st)
    }
    root = document.createElement('div')
    root.id = 'wordisle-captcha-root'
    root.innerHTML = TPL
    document.body.appendChild(root)
    // 弹窗为单例：事件只在此处绑定一次，避免多次 open() 叠加监听
    root.querySelector('.cpt-close').addEventListener('click', () => {
      const p = pending; closeModal(); if (p) p.resolve(null)
    })
    root.querySelector('.cpt-refresh').addEventListener('click', () => {
      setVPhase('idle'); setMsg(''); resetUI(true); fetchCaptcha()
    })
    root.addEventListener('mousedown', (e) => {
      if (e.target === root.querySelector('.cpt-mask')) {
        const p = pending; closeModal(); if (p) p.resolve(null)
      }
    })
    const handle = root.querySelector('.cpt-handle')
    handle.addEventListener('pointerdown', onPointerDown)
    handle.addEventListener('pointermove', onPointerMove)
    handle.addEventListener('pointerup', onPointerUp)
    handle.addEventListener('pointercancel', onPointerUp)
  }

  function $(sel) { return root.querySelector(sel) }

  // ---------- 状态 ----------
  let opts = { getCaptcha: noop, verify: noop }
  let pending = null           // Promise.resolve 句柄
  let captchaId = ''
  let maxPiece = 244           // 拼图块可移动的最大 x（服务端画布单位）
  let pieceSize = 56
  let scale = 1                // 舞台宽度 / 服务端画布宽度（窄屏下拼图块缩放）
  let dx = 0                   // 当前拖动位移
  let dragging = false
  let dragStartX = 0           // pointerdown 相对 handle 起点的偏移
  let verifying = false

  // ---------- DOM 引用 ----------
  const el = () => ({
    mask: $('.cpt-mask'),
    close: $('.cpt-close'),
    refresh: $('.cpt-refresh'),
    bg: $('.cpt-bg'),
    piece: $('.cpt-piece'),
    stage: $('.cpt-stage'),
    track: $('.cpt-track'),
    fill: $('.cpt-fill'),
    handle: $('.cpt-handle'),
    tip: $('.cpt-tip'),
    msg: $('.cpt-msg'),
    loading: $('.cpt-loading'),
  })

  function setMsg(text, kind) {
    const m = el().msg
    m.textContent = text || ''
    m.className = 'cpt-msg' + (kind ? ' cpt-msg-' + kind : '')
  }

  function setLoading(on) {
    el().loading.classList.toggle('cpt-on', !!on)
  }

  function resetUI(instant) {
    dx = 0
    el().piece.style.transition = instant ? 'none' : 'left .12s cubic-bezier(.22,1,.36,1)'
    el().handle.style.transition = 'none'
    el().fill.style.transition = instant ? 'none' : 'width .12s cubic-bezier(.22,1,.36,1)'
    el().piece.style.left = '0px'
    el().handle.style.left = '0px'
    el().fill.style.width = '0px'
    el().tip.style.opacity = '1'
    setTimeout(() => {
      el().piece.style.transition = 'none'
      el().handle.style.transition = 'none'
      el().fill.style.transition = 'none'
    }, 140)
  }

  function setVPhase(state) {
    /* state: 'idle' | 'drag' | 'success' | 'fail' */
    const t = el().track
    t.className = 'cpt-track' + (state === 'drag' ? ' cpt-dragging'
      : state === 'success' ? ' cpt-success'
      : state === 'fail' ? ' cpt-fail' : '')
  }

  function layout(d) {
    captchaId = d.captcha_id
    pieceSize = d.piece_size || 56
    const canvasW = d.width || 300
    maxPiece = Math.max(0, canvasW - pieceSize)
    // 服务端画布 300px；若弹窗舞台被压缩（窄屏），按实际宽度等比缩放拼图块
    const stageW = el().stage.getBoundingClientRect().width || canvasW
    scale = stageW / canvasW
    const { bg, piece } = el()
    // 等两张拼图都就绪再进入可拖状态（用 decode/complete 判定：onload 存在
    // 竞态——piece 先完成会抢先移除 bg 监听导致卡加载，且缓存命中不触发 load）
    const ready = () => {
      setLoading(false)
      el().stage.classList.add('cpt-ready')
    }
    const whenReady = (img) => new Promise((resolve) => {
      if (img.complete && img.naturalWidth > 0) { resolve(); return }
      img.addEventListener('load', resolve, { once: true })
      img.addEventListener('error', resolve, { once: true })   // 加载失败也放行，避免死等
    })
    setLoading(true)
    el().stage.classList.remove('cpt-ready')
    bg.src = d.bg
    piece.src = d.piece
    piece.style.width = Math.round(pieceSize * scale) + 'px'
    piece.style.height = Math.round(pieceSize * scale) + 'px'
    piece.style.top = Math.round(22 * scale) + 'px'
    Promise.all([whenReady(bg), whenReady(piece)]).then(ready)
  }

  async function fetchCaptcha() {
    setLoading(true)
    el().stage.classList.remove('cpt-ready')
    try {
      const d = await opts.getCaptcha()
      if (!d || !d.captcha_id) throw new Error('bad data')
      layout(d)
    } catch (_) {
      setLoading(false)
      setMsg('加载失败，请重试', 'err')
    }
  }

  // ---------- 拖动 ----------
  function move(dxNew, maxDx) {
    dx = Math.max(0, Math.min(maxDx, dxNew))
    el().handle.style.left = dx + 'px'
    el().fill.style.width = (dx + 20) + 'px'
    // 拼图块与滑块位移等比映射（服务端单位 → 舞台像素），保证对齐手感一致
    el().piece.style.left = (dx * (maxPiece / Math.max(1, maxDx)) * scale) + 'px'
  }

  function onPointerDown(e) {
    if (verifying || !el().stage.classList.contains('cpt-ready')) return
    e.preventDefault()
    dragging = true
    dragStartX = e.clientX - el().handle.getBoundingClientRect().left
    el().handle.setPointerCapture(e.pointerId)
    setVPhase('drag')
    setMsg('')
  }

  function onPointerMove(e) {
    if (!dragging) return
    const trackW = el().track.getBoundingClientRect().width
    const handleW = 40
    move(e.clientX - dragStartX - el().track.getBoundingClientRect().left, trackW - handleW)
  }

  async function onPointerUp() {
    if (!dragging) return
    dragging = false
    const trackW = el().track.getBoundingClientRect().width
    const handleW = 40
    const maxDx = trackW - handleW
    if (dx < Math.min(10, maxDx * 0.06)) {           // 毫无移动 → 复位，不算作答
      resetUI(true)
      setVPhase('idle')
      return
    }
    verifying = true
    try {
      const ok = await opts.verify(captchaId, String(Math.round(dx * (maxPiece / Math.max(1, maxDx)))))
      if (ok) {
        setVPhase('success')
        setMsg('验证通过', 'ok')
        const done = pending && pending.resolve
        setTimeout(() => {
          closeModal()
          if (done) done({ captchaId, x: Math.round(dx * (maxPiece / Math.max(1, maxDx))) })
        }, 300)
      } else {
        setVPhase('fail')
        setMsg('验证失败，请重试', 'err')
        setTimeout(() => {
          setVPhase('idle'); setMsg('')
          resetUI(true)
          fetchCaptcha()          // 失败后自动换一张
        }, 650)
      }
    } catch (_) {
      setVPhase('fail')
      setMsg('网络异常，请重试', 'err')
      setTimeout(() => { setVPhase('idle'); setMsg(''); resetUI(true) }, 650)
    } finally {
      verifying = false
    }
  }

  // ---------- 开关 ----------
  function closeModal() {
    if (!root) return
    root.classList.remove('cpt-open')
    document.body.classList.remove('cpt-lock')
    document.removeEventListener('keydown', onKey)
  }

  function onKey(e) {
    if (e.key === 'Escape') {
      const p = pending; closeModal()
      if (p) { p.resolve(null); pending = null }
    }
  }

  function open(o) {
    if (busy) return Promise.resolve(null)
    busy = true
    ensureDOM()
    opts = Object.assign({ getCaptcha: noop, verify: noop }, o || {})
    setMsg('')
    setVPhase('idle')
    resetUI(true)
    root.classList.add('cpt-open')
    document.body.classList.add('cpt-lock')
    document.addEventListener('keydown', onKey)
    fetchCaptcha()

    const p = {}
    pending = p
    p.promise = new Promise(resolve => { p.resolve = resolve })
    p.promise.finally(() => { busy = false; if (pending === p) pending = null })

    return p.promise
  }

  global.Captcha = { open }
})(window)