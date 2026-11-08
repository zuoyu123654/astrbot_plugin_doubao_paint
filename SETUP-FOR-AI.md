# 给 AI 的施工文档：把「豆包画画」配到能在 QQ 里出图

> 读者是**接手配置的 AI**。使用者会把这份文档 + `README.md` 给你，并说一句：
>
> **「请帮我配置好 AstrBot 里的【豆包画画】插件，并验证是否可用。」**
>
> 照做完，应该达到：**在 QQ 里说「画一张……」，图能发回来**；说「画一张你现在的状态」，
> 用的是她自己的形象 + 当前时间/天气/情绪。

---

## 0. 前置插件（缺一样都跑不起来）

| 前置 | 必需？ | 说明 |
| --- | --- | --- |
| **AstrBot ≥ 4.17** | 必需 | 平台必须是 **aiocqhttp（OneBot v11）**，否则发不了图 |
| QQ空间 / QQ资料 等 | 不需要 | 本插件不依赖它们 |
| **豆包桌面客户端** | 只有「本地豆包」路线要 | 装好并**已登录** |
| **dsh-doubao-painter** | 只有「本地豆包」路线要 | 本机 `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter`（CDP 驱动豆包） |
| **node ≥ 22** | 只有「本地豆包」路线要 | painter 是 Node 写的 |
| **生图 API 的 key** | 只有「API」路线要 | 任意 OpenAI 兼容接口（OpenAI / 硅基流动 / 中转站） |

> 两条路线**二选一**，配好哪条都能用；以后想换，改 `image_backend` 再重启即可。

---

## 1. 开工前必须先问使用者（别自己决定）

**第一句就问这个，问清楚再动手：**

> 「生图想用 **① 本地豆包**（免费、要装豆包桌面版），还是 **② 生图模型 API**（填 key 就能用、按量花钱）？」

然后按他的选择走下面某一条：

- 选 **① 本地豆包** → 走第 3 节
- 选 **② 生图模型 API** → 走第 4 节

顺便确认两件事：

1. **要不要「画她自己」**？要的话得放角色设定图（Q 版 / 成熟版），没有图也能用，只是不如她本人。
2. **要不要读陪伴插件状态**（情绪/精力/天气）？默认读，只读、不改。不想让插件碰就设 `read_state: false`。

**配完必须回报**：走的是哪条路线、后端/模型名、参考图有没有、最后一次真实出图成功没有。

---

## 2. 插件能干什么（配之前先让使用者知道）

1. **一句话出图**：QQ 里说「画一张……」，她自己调 `doubao_paint`，图直接发回当前会话。
2. **图生图（三种时序都支持）**
   - 图 + 需求一起发 → 用这条消息里的图当参考图；
   - 先发图、后说需求 → 记住最近 30 分钟发过的图；
   - 先说需求、后发图 → 先把需求挂起（默认 5 分钟），收到图自动接着画并回发。
3. **画她自己**：内置 Q 版 / 成熟版角色设定图；说「画一张你现在的状态」时用角色图当参考，
   并把**当前时间（白天/夜晚）、真实天气、情绪/精力/身体**放到提示词最前面，强制画面光线对得上。
4. **两种后端**：本地豆包（`doubao`）或任意 OpenAI 兼容生图 API（`openai`）。
5. **自检工具** `doubao_paint_status`：查后端、painter、node、端口、参考图、状态读取。

---

## 3. 路线①：本地豆包（免费）

### 3.1 检查

| 检查 | 位置 / 命令 | 判定 |
| --- | --- | --- |
| node ≥ 22 | `node --version` | 打印 v22+ |
| 豆包已装 | `%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe` 或 `D:\Doubao\app\Doubao.exe` | 文件存在（**app 子目录里的主程序**，不是 launcher） |
| 豆包已登录 | 手动打开豆包，能聊天/生图 | 已登录 |
| painter | `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter\tools\paint.mjs` | 文件存在 |

### 3.2 配置（`data/config/astrbot_plugin_doubao_paint_config.json`）

```json
{
  "image_backend": "doubao",
  "painter_dir": "",
  "node_path": "node",
  "app_exe": "",
  "cdp_port": 9225,
  "timeout_seconds": 240,
  "out_dir": "",
  "keep_days": 7,
  "recent_image_minutes": 30,
  "pending_image_minutes": 5,
  "auto_paint_on_next_image": true,
  "read_state": true,
  "state_plugin": "astrbot_plugin_private_companion",
  "state_read_all_files": false,
  "self_ref_cute": "",
  "self_ref_mature": ""
}
```

- `painter_dir` 留空 = 自动用 `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter`。
- `app_exe` 留空 = painter 自动探测（D 盘 / `%LOCALAPPDATA%` / Program Files），探测不到再填完整路径。

### 3.3 验证

1. 在 QQ 里说「豆包画画能用吗？」→ 她调 `doubao_paint_status`，应能看到 painter 路径、node 版本、
   `豆包 CDP 9225: ❌ 没在跑（第一次画图时会自动拉起来）`（没跑是正常的）。
2. 说「**画一张你现在的状态**」→ 等 20~90 秒（第一次要拉起豆包，会更久），应收到一张图。
3. 说「画一只戴墨镜的橘猫」→ 只带这句话，不带时间/状态（验证「只在她自己时才带状态」）。

---

## 4. 路线②：生图模型 API（OpenAI 兼容）

### 4.1 先问清楚

> 「你的接口是哪个？给我 **base 地址（到 /v1 为止）**、**key**、**模型名**、**尺寸**。」

常见写法：

| 服务 | api_base | image_model 例子 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-image-1` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Kwai-Kolors/Kolors` |
| 各种中转站 | 服务商给的地址 | 服务商给的模型名 |

### 4.2 配置

```json
{
  "image_backend": "openai",
  "api_base": "https://api.siliconflow.cn/v1",
  "api_key": "sk-xxxx",
  "image_model": "Kwai-Kolors/Kolors",
  "image_size": "1024x1024",
  "image_count": 1,
  "use_edits": true,
  "keep_days": 7,
  "recent_image_minutes": 30,
  "pending_image_minutes": 5,
  "auto_paint_on_next_image": true,
  "read_state": true,
  "state_plugin": "astrbot_plugin_private_companion",
  "state_read_all_files": false,
  "self_ref_cute": "",
  "self_ref_mature": ""
}
```

- 纯文字 → `POST {api_base}/images/generations`（JSON）。
- 带参考图 → `POST {api_base}/images/edits`（multipart，字段 `image`）。
  **接口不支持 edits 就把 `use_edits` 设 false**，参考图会被忽略，只按文字画。
- 返回要 OpenAI 那套：`{"data":[{"b64_json":"..."}]}` 或 `{"data":[{"url":"..."}]}`。
- `image_size` 留空就不传，能避开「尺寸参数不认」的坑。

### 4.3 验证

1. 「豆包画画能用吗？」→ 自检里应显示后端 `openai`（不显示豆包端口）。
2. 「画一张蓝色小鲸鱼」→ 能收到图（失败会把接口原话回给使用者）。
3. 「照这张画成水彩」（带图发）→ 看日志/回包是否走 `edits`。

---

## 5. 角色参考图（两条路线都要）

```
<AstrBot>/data/plugin_data/astrbot_plugin_doubao_paint/refs/
├── self_cute.png      ← Q 版（最好三视图）
└── self_mature.jpg    ← 成熟版
```

- 文件名带 `cute` / `q版` / `chibi` → Q 版；带 `mature` / `成熟` → 成熟版。
- 也可以直接在配置里写绝对路径（`self_ref_cute` / `self_ref_mature`）。
- 没有图也能画，只是「画你自己」时没有形象参考。

---

## 6. 时间 / 天气 / 状态（只在「画她自己」时带）

- 只有 `self_portrait=true`（用户说「画你自己 / 你现在的状态」）才把这三样放进提示词最前面。
- 时间写成「2026-11-08 15:34（下午）」，并要求「白天就画白天的光，别画成深夜」。
- 天气从陪伴插件 v2 的 sqlite（`proactive_weather_samples`，qweather 采样）读**真实值**：
  `阴 28℃（湿度 67%）`——**不包含地区**，避免把使用者的定位写进提示词。
- 读不到天气就写「天气未知（不要自己编）」。
- 情绪/精力来自 `daily_state`；快照不是当天的会标「（上次记录 11-08）」，当天才有「睡眠」。

---

## 7. 排查表

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| `画不了：没找到…paint.mjs` | painter 没装 / `painter_dir` 错 | 改路径或装 painter |
| `找不到 node（node）` | PATH 没 node | `node_path` 填完整路径 |
| `画失败了：超时` | 豆包启动慢/没登录 | 调大 `timeout_seconds`，手动登一次豆包 |
| `CDP` 相关错误 | 豆包没带调试端口 / 端口被占 | 关掉豆包让它被拉起，或改 `cdp_port` |
| 接口 401/403 | key 错 / 没权限 | 核对 `api_key`、模型名 |
| 接口 400 参数错 | 尺寸/`n`/`response_format` 不认 | 看返回原话，`image_size` 留空试试 |
| 图右下角有水印 | 豆包免费套餐 | 正常现象；要干净原图需会员账号 |
| 画出来不像她 | 参考图没放/名字没对上 | 按第 5 节命名或配绝对路径 |
| 一直排队 | 有人在画（有并发锁） | 等前一张画完 |
| 普通画图也带了状态 | 走了 `self_portrait` | 确认模型调用时 `self_portrait=false` |

---

## 8. 配完要告诉使用者什么

> 配好了。现在：
> - 你在 QQ 里说「画一张……」，她就会出图并直接发回来；
> - 走的是 **① 本地豆包**（免费，需要豆包开着）/ **② 生图 API（模型 xxx）**；
> - 图 + 需求一起发、先图后话、先话后图都行；
> - 说「画一张你现在的状态」，会带她的角色图 + 当前时间/天气/情绪，画出来光线也对得上；
> - 一次生成大概 20~90 秒（API 通常更快），同时只能画一张；
> - 图存在 `plugin_data/astrbot_plugin_doubao_paint/images/`，7 天自动删。

---

## 9. 边界

- 本地豆包：不调用任何在线生图 API，但**免费套餐的原图带「豆包 AI生成」水印**（会员才干净）。
- API 路线：提示词会发给你选的模型服务商，隐私按那家的条款走。
- **只读**陪伴插件的状态（`companions.json` + v2 sqlite），不改不写；`state_read_all_files` 默认关。
- 参考图只提高一致性，不保证 100% 复刻。
