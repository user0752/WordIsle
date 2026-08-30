/**
 * utils.js —— 通用纯函数
 * ======================
 * 只放"纯函数"：同样的输入永远得到同样的输出，且不依赖任何页面状态。
 * 这类函数放这里，任何组件/页面 import 都能直接复用，无需传一堆参数。
 */

/** 日期格式化：2026-08-13T.. → "2026/08/13 14:30"；非法输入返回 '-'。 */
export function formatDate(d) {
  if (!d) return '-'
  const dt = new Date(d)
  if (isNaN(dt)) return '-'
  return dt.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
}

/** 仅日期（单词库导入时间用，不显示时分） */
export function formatDateOnly(d) {
  if (!d) return '-'
  const dt = new Date(d)
  if (isNaN(dt)) return '-'
  return dt.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'})
}

/** 场景角色代号转中文：setup→起 / development→承 / climax→转 / resolution→合 */
export function sceneRoleText(v) {
  const m = {setup:'起',development:'承',climax:'转',resolution:'合'}
  return m[v] || v || ''
}

/** TTS 模型 → 默认推荐音色（展示用） */
export function getModelDefaultVoice(model) {
  const map = {
    'qwen-audio-3.0-tts-plus':'loongmary (温暖英音·女)',
    'cosyvoice-v3-plus':'loongandy_v3 (美式英文男)',
    'cosyvoice-v3-flash':'loongandy_v3 (美式英文男)',
  }
  return map[model] || 'loongandy_v3 (美式英文男)'
}

/**
 * 在英文句子里高亮命中单词（含词形变化），返回带 <span class="word-hit"> 的 HTML。
 * 纯函数：只依赖入参 sentence 和 words，不依赖任何页面状态。
 */
export function highlightPanelWords(sentence, words) {
  if (!sentence) return ''
  let text = sentence.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  if (!words || !words.length) return text
  const sorted = [...words].sort((a,b)=>b.length-a.length)
  const placeholders = []
  for (const w of sorted) {
    const escaped = w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')
    const stem = (w.length > 3 && w.endsWith('e')) ? w.slice(0, -1) : ''
    const prefixes = stem ? '(?:' + escaped + '|' + stem + ')' : escaped
    const re = new RegExp('\\b' + prefixes + '(?:s|es|ed|d|ing|ings|ly|tion|tions|ment|ments|er|est|ion|ied|\'s)?\\b','gi')
    text = text.replace(re, (m) => {
      const ph = '\u0000' + placeholders.length + '\u0000'
      placeholders.push(m)
      return ph
    })
  }
  placeholders.forEach((m, i) => {
    text = text.split('\u0000'+i+'\u0000').join('<span class="word-hit">'+m+'</span>')
  })
  return text
}

/**
 * AI 调用失败错误诊断：把原始错误消息映射成 { msg, tip }。
 * 纯函数：只依赖错误文本，不依赖页面状态。tip 为给用户的可操作建议。
 */
export function humanizeError(err) {
  const msg = (err && err.message) || String(err || '')
  const lower = msg.toLowerCase()
  // 配额/额度相关
  if (/配额|quota|已达上限|额度|balance|insufficient|免费|expired|过期/.test(msg)) {
    return { msg, tip: '今日 AI/模型额度可能已用完，可到"设置"页更换其他模型，或明天再试' }
  }
  // 模型不存在 / 已被下线
  if (/no such model|model not found|invalid model|不存在的模型|模型.*不存在|已被下线|不存在.*模型/.test(lower)) {
    return { msg, tip: '所选模型不可用或已被平台下线，请到设置页/下拉框重新选择模型后重试' }
  }
  // 限流
  if (/429|rate.?limit|too many requests|请求过于频繁|限流/.test(lower)) {
    return { msg, tip: '请求过于频繁被限流，请稍等片刻后再试' }
  }
  // 网络/超时
  if (/network|timeout|超时|connection|连接|reset|disconnect/.test(lower)) {
    return { msg, tip: '网络连接异常，请检查网络后重试' }
  }
  // 文生图失败
  if (/文生图|图片.*失败|image.*fail|生成.*图.*失败/.test(lower)) {
    return { msg, tip: '文生图失败，建议更换其他文生图模型后重试' }
  }
  return { msg, tip: '' }
}