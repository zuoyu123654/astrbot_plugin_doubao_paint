# 豆包画画（astrbot_plugin_doubao_paint）

让她在 QQ 里**真的能画画**：你说「画一张……」，她用本机「豆包」出图，图直接发回来。

用的是 DSH 插件 **dsh-doubao-painter**（CDP 驱动豆包桌面版，拿无水印原图，不花钱、不走第三方 API）。

---

## 它解决的三件事

1. **图生图**
   - 图和需求**一起发**：用消息里那张图当参考图。
   - **分开发**（先发图，隔一会儿再说「照这个画」）：插件记住最近 30 分钟发过的图，照样能用。
2. **她自己的形象**：内置两张角色设定图（Q 版 / 成熟版）。说「画一张你现在的状态」时，
   用角色图当参考图，保持蓝发、呆毛、鲸鳍耳、女仆裙、鲸尾一致。
3. **按状态画**：只读 `astrbot_plugin_private_companion` 里此刻的情绪/精力/天气，
   写进给豆包的提示词——画出来的是「现在的她」，不是随便一张立绘。

---

## 前置

| 需要 | 说明 |
| --- | --- |
| 豆包桌面客户端 | **已安装、已登录**（生图用它的免费额度） |
| `dsh-doubao-painter` | DSH 插件，本机 `~/.dsh/plugins/dsh-doubao-painter`；负责 CDP 驱动豆包 |
| `node` | >= 22（painter 是 Node 写的） |
| ffmpeg | 不需要（那是语音视频插件的事） |

---

## 配置

文件：`<AstrBot>/data/config/astrbot_plugin_doubao_paint_config.json`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `painter_dir` | 空 | painter 目录；空 = `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter` |
| `node_path` | `node` | node 可执行文件 |
| `app_exe` | 空 | 豆包主程序；空 = 让 painter 自动探测（`D:\Doubao\app\Doubao.exe`、`%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe`…） |
| `cdp_port` | 9225 | 豆包调试端口 |
| `timeout_seconds` | 240 | 一次生成最多等多久 |
| `out_dir` | 空 | 图存哪；空 = 插件数据目录 `images/` |
| `keep_days` | 7 | 图保留几天 |
| `recent_image_minutes` | 30 | 「先发图后说需求」记多久 |
| `read_state` | true | 读陪伴插件状态（只读） |
| `state_plugin` | `astrbot_plugin_private_companion` | 读哪个插件 |
| `state_read_all_files` | false | 默认只读 `companions.json`；别开 |
| `self_ref_cute` | 空 | Q 版角色图路径；空 = 在 `refs/` 里按文件名找 |
| `self_ref_mature` | 空 | 成熟版角色图路径；空 = 同上 |

### 角色参考图怎么放

```
<AstrBot>/data/plugin_data/astrbot_plugin_doubao_paint/refs/
├── self_cute.png     ← Q 版（三视图最好）
└── self_mature.jpg   ← 成熟版
```

文件名里带 `cute` / `q版` / `chibi` 的会被当成 Q 版；带 `mature` / `成熟` 的是成熟版。
没有这两张图也能画，只是「画你自己」时没有形象参考。

---

## 工具

| 工具 | 干什么 |
| --- | --- |
| `doubao_paint(prompt, self_portrait, with_state, mature)` | 出图并把图发回当前会话 |
| `doubao_paint_status()` | 自检：painter 脚本、node、豆包调试端口、参考图、状态读取 |

`doubao_paint` 是给模型调的：你说「画一张…」它就会用；你说「画一张你现在的状态」，
它会带 `self_portrait=true, with_state=true`。

---

## 已知边界

- **一次一张**：豆包一个会话一次出一组图，插件加了锁，同时只有一个人在画；排队要等。
- **慢**：一轮 20~90 秒，QQ 那边会干等。插件会在失败/超时的时候直接回你原因。
- **豆包得登录**：没登录/被风控，painter 会报错，插件把错误原话发给你。
- **会开新对话**：每次画图都在豆包的「DSH 和豆包的秘密画室」项目里新开一段，不会污染你自己的聊天。
- **图会过期**：`keep_days` 天后自动删。
