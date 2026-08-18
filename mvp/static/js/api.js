/**
 * api.js —— 统一 HTTP 客户端（AI 应用工程的"请求层"）
 * =====================================================
 * 职责：把散布在各处的 fetch 调用收敛成一处，统一处理：
 *   1. 错误规范化   —— 后端 FastAPI 的错误格式（detail）统一转成带 msg 的 Error
 *   2. Loading 计数 —— 全局请求计数，供 UI 订阅显示"加载中"
 *   3. 超时控制     —— 可选 AbortController 超时，避免请求卡死
 *   4. 幂等重试     —— 可选对网络错误/5xx 自动重试
 *   5. SSE 流式     —— 解析 POST 的 SSE 流（step/result 事件）
 *
 * 为什么值得抽这一层？
 *   - 单一职责：业务代码只关心"发请求、拿结果"，不关心请求细节
 *   - 可复用：错误处理、超时、loading 全项目统一，改一处全局生效
 *   - 可测试：请求层独立，能用 mock 单独测，不依赖 UI
 */

// 默认不超时（0 = 不主动中断）。生产环境建议给具体值，如 120_000。
// 这里默认关闭是为了兼容现有 LLM 长生成请求，不破坏原有行为。
const DEFAULT_TIMEOUT = 0

// ---------------------------------------------------------------------------
// 全局 Loading 计数
//   原理：多个请求并发时，loading 应为"还有请求在飞"（计数>0），而不是
//   每个请求各自为政导致闪烁。通过订阅回调，UI 可统一显示/隐藏全局加载条。
// ---------------------------------------------------------------------------
let _loadingCount = 0
let _onLoadingChange = null

/** 订阅全局 loading 变化。回调收到 boolean：true=有请求在飞，false=全部完成。 */
export function setLoadingListener(fn) {
  _onLoadingChange = fn
}

function _notifyLoading() {
  if (_onLoadingChange) _onLoadingChange(_loadingCount > 0)
}

// ---------------------------------------------------------------------------
// 请求计数辅助（内部）
// ---------------------------------------------------------------------------
function _incLoading() { _loadingCount++; _notifyLoading() }
function _decLoading() { _loadingCount = Math.max(0, _loadingCount - 1); _notifyLoading() }

// ---------------------------------------------------------------------------
// 错误规范化
//   FastAPI 的 HTTPException 返回 {"detail": ...}，detail 可能是字符串、
//   {message:...} 对象、或校验错误的数组。这里统一抽成一条可读消息。
// ---------------------------------------------------------------------------
async function _toError(resp) {
  const err = await resp.json().catch(() => ({ detail: '请求失败' }))
  const detail = err.detail
  let msg = '请求失败'
  if (Array.isArray(detail)) {
    msg = detail.map(d => (d && d.msg) || JSON.stringify(d)).join('；')
  } else if (typeof detail === 'string') {
    msg = detail
  } else if (err && err.message) {
    msg = err.message
  } else if (detail && typeof detail === 'object') {
    msg = JSON.stringify(detail)
  }
  return { msg, status: resp.status }
}

/** 判断一个错误是否值得"幂等"重试：网络错误（TypeError）或服务端 5xx。 */
function _isRetryable(errObj) {
  if (errObj.status >= 500) return true
  return errObj.status === 0 // fetch 网络层失败时原生 Error 无 status
}

// ---------------------------------------------------------------------------
// 通用请求
//   签名与原 index.html 里的 api() 兼容，另支持可选配置：
//     opts.headers       自定义请求头
//     cfg.retries        幂等重试次数（默认 0）
//     cfg.timeout        超时毫秒（默认不超时）
//     cfg.withLoading    是否计入全局 loading（默认 true）
// ---------------------------------------------------------------------------
export async function api(url, opts = {}, cfg = {}) {
  const { retries = 0, timeout = DEFAULT_TIMEOUT, withLoading = true } = cfg
  if (withLoading) _incLoading()

  const controller = timeout > 0 ? new AbortController() : null
  const timer = controller ? setTimeout(() => controller.abort(), timeout) : null

  try {
    let attempt = 0
    while (true) {
      try {
        const resp = await fetch(url, {
          ...opts,
          headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
          signal: controller ? controller.signal : undefined,
        })
        if (!resp.ok) {
          const errObj = await _toError(resp)
          if (attempt < retries && _isRetryable(errObj)) {
            attempt++
            await new Promise(r => setTimeout(r, 500 * attempt)) // 退避等待
            continue
          }
          throw new Error(errObj.msg)
        }
        return await resp.json()
      } catch (e) {
        // 网络层错误（TypeError）也纳入重试
        if (attempt < retries && _isRetryable({ status: 0 })) {
          attempt++
          await new Promise(r => setTimeout(r, 500 * attempt))
          continue
        }
        throw e
      }
    }
  } finally {
    if (timer) clearTimeout(timer)
    if (withLoading) _decLoading()
  }
}

/**
 * SSE 流式请求（POST）
 *   用 fetch + ReadableStream 手动解析 SSE，因为原生 EventSource 不支持 POST。
 *   content 格式：`event: step\ndata: {...}\n\n` 与 `event: result\ndata: {...}\n\n`
 *   回调：onStep(payload) 逐条收到 step 事件；onResult(payload) 收到最终 result。
 */
export async function apiStream(url, opts = {}, { onStep, onResult } = {}, cfg = {}) {
  const { timeout = DEFAULT_TIMEOUT, withLoading = true, signal: externalSignal } = cfg
  if (withLoading) _incLoading()

  // 支持外部取消（生成取消按钮）：外部 AbortController.abort() 时一并中断本请求
  const controller = new AbortController()
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else externalSignal.addEventListener('abort', () => controller.abort(), { once: true })
  }
  const timer = timeout > 0 ? setTimeout(() => controller.abort(), timeout) : null

  try {
    const resp = await fetch(url, {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      signal: controller.signal,
    })
    if (!resp.ok) {
      const errObj = await _toError(resp)
      throw new Error(errObj.msg)
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result = null

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)

        let event = 'message'
        let data = ''
        for (const line of raw.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          else if (line.startsWith('data:')) data += line.slice(5).trim()
        }
        if (!data) continue

        let payload
        try { payload = JSON.parse(data) } catch (_) { continue }
        if (event === 'step' && onStep) onStep(payload)
        else if (event === 'result') { result = payload; if (onResult) onResult(payload) }
      }
    }
    return result
  } finally {
    if (timer) clearTimeout(timer)
    if (withLoading) _decLoading()
  }
}