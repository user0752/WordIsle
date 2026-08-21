/**
 * constants.js —— 前端静态常量
 * ============================
 * 只放"纯数据"：不依赖任何运行时状态，写死即可。
 * 判断标准：别的文件 import 它不会带来任何副作用或参数传递。
 */

/** TTS 模型配置（音色系列 / 价格提示 / 推荐音色） */
export const TTS_MODELS = [
  {value:'qwen-audio-3.0-tts-plus',label:'Qwen-Audio TTS Plus (最佳音质·48kHz·指令控制)',group:'Qwen-Audio-TTS 系列',voices:'longanhuan_v3.6(系统默认·支持多语种)'},
  {value:'cosyvoice-v3-flash',label:'CosyVoice v3 Flash (快速·性价比高·指令控制)',group:'CosyVoice 系列',voices:'loongandy_v3(美式男), loongbeth_v3(美式女), loongemily_v3(英式女), loongeric_v3(英式男)'},
]