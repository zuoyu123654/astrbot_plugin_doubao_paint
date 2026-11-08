"""豆包画画（astrbot_plugin_doubao_paint）

把本机「豆包」桌面客户端的生图能力接进 AstrBot：
用户在 QQ 里说「画一张……」，模型调用 `doubao_paint`，插件调
dsh-doubao-painter 的命令行入口（lib/tools/paint.mjs）让豆包出图，
拿到本地无水印原图后，直接把图发回当前会话。

三个它专门解决的问题：
  1. **图生图**：用户「图 + 需求」一起发 → 用消息里那张图当参考图；
     分开发（先发图、再说「照这个画」）→ 记住最近发过的图，30 分钟内还用得上。
  2. **她自己的形象**：内置两张角色设定图（Q 版 / 成熟版），
     用户说「画一张你现在的状态」时，用角色图当参考图。
  3. **状态与天气**：读 `astrbot_plugin_private_companion` 的 plugin_data
     （只读、文件名白名单），把此刻的情绪/精力/天气等写进给豆包的提示词。

不依赖 DSH 在线：直接跑 painter 的命令行（node）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_doubao_paint"
PLUGIN_VERSION = "0.3.0"

# 判断「她在说她自己」的关键词：命中就用角色设定图 + 状态
# 这些词说明「要用一张图当参考」，但消息里没图 ⇒ 先记着，等图
NEED_REF_HINTS = (
    "这张", "这幅", "这个", "照这", "按这", "参考", "用这", "图里", "照片", "原图", "它",
    "改这", "换个风格", "水彩风", "漫画风", "赛博朋克风",
)

SELF_HINTS = (
    "你自己", "你现在的", "你的样子", "你长什么样", "画你", "画个你", "画一只你",
    "自画像", "泡泡", "本鲸鱼娘", "你的状态", "你此刻",
)
MATURE_HINTS = ("成熟", "长大", "成年", "御姐", "大只", "第二张", "第二个形象")

# 读陪伴插件状态时只认这些 key（大小写不敏感的子串）
STATE_HINTS = (
    "mood", "emotion", "energy", "sleep", "weather", "city", "location",
    "temperature", "relationship", "intimacy", "body", "health", "target",
    "goal", "diary", "dream", "心情", "情绪", "精力", "睡眠", "天气", "城市",
    "温度", "关系", "体温", "目标", "日记", "梦",
)
STATE_KEY_DESCEND = (
    "daily_state", "state", "emotion", "mood", "weather", "environment",
    "relationship", "sleep", "energy", "body", "location", "goal", "target",
    "diary", "dream", "profile", "status",
)
NOISE_HINTS = ("id", "uid", "hash", "path", "file", "version", "updated_at", "created_at")


@register(PLUGIN_NAME, "paopao", "豆包画画：让她用本机豆包出图，并把图发回 QQ", PLUGIN_VERSION)
class DoubaoPaintPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        cfg = config if isinstance(config, dict) else {}

        self.painter_dir = str(cfg.get("painter_dir", "") or "").strip()
        self.node_path = str(cfg.get("node_path", "") or "").strip() or "node"
        self.app_exe = str(cfg.get("app_exe", "") or "").strip()
        try:
            self.cdp_port = int(cfg.get("cdp_port", 9225) or 9225)
        except (TypeError, ValueError):
            self.cdp_port = 9225
        try:
            self.timeout_sec = max(30, int(cfg.get("timeout_seconds", 240) or 240))
        except (TypeError, ValueError):
            self.timeout_sec = 240
        try:
            self.keep_days = max(1, int(cfg.get("keep_days", 7) or 7))
        except (TypeError, ValueError):
            self.keep_days = 7
        try:
            self.recent_minutes = max(1, int(cfg.get("recent_image_minutes", 30) or 30))
        except (TypeError, ValueError):
            self.recent_minutes = 30
        # 「先发需求、后发图」：等到下一张图的窗口期
        try:
            self.pending_minutes = max(1, int(cfg.get("pending_image_minutes", 5) or 5))
        except (TypeError, ValueError):
            self.pending_minutes = 5
        self.auto_paint_next_image = bool(cfg.get("auto_paint_on_next_image", True))

        # 生图后端：doubao（本机豆包）或 openai（任意 OpenAI 兼容的 /images/generations）
        self.backend = str(cfg.get("image_backend", "doubao") or "doubao").strip().lower()
        if self.backend not in ("doubao", "openai"):
            self.backend = "doubao"
        self.api_base = str(cfg.get("api_base", "https://api.openai.com/v1") or "").strip().rstrip("/")
        self.api_key = str(cfg.get("api_key", "") or "").strip()
        self.image_model = str(cfg.get("image_model", "gpt-image-1") or "gpt-image-1").strip()
        self.image_size = str(cfg.get("image_size", "1024x1024") or "").strip()
        try:
            self.image_count = max(1, min(4, int(cfg.get("image_count", 1) or 1)))
        except (TypeError, ValueError):
            self.image_count = 1
        self.use_edits = bool(cfg.get("use_edits", True))

        self.read_state = bool(cfg.get("read_state", True))
        self.state_plugin = str(cfg.get("state_plugin", "astrbot_plugin_private_companion") or "").strip()
        self.state_read_all_files = bool(cfg.get("state_read_all_files", False))

        self.ref_cute_cfg = str(cfg.get("self_ref_cute", "") or "").strip()
        self.ref_mature_cfg = str(cfg.get("self_ref_mature", "") or "").strip()

        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001
            logger.error("[DoubaoPaint] 拿不到插件数据目录（%s），临时文件放系统临时目录", exc)
            self.data_dir = Path(os.environ.get("TEMP", ".")) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ref_dir = self.data_dir / "refs"
        self.ref_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = Path(str(cfg.get("out_dir", "") or "").strip() or (self.data_dir / "images"))
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self._lock = asyncio.Lock()
        # session → [(path, ts)]，用来接「先发图、后说需求」
        self._recent_images: dict[str, list[tuple[str, float]]] = {}
        # session → 等着配图的「先发的需求」
        self._pending: dict[str, dict[str, Any]] = {}
        self._cleaner: asyncio.Task | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def initialize(self) -> None:
        self._cleaner = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "[DoubaoPaint] 已启动：painter=%s｜输出=%s｜Timeout=%ss｜读状态=%s",
            self._painter_script() or "（没找到）",
            self.image_dir,
            self.timeout_sec,
            self.read_state,
        )
        logger.info("[DoubaoPaint] 角色参考图（按文件名挑）：%s", self._ref_list_text())

    async def terminate(self) -> None:
        if self._cleaner and not self._cleaner.done():
            self._cleaner.cancel()

    # ── 路径探测 ────────────────────────────────────────────────────────
    def _painter_script(self) -> Path | None:
        base = Path(
            self.painter_dir
            or (Path(os.environ.get("USERPROFILE", "~")) / ".dsh" / "plugins" / "dsh-doubao-painter")
        )
        for rel in ("tools/paint.mjs", "lib/tools/paint.mjs"):
            p = base / rel
            if p.is_file():
                return p
        return None

    def _ref_files(self) -> list[Path]:
        exts = (".png", ".jpg", ".jpeg", ".webp")
        return [p for p in sorted(self.ref_dir.glob("*")) if p.suffix.lower() in exts]

    def _ref_default(self) -> str:
        files = self._ref_files()
        for p in files:
            if "默认" in p.stem or "default" in p.stem.lower():
                return str(p)
        return str(files[0]) if files else ""

    def _ref_list_text(self) -> str:
        files = self._ref_files()
        if not files:
            return "（refs 目录是空的，没有参考图）"
        return "、".join(p.stem for p in files)

    def _self_appearance_text(self, has_ref: bool) -> str:
        """给画/不给参考图时，用来锁住她形象的说明。"""

        base = (
            "蓝色渐变长发（发梢浅蓝）、头顶一根呆毛、耳侧是薄片状鲸鳍、蓝色圆眼、"
            "深蓝白色女仆长裙配白围裙（围裙上有小鲸鱼）、身后一条大鲸尾，Q 版圆润"
        )
        if has_ref:
            return f"参考图是她的固定角色设定（三视图），务必保持形象一致：{base}。"
        return (
            "这次没有参考图，请严格按她的固定形象画（不要画成别人）："
            f"{base}，身高约 1.45 米，抱起来软乎乎有分量。"
        )

    def _pick_ref(self, keyword: str, prompt: str) -> str:
        """按文件名挑参考图：先看显式指定的词，再看用户话里提到的名字，最后用「默认」。

        文件名里的括号说明也算名字的一部分（例如「默认（Q版可爱 女仆装）」）。
        """

        files = self._ref_files()
        if not files:
            return ""
        kw = str(keyword or "").strip()
        if kw:
            for p in files:
                if kw in p.stem or kw.lower() in p.stem.lower():
                    return str(p)
        for p in files:
            if p.stem and p.stem in (prompt or ""):
                return str(p)
        return self._ref_default()

    # ── 记住最近的图（图与需求分开发的情况） ────────────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-210)
    async def _remember_image(self, event: AstrMessageEvent) -> None:
        try:
            for comp in getattr(event.message_obj, "message", []) or []:
                if type(comp).__name__ != "Image":
                    continue
                path = ""
                for attr in ("path", "file"):
                    v = str(getattr(comp, attr, "") or "")
                    if v.startswith("file://"):
                        v = v[8:]
                    if v and Path(v).is_file():
                        path = v
                        break
                if not path:
                    try:
                        path = await comp.convert_to_file_path()
                    except Exception:  # noqa: BLE001
                        path = ""
                if path and Path(path).is_file():
                    key = str(event.unified_msg_origin or "")
                    bucket = self._recent_images.setdefault(key, [])
                    bucket.append((path, time.time()))
                    self._recent_images[key] = bucket[-5:]
                    # 之前是不是有人「先发需求、等图」？有就直接开画
                    self._fire_pending(key, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DoubaoPaint] 记图片失败：%s", exc)

    def _fire_pending(self, key: str, image_path: str) -> None:
        task = self._pending.pop(key, None)
        if not task or not self.auto_paint_next_image:
            return
        if time.time() - float(task.get("ts") or 0) > self.pending_minutes * 60:
            return
        logger.info("[DoubaoPaint] 收到后补的图，接着画：%s", str(task.get("prompt"))[:40])
        asyncio.create_task(self._paint_and_send(task, image_path))

    async def _paint_and_send(self, task: dict[str, Any], image_path: str) -> None:
        """后台把「先发的需求 + 后发的图」画完，再发回同一个会话。"""

        prompt = str(task.get("prompt") or "")
        umo = str(task.get("umo") or "")
        refs = [image_path]
        self_ref = ""
        if task.get("self_portrait"):
            self_ref = self._pick_ref(
                str(task.get("reference") or ("成熟" if task.get("mature") else "")),
                prompt,
            )
            if self_ref:
                refs.insert(0, self_ref)
        if task.get("self_portrait"):
            head = [self._constraint_block()]
            state = self._state_block()
            if state:
                head.append("【她此刻的状态】" + state + "。把这些画进表情、动作、场景里。")
            body = "\n\n".join(head) + "\n\n【画面】" + prompt
        else:
            body = prompt
        if self_ref:
            body += "\n\n" + self._self_appearance_text(True)
        async with self._lock:
            ok, err, files = await self._generate(body, refs)
        if not ok or not files:
            logger.warning("[DoubaoPaint] 补图后生成失败：%s", err)
            try:
                await self.context.send_message(
                    umo, MessageChain([Plain(f"图收到了，但画失败了：{err}")])
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await self.context.send_message(
                umo, MessageChain([Image.fromFileSystem(files[0])])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DoubaoPaint] 补图后发图失败：%s", exc)

    def _recent_image(self, event: AstrMessageEvent) -> str:
        key = str(event.unified_msg_origin or "")
        cutoff = time.time() - self.recent_minutes * 60
        for path, ts in reversed(self._recent_images.get(key, [])):
            if ts >= cutoff and Path(path).is_file():
                return path
        return ""

    def _image_in_event(self, event: AstrMessageEvent) -> str:
        for comp in getattr(event.message_obj, "message", []) or []:
            if type(comp).__name__ != "Image":
                continue
            for attr in ("path", "file"):
                v = str(getattr(comp, attr, "") or "")
                if v.startswith("file://"):
                    v = v[8:]
                if v and Path(v).is_file():
                    return v
        return ""

    # ── 读「她现在的状态」 ──────────────────────────────────────────────
    def _state_dirs(self) -> list[Path]:
        if not self.state_plugin:
            return []
        data_root = self.data_dir.parent  # <AstrBot>/data/plugin_data
        d = data_root / self.state_plugin
        return [d] if d.is_dir() else []

    def _flatten_state(self, obj: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if depth > 3:
            return out
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                lk = str(k).lower()
                if isinstance(v, (str, int, float, bool)):
                    s = str(v).strip()
                    if not s or len(s) > 80:
                        continue
                    if any(h in lk for h in NOISE_HINTS):
                        continue
                    if any(h in lk for h in STATE_HINTS):
                        out.append((key, s))
                    elif isinstance(v, bool) and any(h in key.lower() for h in STATE_KEY_DESCEND):
                        out.append((key, s))
                elif isinstance(v, (dict, list)):
                    if any(h in lk for h in STATE_KEY_DESCEND) or depth == 0:
                        out.extend(self._flatten_state(v, key, depth + 1))
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:3]):
                out.extend(self._flatten_state(v, f"{prefix}[{i}]", depth + 1))
        return out

    def _load_companions(self) -> dict | None:
        for d in self._state_dirs():
            fp = d / "companions.json"
            if fp.is_file():
                try:
                    data = json.loads(fp.read_text(encoding="utf-8-sig"))
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(data, dict):
                    return data
        return None

    def _v2_weather(self) -> str:
        """从陪伴插件 v2 的 sqlite 里读最近一次真实天气（qweather 采样的那份）。"""

        now = time.time()
        cached = getattr(self, "_weather_cache", None)
        if cached and now - cached[0] < 60:
            return cached[1]

        text = ""
        root = self.data_dir.parent / (self.state_plugin or "")
        if root.is_dir():
            for db in list(root.rglob("companion-v2.sqlite3"))[:2]:
                tmpdir = None
                try:
                    import sqlite3
                    import tempfile

                    tmpdir = Path(tempfile.mkdtemp(prefix="dp_v2_"))
                    tmp = tmpdir / "db.sqlite3"
                    shutil.copy2(db, tmp)
                    for sfx in ("-wal", "-shm"):
                        side = Path(str(db) + sfx)
                        if side.is_file():
                            shutil.copy2(side, Path(str(tmp) + sfx))
                    con = sqlite3.connect(str(tmp))
                    try:
                        row = con.execute(
                            "select sample_json from proactive_weather_samples "
                            "order by rowid desc limit 1"
                        ).fetchone()
                    finally:
                        con.close()
                    if not row or not row[0]:
                        continue
                    data = json.loads(row[0])
                    cond = str(data.get("condition") or "").strip()
                    temp = data.get("temperature_c")
                    hum = data.get("humidity_pct")
                    # 地区不往外带：只给天气本身，免得把用户的定位写进提示词
                    if not cond and temp is None:
                        continue
                    body = ""
                    if cond:
                        body = cond
                    if isinstance(temp, (int, float)):
                        body += (" " if body else "") + f"{temp:g}℃"
                    if isinstance(hum, (int, float)):
                        body += f"（湿度 {hum:g}%）"
                    text = body
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[DoubaoPaint] 读 v2 天气失败：%s", exc)
                finally:
                    if tmpdir is not None:
                        shutil.rmtree(tmpdir, ignore_errors=True)

        self._weather_cache = (now, text)
        return text

    def _weather_text(self, ds: dict, dw: dict) -> str:
        """真实天气优先：v2 sqlite 的采样 → 旧的 daily_weather；占位一律当没有。"""

        text = self._v2_weather()
        if text:
            return text
        for raw in (dw.get("prompt"), ds.get("weather")):
            text = str(raw or "").strip()
            if not text:
                continue
            low = text.lower()
            if "暂无" in text or "unknown" in low or low in ("none", "null"):
                continue
            return text
        return ""

    @staticmethod
    def _daypart(hour: int) -> str:
        if 5 <= hour < 9:
            return "清晨"
        if 9 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 17:
            return "下午"
        if 17 <= hour < 19:
            return "傍晚"
        if 19 <= hour < 23:
            return "晚上"
        return "深夜"

    def _now_block(self) -> str:
        now = datetime.now()
        return f"{now.strftime('%Y-%m-%d %H:%M')}（{self._daypart(now.hour)}）"

    def _situation_block(self) -> str:
        now = datetime.now()
        weather = ""
        data = self._load_companions() or {}
        ds = data.get("daily_state") if isinstance(data.get("daily_state"), dict) else {}
        dw = data.get("daily_weather") if isinstance(data.get("daily_weather"), dict) else {}
        weather = self._weather_text(ds, dw)
        line = f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（{self._daypart(now.hour)}）"
        line += f"；天气：{weather}" if weather else "；天气未知（不要自己编天气）"
        return line

    def _constraint_block(self) -> str:
        """放在提示词最前面的硬性前提：时间/天气必须先对上。"""

        now = datetime.now()
        if 5 <= now.hour < 19:
            light = (
                "现在是白天，画面必须有白天的自然光（窗外/天空是亮的），"
                "绝对不能画成夜晚、深夜或者黑漆漆的室内。"
            )
        else:
            light = "现在是夜里，画面可以是夜色或室内灯光，但别画成大中午。"
        return (
            "【硬性前提·必须准确】" + self._situation_block() + "。"
            + light
            + "天气写了晴/雨/雪/阴，天空和地面要跟着对上；没写就别自己加雨雪。"
        )

    def _state_block(self) -> str:
        """把她此刻的状态翻成人话（不要给豆包看 key=value）。"""

        if not self.read_state:
            return ""
        data = self._load_companions()
        if not data:
            return ""
        ds = data.get("daily_state") if isinstance(data.get("daily_state"), dict) else {}
        dw = data.get("daily_weather") if isinstance(data.get("daily_weather"), dict) else {}

        bits: list[str] = []
        mood = str(ds.get("mood_bias") or "").strip()
        moods: list[str] = []
        conds = ds.get("conditions")
        if isinstance(conds, list):
            for c in conds:
                if isinstance(c, dict):
                    m = str(c.get("mood") or "").strip()
                    if m and m not in moods:
                        moods.append(m)
        names = [x for x in dict.fromkeys([mood] + moods) if x]
        if names:
            bits.append("心情：" + "、".join(names))
        energy = ds.get("energy")
        if isinstance(energy, (int, float)):
            bits.append(f"精力 {int(energy)}/100")
        # 状态快照可能是前几天的：过期的「睡眠」和当下时间会打架，就不带了
        state_date = str(ds.get("date") or "").strip()
        fresh = state_date == datetime.now().strftime("%Y-%m-%d")
        sleep = str(ds.get("sleep") or "").strip()
        if sleep and fresh:
            bits.append("睡眠：" + sleep)
        body = "，".join(
            x
            for x in (
                str(ds.get("health") or "").strip(),
                str(ds.get("body_cycle") or "").strip(),
            )
            if x
        )
        if body:
            bits.append("身体：" + body)
        weather = self._weather_text(ds, dw)
        if weather:
            bits.append("天气：" + weather)
        dream = str(ds.get("dream") or "").strip().rstrip("。.,，;；")
        if dream:
            bits.append("最近的梦：" + (dream[:50] + "…" if len(dream) > 50 else dream))
        text = "；".join(bits)
        if bits and state_date and not fresh:
            text = f"（上次记录 {state_date}）" + text
        return text

    # ── 主工具：画图 ────────────────────────────────────────────────────
    @filter.llm_tool(name="doubao_paint")
    async def doubao_paint(
        self,
        event: AstrMessageEvent,
        prompt: str,
        self_portrait: bool = False,
        with_state: bool = False,
        reference: str = "",
        mature: bool = False,
    ) -> str:
        """用本机「豆包」生成图片，并把图直接发回当前 QQ 会话。

        什么时候用：
        - 用户说「画一张…」「生成一张…」「来张图」「照这个画」；
        - 用户让你「画一张你现在的状态 / 画你自己」→ self_portrait=true 且 with_state=true。
          画她自己之前，可以先调 `doubao_list_refs` 看有哪些角色参考图，挑名字最合适的填 reference。

        Args:
            prompt(string): 画面描述（要有主体、风格、构图、光线；中文写）
            self_portrait(bool): 是不是画「她自己」。true 时用内置角色设定图当参考图，保持形象一致
            with_state(bool): 是否把此刻的状态/天气写进提示词（配 self_portrait 用得最多）
            reference(string): 用哪张参考图，填文件名里的关键词（例如「成熟」「女仆装」「默认」；
                括号里的说明也算，比如「默认（Q版可爱 女仆装）」可以填「女仆装」）。
                留空 = 自动挑：先看话里有没有提到文件名，没有就用「默认」那张。
        """

        target = self._painter_script()
        if target is None:
            return "画不了：没找到 dsh-doubao-painter 的 tools/paint.mjs（检查 painter_dir 配置）"

        body = (prompt or "").strip()
        if not body:
            return "没给画面描述"

        # 1) 参考图：用户消息里的图 > 最近发过的图 > 她的角色图
        refs: list[str] = []
        use_self = bool(self_portrait) or any(h in body for h in SELF_HINTS)
        img_now = self._image_in_event(event)
        if img_now:
            # 这条消息里就带图：优先用它当参考图
            refs.append(img_now)
        elif not use_self:
            # 分开发的情况：用最近发过的那张（画她自己时不掺用户的图）
            cached = self._recent_image(event)
            if cached:
                refs.append(cached)
        if use_self:
            ref = self._pick_ref(reference or ("成熟" if mature else ""), body)
            if ref:
                refs.insert(0, ref)
                body += "\n\n" + self._self_appearance_text(bool(ref))
        refs = [r for r in dict.fromkeys(refs) if Path(r).is_file()]

        # 先发需求、后发图：需求里明显要「参考一张图」，但这会儿还没有图
        if not refs and not use_self and self.auto_paint_next_image and any(
            h in body for h in NEED_REF_HINTS
        ):
            umo = str(event.unified_msg_origin or "")
            self._pending[umo] = {
                "prompt": body,
                "umo": umo,
                "self_portrait": bool(self_portrait),
                "with_state": bool(with_state),
                "reference": str(reference or ""),
                "mature": bool(mature),
                "ts": time.time(),
            }
            logger.info("[DoubaoPaint] 先记下需求，等用户发图：%s", body[:40])
            return f"好，把图发来，我照它画（{self.pending_minutes} 分钟内有效）"

        # 2) 只有「画她自己 / 她现在的状态」才加时间+天气+状态；普通画图不掺这些
        if use_self:
            head = [self._constraint_block()]
            state = self._state_block()
            if state:
                head.append("【她此刻的状态】" + state + "。把这些画进表情、动作、场景里。")
            body = "\n\n".join(head) + "\n\n【画面】" + body

        # 3) 调 painter（一次只能跑一个，加锁）
        async with self._lock:
            ok, err, files = await self._generate(body, refs)
        if not ok or not files:
            logger.warning("[DoubaoPaint] 生成失败：%s", err)
            return f"画失败了：{err or '没有拿到图片'}"

        out = files[0]
        if not Path(out).is_file():
            return "画完了但没找到文件，看看豆包是不是把图存在别处了"

        try:
            await event.send(MessageChain([Image.fromFileSystem(out)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DoubaoPaint] 发图失败：%s", exc)
            return f"画好了，但发图失败：{exc}"

        return "画好了，图已经发给你了"

    async def _generate(self, prompt: str, refs: list[str]) -> tuple[bool, str, list[str]]:
        """按配置选后端：本机豆包，或任意 OpenAI 兼容的生图接口。"""

        if self.backend == "openai":
            return await self._run_openai(prompt, refs)
        return await self._run_painter(prompt, refs)

    async def _run_openai(self, prompt: str, refs: list[str]) -> tuple[bool, str, list[str]]:
        if not self.api_base:
            return False, "没配 api_base（image_backend=openai）", []
        if not self.api_key:
            return False, "没配 api_key（image_backend=openai）", []
        try:
            return await asyncio.to_thread(self._openai_sync, prompt, refs)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:300], []

    def _openai_sync(self, prompt: str, refs: list[str]) -> tuple[bool, str, list[str]]:
        import base64
        import mimetypes
        import urllib.request
        import uuid as _uuid

        refs = [r for r in refs if Path(r).is_file()][:1]
        timeout = self.timeout_sec
        if refs and self.use_edits:
            url = self.api_base + "/images/edits"
            boundary = "----dp" + _uuid.uuid4().hex
            parts: list[bytes] = []

            def field(name: str, value: str) -> None:
                parts.append(
                    (
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                        f"{value}\r\n"
                    ).encode()
                )

            field("model", self.image_model)
            field("prompt", prompt)
            if self.image_size:
                field("size", self.image_size)
            field("n", str(self.image_count))
            ctype = mimetypes.guess_type(refs[0])[0] or "application/octet-stream"
            parts.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                    f"filename=\"ref{Path(refs[0]).suffix}\"\r\nContent-Type: {ctype}\r\n\r\n"
                ).encode()
            )
            parts.append(Path(refs[0]).read_bytes())
            parts.append(f"\r\n--{boundary}--\r\n".encode())
            data = b"".join(parts)
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }
        else:
            url = self.api_base + "/images/generations"
            payload = {
                "model": self.image_model,
                "prompt": prompt,
                "n": self.image_count,
                "response_format": "b64_json",
            }
            if self.image_size:
                payload["size"] = self.image_size
            data = json.dumps(payload).encode()
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            }

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "read", lambda: b"")()
            detail = body.decode("utf-8", "ignore")[:200] if isinstance(body, bytes) else ""
            return False, f"{exc} {detail}".strip(), []

        try:
            payload = json.loads(raw)
        except Exception:
            return False, "接口返回的不是 JSON：" + raw[:200], []
        items = payload.get("data") or []
        files: list[str] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            out = self.image_dir / f"api-{int(time.time())}-{i + 1}.png"
            if item.get("b64_json"):
                out.write_bytes(base64.b64decode(item["b64_json"]))
                files.append(str(out))
            elif item.get("url"):
                with urllib.request.urlopen(item["url"], timeout=timeout) as r:
                    out.write_bytes(r.read())
                files.append(str(out))
        if not files:
            err = payload.get("error") or payload
            return False, "接口没返回图片：" + str(err)[:200], []
        return True, "", files

    async def _run_painter(self, prompt: str, refs: list[str]) -> tuple[bool, str, list[str]]:
        script = self._painter_script()
        if script is None:
            return False, "没找到 paint.mjs", []
        payload: dict[str, Any] = {
            "prompt": prompt,
            "outDir": str(self.image_dir),
            "timeoutMs": self.timeout_sec * 1000,
            "cdpPort": self.cdp_port,
            "newChat": True,
        }
        if refs:
            payload["refImages"] = refs
        if self.app_exe:
            payload["appExe"] = self.app_exe
        cmd = [self.node_path, str(script), "--stdin-json", "--json"]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=self.timeout_sec + 30,
            )
        except asyncio.TimeoutError:
            return False, f"超时（>{self.timeout_sec}s）", []
        except FileNotFoundError:
            return False, f"找不到 node（{self.node_path}）", []
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), []

        text = (out_b or b"").decode("utf-8", "ignore").strip()
        err = (err_b or b"").decode("utf-8", "ignore").strip()
        # paint.mjs 的 --json 是「美化过」的多行 JSON，不能按行解析
        data = None
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except Exception:  # noqa: BLE001
                    data = None
        if not data:
            return False, (err or text)[-300:], []
        if not data.get("ok"):
            return False, str(data.get("error") or err)[-300:], []
        files = [str(f) for f in (data.get("files") or []) if f]
        if not files and data.get("items"):
            files = [f for it in data["items"] for f in (it.get("files") or [])]
        return True, "", files

    @filter.llm_tool(name="doubao_list_refs")
    async def doubao_list_refs(self, event: AstrMessageEvent) -> str:
        """列出可用的角色参考图。画她自己之前先看一眼，挑名字最合适的那张。

        文件名就是说明（括号里的也算），例如「默认（Q版可爱 女仆装）」「成熟（长大）」。
        """

        files = self._ref_files()
        if not files:
            return "refs 目录是空的：没有参考图，画她自己时只能用文字描述她的样子。"
        default = self._ref_default()
        default_name = Path(default).stem if default else ""
        lines = ["可用参考图（文件名就是说明，括号里的也算）："]
        for p in files:
            tag = "（默认）" if p.stem == default_name else ""
            lines.append(f"- {p.stem}{tag}")
        if default_name:
            lines.append(f"没有更合适的，就用默认：{default_name}")
        return "\n".join(lines)

    # ── 自检工具 ────────────────────────────────────────────────────────
    @filter.llm_tool(name="doubao_paint_status")
    async def doubao_paint_status(self, event: AstrMessageEvent) -> str:
        """检查豆包画画能不能用：painter 脚本、node、豆包是否在调试端口上。"""

        script = self._painter_script()
        lines = [
            f"painter 脚本：{script or '❌ 没找到'}",
            f"node：{self.node_path}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                self.node_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            lines.append("node 版本：" + (out_b or b"").decode("utf-8", "ignore").strip())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"node 不可用：{exc}")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.cdp_port), timeout=3
            )
            writer.close()
            lines.append(f"豆包 CDP {self.cdp_port}：✅ 在跑")
        except Exception:  # noqa: BLE001
            lines.append(f"豆包 CDP {self.cdp_port}：❌ 没在跑（第一次画图时会自动拉起来）")

        lines.append("角色图（按文件名挑）：" + self._ref_list_text())
        state = self._state_block()
        lines.append("状态读取：" + (state[:120] if state else "（没读到，或 read_state 关了）"))
        return "\n".join(lines)

    # ── 清理旧图 ────────────────────────────────────────────────────────
    async def _cleanup_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                cutoff = time.time() - self.keep_days * 86400
                for p in self.image_dir.glob("*"):
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[DoubaoPaint] 清理失败：%s", exc)
            await asyncio.sleep(3600)
