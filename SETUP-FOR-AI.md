# 给 AI 的施工文档：把「豆包画画」配到能真的发图

> 读者是**接手配置的 AI**。使用者会把这份文档 + `README.md` 给你，并说一句：
>
> 「请帮我配置好 AstrBot 里的【豆包画画】插件，并验证是否可用。」
>
> 照做完，应该达到：**在 QQ 里说「画一张……」，图真的能发回来**；
> 说「画一张你现在的状态」，用的是她自己的形象 + 当前状态。
> 文档里的数值都是实测跑通的：本机一次生成 4 张图耗时约 **72 秒**。

---

## 0. 开工前先问（别自己决定）

1. **画图走的是本机豆包，已登录吗？** 生图消耗的是豆包账号的免费额度，不是钱，但频率高可能被限。
2. **要不要读陪伴插件的状态？**（默认读）——读的是 `astrbot_plugin_private_companion` 的
   `companions.json`（情绪/精力/天气），**只读、不改**。不想让插件碰就设 `read_state: false`。
3. **要不要放角色参考图？** 让使用者的「她」画得像她本人，就必须有角色设定图（Q 版 / 成熟版各一张）。

**配完必须回报**：node 版本、painter 路径、豆包端口状态、参考图有没有、最后一次真实生成成功没有。

---

## 1. 前置（缺一样都画不出来）

| 检查 | 命令 / 位置 | 判定 |
| --- | --- | --- |
| node ≥ 22 | `node --version` | 打印 `v22+` |
| 豆包已安装 | `%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe` 或 `D:\Doubao\app\Doubao.exe` | 文件存在（**是 app 子目录里那个主程序**，不是外层 launcher） |
| 豆包已登录 | 手动打开豆包，能正常聊天/生图 | 已登录 |
| 是否会员 | 豆包首页看套餐 | **免费套餐的图会带「豆包 AI生成」水印**，会员才是干净的原图 |
| painter 插件 | `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter\tools\paint.mjs` | 文件存在 |

> 如果豆包装在别处：拿到主程序完整路径，稍后填进 `app_exe`。
> painter ≥ 0.5.2 会自动探测 `D:\Doubao\app\Doubao.exe` 和 `%LOCALAPPDATA%\Doubao\Application\app\Doubao.exe`；
> 更老的版本只认 `D:\Doubao\app\Doubao.exe`，那就必须填 `app_exe`。

---

## 2. 装插件

```
<AstrBot>/data/plugins/astrbot_plugin_doubao_paint/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
├── SETUP-FOR-AI.md
└── logo.png
```

放好后**完整重启 AstrBot**（不是重载插件）。日志里应有：

```
Loading plugin astrbot_plugin_doubao_paint ...
Added llm tool: doubao_paint
Added llm tool: doubao_paint_status
[DoubaoPaint] 已启动：painter=...\dsh-doubao-painter\tools\paint.mjs｜输出=...｜Timeout=240s｜读状态=True
[DoubaoPaint] 角色参考图：Q版=...｜成熟版=...
```

`painter=` 显示「（没找到）」就是 `painter_dir` 配错了。

---

## 3. 配置

文件：`<AstrBot>/data/config/astrbot_plugin_doubao_paint_config.json`

```json
{
  "painter_dir": "",
  "node_path": "node",
  "app_exe": "",
  "cdp_port": 9225,
  "timeout_seconds": 240,
  "out_dir": "",
  "keep_days": 7,
  "recent_image_minutes": 30,
  "read_state": true,
  "state_plugin": "astrbot_plugin_private_companion",
  "state_read_all_files": false,
  "self_ref_cute": "",
  "self_ref_mature": ""
}
```

- `painter_dir` 留空 = `%USERPROFILE%\.dsh\plugins\dsh-doubao-painter`。
- `app_exe` 留空 = 让 painter 自己探测；探测不到再填完整路径。
- 其余保持默认即可。

---

## 4. 放角色参考图

```
<AstrBot>/data/plugin_data/astrbot_plugin_doubao_paint/refs/
├── self_cute.png      ← Q 版（最好三视图：正面/侧面/背面）
└── self_mature.jpg    ← 成熟版
```

- 文件名带 `cute` / `q版` / `chibi` → 认成 Q 版；带 `mature` / `成熟` → 成熟版。
- 也可以在配置里直接写绝对路径（`self_ref_cute` / `self_ref_mature`）。
- 没有图也能用，只是「画你自己」时没有形象参考。

---

## 5. 验证（照顺序）

### 5.1 自检（30 秒）

在 QQ 里对她说：「豆包画画能用吗？」→ 她调 `doubao_paint_status`，应当回：

```
painter 脚本：...\tools\paint.mjs
node 版本：v22+ / v26
豆包 CDP 9225：❌ 没在跑（第一次画图时会自动拉起来）   ← 没跑也正常
角色图：Q版=...｜成熟版=...
状态读取：...（一段 key=value）
```

- `painter 脚本：❌ 没找到` → 回去改 `painter_dir`。
- `node 不可用` → 填 `node_path` 的完整路径。
- `状态读取：（没读到）` → 陪伴插件没装/没数据，不影响画图。

### 5.2 真实生成（1~2 分钟，必须有）

在 QQ 里说：**「画一张你现在的状态」**

期望：
1. 等 20~90 秒（第一次要拉起豆包，会更久）；
2. QQ 里收到一张图；
3. 日志：

```
[DoubaoPaint] 已启动：...
```
以及没有 `[DoubaoPaint] 生成失败`。

判定三条：**图收到了**、**形象能对上参考图**、**画面里有当前状态/天气的影子**（有角色图 + 状态才成立）。

### 5.3 三条支路（各 1 分钟）

1. **图 + 需求一起发**：发一张照片并说「照这个画成水彩」→ 应当是用这张照片当参考图。
2. **先图后话**：先发一张图，隔 10 秒再说「照这张画成 Q 版」→ 插件用缓存里最近那张。
3. **先话后图**：先说「照这张画成水彩」（不带图）→ 她回「把图发来」；再发图 →
   插件**自动接着画**并直接把图发回来（`pending_image_minutes` 默认 5 分钟内有效）。
4. **纯文字**：「画一只戴墨镜的橘猫」→ 不带参考图，直接文生图。

---

## 6. 排查表

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| `画不了：没找到…paint.mjs` | `painter_dir` 错 / 没装 painter | 改成真实路径 |
| `找不到 node（node）` | PATH 里没有 node | `node_path` 填完整路径（`where node` 查） |
| `画失败了：超时` | 豆包启动慢 / 没登录 / 卡住 | 先把 `timeout_seconds` 提到 300，手动打开豆包登一次 |
| `画失败了：... ECONNREFUSED` / `CDP` 相关 | 豆包没带调试端口、或端口被占 | 关掉豆包让它被自动拉起；或改 `cdp_port` |
| 图发不出去 | 平台侧限制 | 看 AstrBot 日志里的发送错误；图在 `images/` 里能手动拿 |
| 画出来不像她 | 参考图没放 / 名字没对上 | 按第 4 节命名；或直接配绝对路径 |
| 图右下角有水印 | 豆包免费套餐 | 正常现象，本地去不干净；要无水印就换会员账号 |
| 状态是空的 | 陪伴插件没有该字段 | 正常，画图不依赖它 |
| 一直排队 | 有人在画（插件加了锁） | 等前一张画完 |

---

## 7. 配完要告诉使用者什么（照抄改数字）

> 配好了。现在：
> - 在 QQ 里说「画一张……」她就会用本机豆包出图，图直接发回来；
> - 说「画一张你现在的状态」，会带上她的角色设定图 + 当前情绪/天气来画；
> - 图 + 需求一起发能图生图；先发图、过一会儿再说「照这个画」也行（记住 30 分钟）；
> - 一次生成大概 20~90 秒，期间要等；同时只能画一张；
> - 生成的图存在插件数据目录里，7 天后自动删。

---

## 8. 关于水印（必须告诉使用者）

- 插件取的是豆包 CDN 的 **cgen 原图**（不是缩略图），上游 painter 也是这么做的。
- **免费套餐账号的 cgen 原图仍带「豆包 AI生成」水印**（2026-11 实测）：
  其它 CDN 变体全部 403、预览层没有下载按钮、本地去水印会留鬼影。
- **要无水印就得用会员账号**，插件不用改配置。

## 9. 边界

- **只走本机豆包**：不调用任何在线生图 API，不上传提示词到第三方（豆包本身除外）。
- **只读状态**：读陪伴插件的 `companions.json`，不改不写；`state_read_all_files` 默认关。
- **不保证每次都像**：豆包是生成模型，参考图只提高一致性，不是 100% 复刻。
- **豆包账号风险**：高频生图可能触发豆包的限制，这是豆包侧的规则，插件管不了。
