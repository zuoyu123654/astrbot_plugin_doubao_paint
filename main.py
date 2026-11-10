"""豆包画画（astrbot_plugin_doubao_paint）

把本机「豆包」桌面客户端的生图能力接进 AstrBot：
用户在 QQ 里说「画一张……」，模型调用 `doubao_paint`，插件调
插件自己用 CDP（调试端口）驱动豆包出图，不再调用 dsh-doubao-painter，
拿到本地无水印原图后，直接把图发回当前会话。

三个它专门解决的问题：
  1. **图生图**：用户「图 + 需求」一起发 → 用消息里那张图当参考图；
     分开发（先发图、再说「照这个画」）→ 记住最近发过的图，30 分钟内还用得上。
  2. **她自己的形象**：内置两张角色设定图（Q 版 / 成熟版），
     用户说「画一张你现在的状态」时，用角色图当参考图。
  3. **状态与天气**：读 `astrbot_plugin_private_companion` 的 plugin_data
     （只读、文件名白名单），把此刻的情绪/精力/天气等写进给豆包的提示词。

不依赖 DSH：插件内置 CDP 客户端，直接和豆包桌面端说话。
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

from .doubao_cdp import DoubaoClient, find_doubao_exe
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_doubao_paint"
PLUGIN_VERSION = "0.7.1"

# 判断「她在说她自己」的关键词：命中就用角色设定图 + 状态
# 这些词说明「要用一张图当参考」，但消息里没图 ⇒ 先记着，等图
# 只有用户「明确在指一张图」时才认为缺参考图；不要拿模型写的画面描述来匹配
NEED_REF_HINTS = (
    "这张图", "这幅图", "这张照片", "这几张", "照这张", "照这幅", "照这个", "照着这张",
    "按这张", "按这幅", "按这个画", "参考这张", "参考这幅", "参考图", "用这张", "用这幅",
    "图里的", "照片里的", "原图", "改这张", "把这张", "拿这张",
)

# 「画/看 你……的样子」这类也要当成画她自己（正则比关键词准）
SELF_PATTERNS = (
    r"你的?[^\s，。,.!?！？]{0,8}(样子|模样|画像|自拍|半身像|全身像)",
    r"(?:画|来|想看|想看看|看看|要)(?:一张|一个|个)?你(?![的])",
    r"你长什么样|你现在的|你平时|你读书|你看书",
)

SELF_HINTS = (
    "你自己", "你现在的", "你的样子", "你的模样", "你长什么样", "画个你", "画一只你",
    "自画像", "泡泡", "本鲸鱼娘", "你的状态", "你此刻", "画一张你", "画一个你",
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

        # 已经不需要 dsh-doubao-painter 了：插件自己用 CDP 驱动豆包。
        # painter_dir / node_path 只为兼容老配置保留（不再使用）。
        self.painter_dir = str(cfg.get("painter_dir", "") or "").strip()
        self.node_path = str(cfg.get("node_path", "") or "").strip() or "node"
        self.app_exe = str(cfg.get("app_exe", "") or "").strip() or find_doubao_exe()
        self.auto_launch = bool(cfg.get("auto_launch_doubao", True))
        # 豆包出一张要 1~2 分钟，而 AstrBot 的工具调用有 120 秒上限。
        # 所以默认「先回一句在画了，画完再发图」，避免工具被超时掐掉。
        self.async_paint = bool(cfg.get("async_paint", True))
        self._paint_tasks: dict[str, asyncio.Task] = {}
        self.restart_when_no_cdp = bool(cfg.get("restart_when_no_cdp", True))
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

        # 谁能用：owner（只有主人，默认）/ whitelist（主人 + 白名单）/ everyone
        self.access_mode = str(cfg.get("access_mode", "owner") or "owner").strip().lower()
        if self.access_mode not in ("owner", "whitelist", "everyone"):
            self.access_mode = "owner"
        raw_users = cfg.get("allowed_users") or []
        if isinstance(raw_users, str):
            raw_users = raw_users.replace("，", ",").split(",")
        self.allowed_users = {
            str(u).strip() for u in raw_users if str(u).strip()
        } if isinstance(raw_users, (list, tuple, set)) else set()

        # 她可以主动画：自己想画就画一张发过去，带冷却
        self.allow_self_initiated = bool(cfg.get("allow_self_initiated", True))
        try:
            self.self_initiated_cooldown = max(
                0, int(cfg.get("self_initiated_cooldown_minutes", 30) or 0)
            )
        except (TypeError, ValueError):
            self.self_initiated_cooldown = 30
        self._last_self_draw = 0.0

        self.read_state = bool(cfg.get("read_state", True))
        self.state_plugin = str(cfg.get("state_plugin", "astrbot_plugin_private_companion") or "").strip()
        self.state_read_all_files = bool(cfg.get("state_read_all_files", False))

        # 没有参考图时用来锁形象的文字；留空 = 用内置的「蓝色大肥鱼」描述
        self.self_appearance = str(cfg.get("self_appearance", "") or "").strip()
        # 拿用户的图当参考图之前，先让多模态模型描述一下（没有多模态 provider 就自动跳过）
        self.read_image = bool(cfg.get("read_image", True))
        self.caption_provider = str(cfg.get("caption_provider", "") or "").strip()
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
            "[DoubaoPaint] 已启动：豆包=%s｜输出=%s｜Timeout=%ss｜读状态=%s",
            self.app_exe or "（没找到豆包）",
            self.image_dir,
            self.timeout_sec,
            self.read_state,
        )
        logger.info("[DoubaoPaint] 角色参考图（按文件名挑）：%s", self._ref_list_text())

    async def terminate(self) -> None:
        if self._cleaner and not self._cleaner.done():
            self._cleaner.cancel()

    # ── 权限 ────────────────────────────────────────────────────────────
    def _owner_ids(self) -> set[str]:
        """主人名单：AstrBot 的 cmd_config.json 里的 admins_id（缓存 60 秒）。"""

        now = time.time()
        cached = getattr(self, "_owner_cache", None)
        if cached and now - cached[0] < 60:
            return cached[1]
        ids: set[str] = set()
        try:
            cfg_path = self.data_dir.parent.parent / "cmd_config.json"
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            value = data.get("admins_id")
            if isinstance(value, list):
                ids = {str(x).strip() for x in value if str(x).strip()}
        except Exception:  # noqa: BLE001
            pass
        self._owner_cache = (now, ids)
        return ids

    def _can_use(self, event: AstrMessageEvent) -> bool:
        if self.access_mode == "everyone":
            return True
        try:
            uid = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            uid = ""
        if uid:
            if uid in self._owner_ids():
                return True
            if self.access_mode == "whitelist" and uid in self.allowed_users:
                return True
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    DENY_TEXT = "这个只给老大用，你别乱点。"

    # ── 路径探测 ────────────────────────────────────────────────────────
    def _doubao_exe(self) -> str:
        """豆包主程序路径；配置里填了就优先用配置。"""

        if self.app_exe and Path(self.app_exe).is_file():
            return self.app_exe
        return find_doubao_exe()

    def _doubao_ready(self) -> tuple[bool, str]:
        exe = self._doubao_exe()
        if exe and Path(exe).is_file():
            return True, exe
        return False, f"找不到豆包主程序：{exe}（在插件配置里填 app_exe）"

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

        base = getattr(self, "self_appearance", "") or (
            "蓝色渐变长发（发梢浅蓝）、头顶一根呆毛、耳侧是薄片状鲸鳍、蓝色圆眼、"
            "深蓝白色女仆长裙配白围裙（围裙上有小鲸鱼）、身后一条大鲸尾，Q 版圆润"
        )
        if has_ref:
            return f"参考图是她的固定角色设定（三视图），务必保持形象一致：{base}。"
        return (
            "这次没有参考图，请严格按她的固定形象画（不要画成别人）："
            f"{base}，身高约 1.45 米，抱起来软乎乎有分量。"
        )

    @staticmethod
    def _looks_like_self(text: str) -> bool:
        """用户是不是在说「画她自己」（画你看书的样子 / 想看你的样子 …）。"""

        text = str(text or "")
        if not text:
            return False
        if any(h in text for h in SELF_HINTS):
            return True
        for pattern in SELF_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

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

    async def _paint_then_send(
        self, umo: str, body: str, refs: list[str]
    ) -> None:
        """后台画完再把图发回会话（不受工具 120 秒超时影响）。"""

        started = time.time()
        try:
            async with self._lock:
                ok, err, files = await self._generate(body, refs)
        except Exception as exc:  # noqa: BLE001
            ok, err, files = False, str(exc), []
        elapsed = time.time() - started

        if not ok or not files:
            logger.warning("[DoubaoPaint] 后台画图失败（%.1fs）：%s", elapsed, err)
            try:
                await self.context.send_message(
                    umo,
                    MessageChain([Plain(f"画失败了：{str(err or '没拿到图片')[:120]}")]),
                )
            except Exception:  # noqa: BLE001
                pass
            return

        logger.info("[DoubaoPaint] 后台画图完成（%.1fs）：%s", elapsed, Path(files[0]).name)
        try:
            await self.context.send_message(
                umo, MessageChain([Image.fromFileSystem(files[0])])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DoubaoPaint] 后台发图失败：%s", exc)

    async def _paint_and_send(self, task: dict[str, Any], image_path: str) -> None:
        """后台把「先发的需求 + 后发的图」画完，再发回同一个会话。"""

        prompt = str(task.get("prompt") or "")
        umo = str(task.get("umo") or "")
        refs = [image_path]
        self_ref = ""
        if not task.get("self_portrait") and image_path:
            desc = await self._describe_image(image_path)
            if desc:
                prompt = prompt + f"\n\n【参考图里是】{desc}"

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
        if hour < 5:
            return "凌晨"
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

    # 不同时段该有什么光——不要一律「白天就画白天的自然光」
    LIGHT_BY_BAND = {
        "凌晨": "天还没亮，是夜色/路灯，室内只有微弱的光",
        "清晨": "天刚亮，光线柔和偏冷，影子很长",
        "上午": "明亮但还柔和的日光，影子偏长",
        "中午": "正午顶光，光线强、影子短",
        "下午": "午后偏斜的阳光，光开始变暖",
        "傍晚": "太阳偏西，光线明显变弱、偏暖（橘/粉/灰），影子拉长；天还没黑透",
        "晚上": "天已经黑了，靠室内灯/路灯照明",
        "深夜": "很晚了，安静、光线很弱，只有室内弱光",
    }

    def _v2_role_plan(self) -> str:
        """读陪伴插件 v2 的「角色计划·当前计划时段」（calendar_records / kind=role_plan）。"""

        now_ts = time.time()
        cached = getattr(self, "_plan_cache", None)
        if cached and now_ts - cached[0] < 60:
            return cached[1]

        text = ""
        root = self.data_dir.parent / (self.state_plugin or "")
        if root.is_dir():
            for db in list(root.rglob("companion-v2.sqlite3"))[:2]:
                tmpdir = None
                try:
                    import sqlite3
                    import tempfile

                    tmpdir = Path(tempfile.mkdtemp(prefix="dp_plan_"))
                    tmp = tmpdir / "db.sqlite3"
                    shutil.copy2(db, tmp)
                    for sfx in ("-wal", "-shm"):
                        side = Path(str(db) + sfx)
                        if side.is_file():
                            shutil.copy2(side, Path(str(tmp) + sfx))
                    con = sqlite3.connect(str(tmp))
                    try:
                        rows = con.execute(
                            "select record_json from calendar_records "
                            "where record_json like '%role_plan%'"
                        ).fetchall()
                    finally:
                        con.close()

                    now = datetime.now().astimezone()
                    best: tuple[datetime, datetime, dict] | None = None
                    for (raw,) in rows:
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        sched = rec.get("schedule") or {}
                        anchor_at = sched.get("anchor")
                        if not anchor_at:
                            continue
                        try:
                            start = datetime.fromisoformat(
                                str(anchor_at).replace("Z", "+00:00")
                            ).astimezone()
                        except Exception:
                            continue
                        end = start + timedelta(minutes=int(rec.get("duration_minutes") or 45))
                        if start <= now < end and (best is None or start > best[0]):
                            best = (start, end, rec)

                    if best:
                        start, end, rec = best
                        title = str(rec.get("title") or "").strip()
                        narrative = ""
                        details = rec.get("details")
                        if isinstance(details, dict):
                            narrative = " ".join(
                                str(v).strip() for v in details.values() if str(v).strip()
                            )
                        narrative = narrative or str(rec.get("detail") or "").strip()
                        text = f"{start:%H:%M}–{end:%H:%M} {title}"
                        if narrative:
                            text += "：" + narrative
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[DoubaoPaint] 读 v2 计划失败：%s", exc)
                finally:
                    if tmpdir is not None:
                        shutil.rmtree(tmpdir, ignore_errors=True)

        self._plan_cache = (now_ts, text)
        return text

    def _now_playing_text(self) -> str:
        """她「现在在做什么」：读陪伴插件能读到的活动 + 当下的时段/心情。

        面板上的「今日上演」是插件运行时拼出来的，不一定落盘；
        这里把能读到的线索都给出来，让模型自己写画面提示词。
        """

        data = self._load_companions() or {}
        bits: list[str] = []

        now = datetime.now()
        bits.append(f"现在是{now.strftime('%Y-%m-%d %H:%M')}（{self._daypart(now.hour)}）")

        plan_text = self._v2_role_plan()
        if plan_text:
            bits.append("今日上演：" + plan_text)

        # 「今日上演」：daily_plan.schedule 里覆盖当前时间的那个活动
        try:
            plan = data.get("daily_plan") if isinstance(data.get("daily_plan"), dict) else {}
            plan_date = str(plan.get("date") or "")
            raw = plan.get("raw")
            payload = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            sched = payload.get("schedule") or []
            if plan_date == now.strftime("%Y-%m-%d") and isinstance(sched, list):
                cur = now.hour * 60 + now.minute
                for item in sched:
                    if not isinstance(item, dict):
                        continue
                    t0 = str(item.get("time") or "")
                    t1 = str(item.get("end") or "")
                    try:
                        h0, m0 = [int(x) for x in t0.split(":")[:2]]
                        h1, m1 = [int(x) for x in t1.split(":")[:2]]
                    except Exception:
                        continue
                    if h0 * 60 + m0 <= cur < h1 * 60 + m1:
                        act = str(item.get("activity") or "").strip()
                        mood_s = str(item.get("mood") or "").strip()
                        if act:
                            bits.append("今日上演：" + act + (f"（{mood_s}）" if mood_s else ""))
                        break
        except Exception:  # noqa: BLE001
            pass

        ds = data.get("daily_state") if isinstance(data.get("daily_state"), dict) else {}
        mood = str(ds.get("mood_bias") or "").strip()
        energy = ds.get("energy")
        if mood:
            bits.append(f"心情偏{mood}")
        if isinstance(energy, (int, float)):
            bits.append(f"精力 {int(energy)}/100")

        try:
            acts = data.get("observed_activities") or []
            recent: list[str] = []
            if isinstance(acts, list):
                for a in acts[-8:]:
                    if not isinstance(a, dict):
                        continue
                    end_at = str(a.get("end_at") or a.get("start_at") or "")
                    title = str(a.get("title") or "").strip()
                    summary = str(a.get("summary") or "").strip()
                    if not end_at or not (title or summary):
                        continue
                    stamp = datetime.fromisoformat(end_at)
                    if (datetime.now(stamp.tzinfo) - stamp).total_seconds() > 36 * 3600:
                        continue
                    recent.append(title or summary)
            if recent:
                bits.append("最近在做：" + "；".join(dict.fromkeys(recent))[:120])
        except Exception:  # noqa: BLE001
            pass

        # 没有陪伴插件（或读不到状态）⇒ 明确告诉她用「时间 + 聊天内容」自己编一个合理画面
        if len(bits) <= 1:
            bits.append(
                "（读不到她的状态和日程：没有装/没启用陪伴插件。"
                "按当前时间和你俩刚才聊的内容，自己想一个合理的画面来画）"
            )
        return "；".join(bits)

    def _constraint_block(self, now: datetime | None = None) -> str:
        """放在提示词最前面的硬性前提：给时间 + 天气，具体怎么画交给豆包。"""

        now = now or datetime.now()
        band = self._daypart(now.hour)
        data = self._load_companions() or {}
        ds = data.get("daily_state") if isinstance(data.get("daily_state"), dict) else {}
        dw = data.get("daily_weather") if isinstance(data.get("daily_weather"), dict) else {}
        weather = self._weather_text(ds, dw)

        head = f"【现在的时间与天气】{now.strftime('%Y-%m-%d %H:%M')}（{band}）"
        head += f"；天气：{weather}。" if weather else "；天气未知（别自己编天气）。"
        body = (
            "画面的光线、天空、地面和氛围都要跟这个时间和天气对得上，具体怎么画你自己发挥；"
            "但别画成别的时段（比如傍晚别画成正午大太阳、白天别画成深夜），也别凭空加雨雪。"
        )
        return head + body

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
        proactive: bool = False,
    ) -> str:
        """用本机「豆包」生成图片，并把图直接发回当前 QQ 会话。

        什么时候用：
        - 用户说「画一张…」「生成一张…」「来张图」「照这个画」；
        - **你也可以自己想画就画**：聊天里聊到好吃的、好看的、你今天干了什么、
          或者你就是想给他看看你现在的样子，都可以直接画一张发过去，不用等他开口。
          这种情况把 proactive 设成 true（会有冷却，别刷屏）。
        - 用户让你「画一张你现在的状态 / 画你自己」→ self_portrait=true 且 with_state=true。
          画她自己之前：先调 `doubao_now_playing` 看她现在在做什么，**你来写一段具体的画面描述**放进 prompt；
          再调 `doubao_list_refs` 看有哪些角色参考图，挑名字最合适的填 reference。

        Args:
            prompt(string): **用你自己的话**写的画面描述（主体、风格、构图、光线，中文）。
                不要原样转发用户的话：先理解，再写成你心里那个画面——
                比如用户说「画一只猫」，就写你心目中的猫长什么样（颜色/胖瘦/表情）、在干什么、什么光；
                用户说「画你现在的状态」，就写你此刻在做什么、什么表情。
                可以把这理解成**你在画画**：你描述的这段画面，最后会真的画出来给用户看。
            self_portrait(bool): 是不是画「她自己」。true 时用内置角色设定图当参考图，保持形象一致。
                用户说「画一个你看书的样子」「想看你看书」「画你的样子」这类，都算画她自己 → true。
            with_state(bool): 是否把此刻的状态/天气写进提示词（配 self_portrait 用得最多）
            proactive(bool): 这次是「你自己想画」的（不是用户要求的）→ true；用户要求的留 false
            reference(string): 用哪张参考图，填文件名里的关键词（例如「成熟」「女仆装」「默认」；
                括号里的说明也算，比如「默认（Q版可爱 女仆装）」可以填「女仆装」）。
                留空 = 自动挑：先看话里有没有提到文件名，没有就用「默认」那张。
        """

        # 只有走本地豆包才需要豆包程序；API 后端不需要
        if self.backend != "openai":
            ok, detail = self._doubao_ready()
            if not ok:
                return "画不了：" + detail

        body = (prompt or "").strip()
        if not body:
            return "没给画面描述"

        # 1) 参考图：用户消息里的图 > 最近发过的图 > 她的角色图
        refs: list[str] = []
        if not self._can_use(event):
            return self.DENY_TEXT

        if proactive:
            if not self.allow_self_initiated:
                return "（这次就不画了，老大没让我自己画）"
            gap = time.time() - self._last_self_draw
            if self.self_initiated_cooldown and gap < self.self_initiated_cooldown * 60:
                left = int((self.self_initiated_cooldown * 60 - gap) / 60) + 1
                return f"刚画过一张了，等 {left} 分钟再画，别刷屏。"
            self._last_self_draw = time.time()

        user_text = str(getattr(event, "message_str", "") or "")
        use_self = bool(self_portrait) or self._looks_like_self(user_text or body)
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

        # 用用户的图当参考图时，先读一下图里是什么（她才知道自己照着什么画）
        image_note = ""
        desc_text = ""
        if refs and not use_self:
            desc = await self._describe_image(refs[0])
            if desc:
                desc_text = desc
                image_note = f"\n\n【参考图里是】{desc}"

        # 先发需求、后发图：只有「用户自己说」要照某张图、且确实没图时，才挂起等图
        if not refs and not use_self and self.auto_paint_next_image and any(
            h in user_text for h in NEED_REF_HINTS
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
        if image_note:
            body += image_note

        # 3) 异步模式：豆包太慢（1~2 分钟），先回一句，画完再发图
        if self.async_paint and self.backend != "openai":
            umo = str(event.unified_msg_origin or "")
            running = self._paint_tasks.get(umo)
            if running is not None and not running.done():
                return "上一张还在画，等它出来再画下一张。"
            task = asyncio.create_task(self._paint_then_send(umo, body, refs))
            self._paint_tasks[umo] = task

            def _cleanup(_t: asyncio.Task, key: str = umo) -> None:
                if self._paint_tasks.get(key) is _t:
                    self._paint_tasks.pop(key, None)

            task.add_done_callback(_cleanup)
            logger.info("[DoubaoPaint] 已排上后台画图：%s", body[:40])
            return "好，在画了，画完直接把图发给你（大概一两分钟）。"

        # 3') 同步模式（API 后端 / 或关掉 async_paint）
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

        # 图不在本条消息里（先发图、后说需求）⇒ 把看图结果交给她，她可以顺口转述；
        # 图和需求同一条 / 先需求后补图：只回「画好了」，不再额外回复图的内容
        if desc_text and not img_now:
            return f"画好了，图已经发给你了（你说的是这张图吧：{desc_text}）"
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
        """用自带的 CDP 客户端驱动豆包出图（不再调用 dsh-doubao-painter）。"""

        ok, detail = self._doubao_ready()
        if not ok:
            return False, detail, []

        client = DoubaoClient(
            port=self.cdp_port,
            app_exe=detail,
            timeout=self.timeout_sec,
            out_dir=self.image_dir,
            auto_launch=self.auto_launch,
            restart_when_no_cdp=self.restart_when_no_cdp,
        )
        try:
            result = await client.generate(
                prompt,
                ref_images=[str(r) for r in refs] if refs else None,
                new_chat=True,
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DoubaoPaint] 豆包出图失败：%s", exc)
            return False, str(exc)[:300], []
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        files = [str(f) for f in (result.get("files") or []) if Path(str(f)).is_file()]
        if not files:
            return False, "豆包没返回可用的图片", []
        logger.info(
            "[DoubaoPaint] 出图成功：%s 张｜%s",
            len(files),
            "原图" if result.get("resolution") == "original" else "缩略图",
        )
        return True, "", files

    @filter.llm_tool(name="doubao_now_playing")
    async def doubao_now_playing(self, event: AstrMessageEvent) -> str:
        """查她「现在在做什么」：当下的时间/时段、心情、精力、最近在做的活动。

        用户说「画一张你现在的状态」时，先调这个，然后**由你根据这些线索写一段具体的画面描述**，
        再调 doubao_paint(self_portrait=true, prompt=你写的那段)。
        """
        if not self._can_use(event):
            return self.DENY_TEXT


        return self._now_playing_text() or "（没读到她的活动，按当前时间和心情自己写一段吧）"

    @filter.llm_tool(name="doubao_list_refs")
    async def doubao_list_refs(self, event: AstrMessageEvent) -> str:
        """列出可用的角色参考图。画她自己之前先看一眼，挑名字最合适的那张。

        文件名就是说明（括号里的也算），例如「默认（Q版可爱 女仆装）」「成熟（长大）」。
        """
        if not self._can_use(event):
            return self.DENY_TEXT


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
        """检查豆包画画能不能用：豆包程序、调试端口、参考图、状态读取。"""
        if not self._can_use(event):
            return self.DENY_TEXT

        exe_ok, exe = self._doubao_ready()
        lines = [
            f"豆包程序：{exe if exe_ok else '❌ ' + exe}",
            f"驱动方式：自带 CDP（不依赖 dsh-doubao-painter）",
            f"自动拉起豆包：{'开' if self.auto_launch else '关'}",
        ]

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
