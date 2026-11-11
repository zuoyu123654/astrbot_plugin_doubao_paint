# 豆包画画（astrbot_plugin_doubao_paint）

让她在 QQ 里**真的能画画**：你说「画一张……」，她用本机「豆包」出图，图直接发回来。

用的是 DSH 插件 **dsh-doubao-painter**（CDP 驱动豆包桌面版，拿无水印原图，不花钱、不走第三方 API）。

---

## 它解决的三件事

1. **图生图**
   - 图和需求**一起发**：用消息里那张图当参考图。
   - **先发图、后说需求**：插件记住最近 30 分钟发过的图，说「照这张画」时自动用上。
   - **先发需求、后发图**：说「照这张画成水彩」但没带图 → 插件先把需求挂起来（默认 5 分钟），
     回一句「把图发来」；你下一条发图，它**自动接着画**并发回来，不用再喊一遍。
2. **她自己的形象**：内置两张角色设定图（Q 版 / 成熟版）。说「画一张你现在的状态」时，
   用角色图当参考图，保持蓝发、呆毛、鲸鳍耳、女仆裙、鲸尾一致。
3. **按状态画（只在「画她自己」时生效）**：
   天气优先从陪伴插件 v2 的 sqlite（`proactive_weather_samples`，qweather 采样）读真实值，**只取天气/温度/湿度，不带地区**，读不到才用旧的 `daily_weather`；再没有就写「天气未知，不要自己编」。
说「画一张你现在的状态」时，才把
   **时间/白天黑夜 + 真实天气 + 情绪/精力/身体状态** 放到提示词最前面并强调必须对上；
   其它普通画图**不掺**这些，免得把一张猫画成深夜。

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
| `pending_image_minutes` | 5 | 「先发需求后发图」等图等多久 |
| `auto_paint_on_next_image` | true | 等到图后自动接着画（关掉就只回一句「把图发来」） |
| `read_state` | true | 读陪伴插件状态（只读） |
| `state_plugin` | `astrbot_plugin_private_companion` | 读哪个插件 |
| `state_read_all_files` | false | 默认只读 `companions.json`；别开 |
| `self_ref_cute` | 空 | Q 版角色图路径；空 = 在 `refs/` 里按文件名找 |
| `self_ref_mature` | 空 | 成熟版角色图路径；空 = 同上 |

### 角色参考图怎么放

```
<AstrBot>/data/plugin_data/astrbot_plugin_doubao_paint/refs/
├── 默认（Q版可爱 女仆装）.png
└── 成熟（长大）.jpg
```

- **插件不自带图**，刚装完 `refs/` 是空的；空着也能画——会用默认的「蓝色大肥鱼」文字描述，这段可以用 `self_appearance` 改成你自己的角色。
- **按文件名挑图**，括号里的字也算：说「成熟」就挑带「成熟」的，说「女仆装」就挑带「女仆装」的；
  都不匹配就用名字带「默认」的那张。
- **不限张数**：加多少张都行，文件名写清内容/场景（`泳装（海边 夏天）.png`、`睡衣（居家）.png`）。
- 模型可以先调 `doubao_list_refs` 看有哪些图；画她自己前会先调 `doubao_now_playing`
  （读当天计划里「现在在演什么」），再由模型写成画面提示词交给豆包。

## 工具

| 工具 | 干什么 |
| --- | --- |
| `doubao_paint(prompt, self_portrait, with_state, mature)` | 出图并把图发回当前会话 |
| `doubao_paint_status()` | 自检：painter 脚本、node、豆包调试端口、参考图、状态读取 |

`doubao_paint` 是给模型调的：你说「画一张…」它就会用；你说「画一张你现在的状态」，
它会带 `self_portrait=true, with_state=true`。

---

## 她可以自己想画就画

聊天里聊到好吃的、好看的、她今天干了什么，或者她就是想让你看看她现在的样子——
她可以直接画一张发过来，不用你先说「画一张」。

- `allow_self_initiated`（默认 true）：开/关这个能力。
- `self_initiated_cooldown_minutes`（默认 30）：她自己主动画的冷却，防刷屏；0 = 不限制。
- 你明确要求画的（"画一只猫"）**不受冷却影响**。

## 权限（默认只有主人能用）

`access_mode` 三档：

| 值 | 谁能触发 |
| --- | --- |
| `owner`（默认） | 只有主人——`data/cmd_config.json` 里 `admins_id` 那几个 QQ 号，外加平台里 `role=admin` 的人 |
| `whitelist` | 主人 + `allowed_users` 列表里的人 |
| `everyone` | 所有人都能 |

- 控制的是这套工具：`doubao_paint` / `doubao_paint_status` / `doubao_list_refs` / `doubao_now_playing`。
- 被拦下时她只回一句「这个只给老大用，你别乱点。」，不会画、也不暴露路径。
- 想加人：`"access_mode": "whitelist"`, `"allowed_users": ["10086"]`。

## 换成别的生图接口（不用豆包也行）

`image_backend` 换成 `openai`，就能接**任何 OpenAI 兼容**的 `/images/generations` 接口
（OpenAI、硅基流动、各种中转站、自建服务都算）：

```json
{
  "image_backend": "openai",
  "api_base": "https://api.siliconflow.cn/v1",
  "api_key": "sk-xxxx",
  "image_model": "Kwai-Kolors/Kolors",
  "image_size": "1024x1024",
  "image_count": 1,
  "use_edits": true
}
```

- **纯文字**：POST `{api_base}/images/generations`，收 `b64_json` 或 `url`，存下来发回 QQ。
- **带参考图**：走 `{api_base}/images/edits`（multipart，字段名 `image`）；接口不支持就把
  `use_edits` 关掉——那就只按文字画，参考图会被忽略。
- 返回格式要 OpenAI 那套：`{"data":[{"b64_json": "..."}]}` 或 `{"data":[{"url": "..."}]}`。
- 豆包后端（`image_backend: doubao`）仍然照旧，两套互不影响。

## 启动与发送的两个坑（0.7.2 修）

- **冷启动慢**：豆包第一次启动要十几秒。现在 AstrBot 一起来就在后台把它拉起来
  （`prewarm_doubao`，默认开），窗口会**挪到屏幕外**（不是最小化——最小化会让豆包不处理输入，发送按钮点不动），你看不见它，但它正常渲染、能收键盘鼠标。第一次画图也直接可用。
- **发送没反应**：豆包空白新对话页上「回车」是发不出去的（页面没绑 Enter）。
  现在改成点 `button[data-testid="chat_input_send_button"]`（可信鼠标事件）；
  如果点了还没反应（页面状态卡住），会**自动重启豆包再发一次**。

## 为什么要「先回一句在画了」

豆包出一张图要 1~2 分钟，而 AstrBot 的工具调用有 **120 秒上限**——同步等一定会被掐断（日志里是 `tool doubao_paint execution timeout after 120 seconds`）。
所以默认 `async_paint: true`：工具立刻返回「在画了」，画完由后台把图直接发回会话。
关掉它（`async_paint: false`）就会退回同步等待，只适合 API 后端那种十几秒出图的。

## 豆包窗口怎么处理（0.7.5）

- **平时藏起来**：预热/画图时窗口被挪到屏幕外（`-3200,-3200`），你看不见它，也不会弹出来；
  它没被最小化，所以照常渲染、照常收键盘鼠标。
- **画完放回来**：`restore_after_paint`（默认开）会在每次出图后把窗口放回屏幕上原来的位置，
  **不抢焦点**——你想自己用豆包，它就在那儿。
- **随时手动**：跟她说「把豆包窗口放出来」/「把豆包藏起来」，会调 `doubao_window` 工具（show/hide）。

> 顺带说明：豆包**没有**「生图模式」开关。dsh-doubao-painter 也是把提示词直接发进普通对话，
> 由豆包自己决定要不要画。所以这里的关键不是切模式，而是
> ① 提示词里带「画一张图」这类生图意图（会自动补前缀）；② 真的把消息发出去（点发送按钮）。

## 本地豆包怎么驱动（v0.7.0 起）

插件自己连豆包的调试端口（默认 9225），流程：

1. 没有调试端口 → 先看豆包是不是普通模式开着，是就关掉、用 `--remote-debugging-port` 重开；
2. 找 `chrome://doubao-chat/chat` 页面，开新对话；
3. 往输入框 `Input.insertText` 写提示词，回车发送；
4. 等新图出现（先等历史图渲染稳定，避免把上一张当新图）；
5. **点开预览**（CDP 真实鼠标事件）把带水印缩略图换成 `cgen` 原图；拿不到再试两次；
6. 下载并校验尺寸，384 缩略图会重试。

**提示词会自动补生图意图**：豆包是按意图判断的，光写「一只猫趴在窗台」它会当聊天回你一段文字。
所以提示词里没有「画 / 生成 / 插画 / 图片」这类词时，插件会自动加「画一张图：」前缀。

配置：
- `cdp_port`（默认 9225）
- `app_exe`（留空自动找 `%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe` 等）
- `auto_launch_doubao`（默认开：没端口时自动拉起）
- `restart_when_no_cdp`（默认开：豆包普通模式开着时自动关掉重开）

## 参考图会被先「看一眼」

图 + 需求一起发、先图后话、先话后图——三种情况在拿用户的图当参考图之前，都会先让模型
描述一下图里是什么，写进提示词（`【参考图里是】…`）。
**默认识图用当前对话的 provider（DeepSeek 实测能看图）**，`caption_provider` 只在想换别的模型看图时才填；
看图失败自动跳过。

## 关于水印（实测结论，2026-11-08）## 关于水印（实测结论，2026-11-08）

- 插件拿到的是豆包 CDN 上的 **cgen 原图**（1536×1536 / 1920×1280），不是 384px 缩略图——
  这一点和上游 `dsh-doubao-painter` 的做法一致（它也是取 cgen）。
- 但**免费套餐的账号，cgen 原图右下角仍然烧了「豆包 AI生成」水印**。实测：
  - 同一张图的其它 CDN 变体（`cgen_wm0`、`cgen_wm0_hflow`、去掉 `~tplv` 等）全部 **403**，
    签名只对豆包自己发的那条 URL 有效，改不了。
  - 预览层里没有「下载原图」按钮（点开只是放大），拿不到第二份文件。
  - 本地反向混合去水印能压淡，但会留鬼影（水印是白字 + 深色描边），不划算，所以**没做**。
- **想要真正无水印**：用开了会员的豆包账号登录（代码不用改，cgen 就是干净的）。
  免费账号目前只能拿到「高清 + 带水印」。

## 已知边界

- **一次一张**：豆包一个会话一次出一组图，插件加了锁，同时只有一个人在画；排队要等。
- **慢**：一轮 20~90 秒，QQ 那边会干等。插件会在失败/超时的时候直接回你原因。
- **豆包得登录**：没登录/被风控，painter 会报错，插件把错误原话发给你。
- **会开新对话**：每次画图都在豆包的「DSH 和豆包的秘密画室」项目里新开一段，不会污染你自己的聊天。
- **图会过期**：`keep_days` 天后自动删。
