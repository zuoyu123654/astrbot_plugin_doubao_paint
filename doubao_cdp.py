"""直接驱动本机豆包桌面端（CDP），不依赖 dsh-doubao-painter。

思路和那个 Node 插件一样：给豆包开调试端口，用 CDP 在页面里输入提示词、点发送、
等生成图出现，再点开图片把 CDN 上的 **cgen 原图** URL 读出来下载。

为避免再踩那个插件的坑，这里做了几件事：
- 连不上 CDP 时先看豆包是不是普通模式开着，是就关掉重开（带调试端口）；
- 抓图前先等历史图渲染稳定，避免把上一张图当成本次结果；
- 点开预览用 CDP 的「真实鼠标事件」（合成 click 打不开预览，只能拿带水印缩略图）；
- 原图没拿到时会再试一次，仍失败才退回缩略图，并在日志里说明；
- 下载后校验文件大小/尺寸，太小（384px 缩略图）会重试一次；
- 出错时把页面尾部文案带回来（比如「额度不足」）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

CHAT_URL_MARK = "doubao-chat/chat"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def find_doubao_exe() -> str:
    """找豆包主程序（和 painter 一样的候选顺序）。"""

    env = os.environ.get("DOUBAO_APP_EXE", "").strip()
    if env and Path(env).is_file():
        return env
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"D:\Doubao\app\Doubao.exe",
        str(Path(local) / "Doubao" / "Application" / "app" / "Doubao.exe"),
        str(Path(local) / "Doubao" / "Application" / "Doubao.exe"),
        r"C:\Program Files\Doubao\app\Doubao.exe",
        r"C:\Program Files (x86)\Doubao\app\Doubao.exe",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return candidates[0]


def image_key(url: str) -> str:
    import re

    m = re.search(r"rc_gen_image/([^~?]+)", str(url or ""))
    return m.group(1) if m else str(url or "")[:200]


def pick_best_urls(urls: list[str]) -> list[str]:
    gen = [u for u in dict.fromkeys(urls) if isinstance(u, str) and "rc_gen_image" in u]
    if not gen:
        return []
    cgen = [u for u in gen if "cgen_" in u]
    if cgen:
        return cgen
    non_thumb = [u for u in gen if "cthumb" not in u]
    return non_thumb or gen


def _is_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Doubao.exe", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=0x08000000,
        ).stdout.decode("gbk", "ignore")
        return "doubao.exe" in out.lower()
    except Exception:  # noqa: BLE001
        return False


def _kill_doubao() -> bool:
    if os.name != "nt":
        return False
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Doubao.exe", "/T"],
            capture_output=True,
            timeout=25,
            creationflags=0x08000000,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


class CdpConnection:
    """极简 CDP 客户端（aiohttp WebSocket）。"""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:  # noqa: BLE001
                    continue
                rid = data.get("id")
                if rid is None:
                    continue
                fut = self._pending.pop(rid, None)
                if fut is None or fut.done():
                    continue
                if "error" in data:
                    err = data.get("error") or {}
                    fut.set_exception(
                        RuntimeError(f"CDP 错误：{err.get('message') or err}")
                    )
                else:
                    fut.set_result(data.get("result") or {})
        except Exception as exc:  # noqa: BLE001
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self._pending.clear()

    async def send(
        self, method: str, params: dict | None = None, timeout: float = 20.0
    ) -> dict:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.ws.send_str(
                json.dumps({"id": rid, "method": method, "params": params or {}})
            )
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout,
        )
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            raise RuntimeError(
                "页面脚本异常：" + str(detail.get("text") or detail)[:300]
            )
        return (res.get("result") or {}).get("value")

    async def close(self) -> None:
        if self._reader and not self._reader.done():
            self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class DoubaoClient:
    """一次连接 → 一次生成。"""

    def __init__(
        self,
        *,
        port: int = 9225,
        app_exe: str = "",
        timeout: int = 180,
        out_dir: Path,
        auto_launch: bool = True,
        restart_when_no_cdp: bool = True,
    ) -> None:
        self.port = int(port)
        self.app_exe = app_exe or find_doubao_exe()
        self.timeout = max(30, int(timeout))
        self.out_dir = Path(out_dir)
        self.auto_launch = auto_launch
        self.restart_when_no_cdp = restart_when_no_cdp
        self._http: aiohttp.ClientSession | None = None
        self.conn: CdpConnection | None = None

    # ── HTTP/CDP 基础设施 ───────────────────────────────────────────
    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._http

    async def _cdp_alive(self, timeout: float = 1.5) -> bool:
        try:
            sess = await self._session()
            async with sess.get(
                f"http://127.0.0.1:{self.port}/json/version",
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    async def _list_targets(self) -> list[dict]:
        sess = await self._session()
        async with sess.get(f"http://127.0.0.1:{self.port}/json/list") as resp:
            return await resp.json()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def ensure_ready(self, launch_timeout: float = 60.0) -> None:
        if await self._cdp_alive():
            return
        if not self.auto_launch:
            raise RuntimeError(
                f"豆包没有以调试模式运行（127.0.0.1:{self.port} 无响应）。"
                f'请先运行："{self.app_exe}" --remote-debugging-port={self.port}'
            )
        if _is_process_running():
            if not self.restart_when_no_cdp:
                raise RuntimeError(
                    "豆包正在运行，但没有开调试端口。请先完全退出豆包再重试。"
                )
            logger.info("[DoubaoCDP] 豆包是普通模式开着，关掉重开（带调试端口）")
            _kill_doubao()
            deadline = time.time() + 25
            while time.time() < deadline and _is_process_running():
                await asyncio.sleep(0.4)
            await asyncio.sleep(0.8)

        if not Path(self.app_exe).is_file():
            raise RuntimeError(f"找不到豆包主程序：{self.app_exe}")
        logger.info("[DoubaoCDP] 启动豆包：%s --remote-debugging-port=%s", self.app_exe, self.port)
        subprocess.Popen(  # noqa: S603
            [self.app_exe, f"--remote-debugging-port={self.port}"],
            close_fds=True,
            creationflags=0x00000008 if os.name == "nt" else 0,  # DETACHED_PROCESS
        )
        deadline = time.time() + launch_timeout
        while time.time() < deadline:
            if await self._cdp_alive():
                return
            await asyncio.sleep(0.7)
        raise RuntimeError(
            f"启动豆包后 {launch_timeout:.0f}s 内调试端口仍未就绪（{self.port}）。"
            "确认豆包能正常启动并已登录。"
        )

    async def connect(self) -> CdpConnection:
        await self.ensure_ready()
        targets = await self._list_targets()
        chat = next(
            (t for t in targets if CHAT_URL_MARK in str(t.get("url") or "")), None
        )
        if chat is None:
            raise RuntimeError(
                "连上了调试端口，但没找到豆包聊天页。请确认豆包已登录并停在主界面。"
            )
        sess = await self._session()
        ws = await sess.ws_connect(chat["webSocketDebuggerUrl"], heartbeat=30)
        self.conn = CdpConnection(ws)
        await self.conn.start()
        for method in ("Runtime.enable", "Page.enable"):
            try:
                await self.conn.send(method, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        return self.conn

    # ── 页面操作 ───────────────────────────────────────────────────
    async def wait_composer(self, timeout: float = 9.0) -> bool:
        assert self.conn
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = await self.conn.evaluate(
                    "(() => { const el = document.querySelector('div.tiptap.ProseMirror, [contenteditable=\"true\"]');"
                    " return !!(el && el.getBoundingClientRect().width > 0) })()"
                )
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                return True
            await asyncio.sleep(0.3)
        return False

    async def new_chat(self) -> bool:
        assert self.conn
        try:
            before = await self.conn.evaluate("location.href")
        except Exception:  # noqa: BLE001
            before = ""
        for _ in range(2):
            try:
                clicked = await self.conn.evaluate(
                    "(() => { const cands = [...document.querySelectorAll('div,a,button')]"
                    ".filter((el) => { const r = el.getBoundingClientRect();"
                    " if (!(r.width > 0 && r.height > 0)) return false;"
                    " const t = (el.innerText || '').trim();"
                    " return t === '新对话' || t.startsWith('新对话 ') || t.startsWith('新对话\\n') });"
                    " if (!cands.length) return { ok: false };"
                    " cands[cands.length - 1].click(); return { ok: true } })()"
                )
            except Exception:  # noqa: BLE001
                clicked = None
            if not (clicked or {}).get("ok"):
                for evt in ("keyDown", "keyUp"):
                    try:
                        await self.conn.send(
                            "Input.dispatchKeyEvent",
                            {
                                "type": evt,
                                "key": "K",
                                "code": "KeyK",
                                "windowsVirtualKeyCode": 75,
                                "modifiers": 10,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
            for _ in range(25):
                await asyncio.sleep(0.4)
                try:
                    href = await self.conn.evaluate("location.href")
                except Exception:  # noqa: BLE001
                    href = before
                if href != before:
                    return await self.wait_composer()
        return await self.wait_composer()

    async def type_text(self, text: str) -> None:
        assert self.conn
        focused = await self.conn.evaluate(
            "(() => { const el = document.querySelector('div.tiptap.ProseMirror, [contenteditable=\"true\"]');"
            " if (!el) return { ok: false, reason: 'no-input' };"
            " el.focus();"
            " try { const sel = window.getSelection(); const r = document.createRange();"
            " r.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(r);"
            " document.execCommand('delete', false, null) } catch (e) {}"
            " const sel2 = window.getSelection(); const r2 = document.createRange();"
            " r2.selectNodeContents(el); r2.collapse(false); sel2.removeAllRanges(); sel2.addRange(r2);"
            " return { ok: true } })()"
        )
        if not (focused or {}).get("ok"):
            raise RuntimeError("找不到豆包输入框（页面结构可能变了，或没停在聊天页）")
        await self.conn.send("Input.insertText", {"text": text})
        await asyncio.sleep(0.5)

    async def submit(self) -> None:
        assert self.conn
        for evt in ("keyDown", "keyUp"):
            await self.conn.send(
                "Input.dispatchKeyEvent",
                {
                    "type": evt,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            )

    async def collect_urls(self) -> list[str]:
        assert self.conn
        try:
            out = await self.conn.evaluate(
                "(() => { const out = [];"
                " for (const el of document.querySelectorAll('img')) {"
                " const u = el.currentSrc || el.src || '';"
                " if (u && u.includes('rc_gen_image')) out.push(u) } return out })()"
            )
            return [str(u) for u in (out or [])]
        except Exception:  # noqa: BLE001
            return []

    async def wait_images_stable(
        self, quiet: float = 1.2, max_wait: float = 8.0, poll: float = 0.4
    ) -> int:
        deadline = time.time() + max_wait
        prev = -1
        stable_since = time.time()
        while time.time() < deadline:
            n = len(await self.collect_urls())
            if n != prev:
                prev = n
                stable_since = time.time()
            elif time.time() - stable_since >= quiet:
                return n
            await asyncio.sleep(poll)
        return max(0, prev)

    async def page_tail(self, size: int = 600) -> str:
        assert self.conn
        try:
            text = await self.conn.evaluate(
                f"(document.body?.innerText || '').slice(-{int(size)})"
            )
            return str(text or "")
        except Exception:  # noqa: BLE001
            return ""

    async def wait_new_images(
        self, before_keys: list[str], timeout: float, poll: float = 1.5
    ) -> dict[str, Any]:
        before = set(before_keys)
        deadline = time.time() + timeout
        last: list[str] = []
        tail = ""
        while time.time() < deadline:
            await asyncio.sleep(poll)
            urls = await self.collect_urls()
            fresh = [u for u in urls if image_key(u) not in before]
            last = urls
            tail = await self.page_tail()
            if fresh:
                await asyncio.sleep(0.9 if ("图片已生成" in tail or "生成完成" in tail) else 1.2)
                urls2 = await self.collect_urls() or urls
                fresh2 = [u for u in urls2 if image_key(u) not in before]
                if fresh2:
                    return {
                        "urls": pick_best_urls(fresh2),
                        "keys": list(dict.fromkeys(image_key(u) for u in fresh2)),
                        "all": urls2,
                        "page_text": tail,
                    }
                continue
            if any(k in tail for k in ("生成失败", "无法生成", "请稍后再试", "额度不足", "次数已用完")):
                return {
                    "urls": [],
                    "keys": [],
                    "all": last,
                    "page_text": tail,
                    "error": tail[-200:],
                }
        fresh = [u for u in last if image_key(u) not in before]
        return {
            "urls": pick_best_urls(fresh),
            "keys": list(dict.fromkeys(image_key(u) for u in fresh)),
            "all": last,
            "page_text": tail,
            "timeout": True,
        }

    async def open_preview(self, key: str) -> bool:
        assert self.conn
        rect = await self.conn.evaluate(
            "(() => { const key = " + json.dumps(str(key)) + ";"
            " const imgs = [...document.querySelectorAll('img')].filter((i) => {"
            " const u = i.currentSrc || i.src || '';"
            " return u.includes('rc_gen_image') && u.includes(key) });"
            " if (!imgs.length) return null;"
            " const el = imgs[imgs.length - 1];"
            " try { el.scrollIntoView({ block: 'center' }) } catch (e) {}"
            " const r = el.getBoundingClientRect();"
            " return { x: r.left + r.width / 2, y: r.top + r.height / 2 } })()"
        )
        if not rect:
            return False
        x, y = rect["x"], rect["y"]
        await self.conn.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
        )
        await asyncio.sleep(0.08)
        await self.conn.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await asyncio.sleep(0.06)
        await self.conn.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        return True

    async def close_preview(self) -> None:
        assert self.conn
        for evt in ("keyDown", "keyUp"):
            try:
                await self.conn.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": evt,
                        "key": "Escape",
                        "code": "Escape",
                        "windowsVirtualKeyCode": 27,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.5)
        try:
            await self.conn.evaluate(
                "(() => { const btn = [...document.querySelectorAll('button,[role=\"button\"]')]"
                ".find((b) => { const t = (b.getAttribute('aria-label') || b.innerText || '').trim();"
                " return b.offsetParent && /^(关闭|close)$/i.test(t) });"
                " if (btn) { btn.click(); return 'clicked' } return 'none' })()"
            )
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)

    async def ensure_originals(self, keys: list[str]) -> list[str]:
        if not keys:
            return []
        current = await self.collect_urls()
        have = [u for u in current if "cgen_" in u]
        have_key_set = {image_key(u) for u in have}
        missing = [k for k in keys if k not in have_key_set]
        for key in missing:
            try:
                if not await self.open_preview(key):
                    continue
                await asyncio.sleep(2.0)
                for url in await self.collect_urls():
                    if "cgen_" in url and url not in have:
                        have.append(url)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[DoubaoCDP] 打开预览失败（%s）：%s", key, exc)
            finally:
                await self.close_preview()
        return have

    async def attach_images(self, paths: list[str]) -> None:
        assert self.conn
        files = [str(p) for p in paths if Path(p).is_file()]
        if not files:
            return
        doc = await self.conn.send("DOM.getDocument", {"depth": -1}, timeout=20)
        root = (doc.get("root") or {}).get("nodeId")
        nodes = await self.conn.send(
            "DOM.querySelectorAll",
            {"nodeId": root, "selector": 'input[type="file"]'},
            timeout=20,
        )
        node_ids = nodes.get("nodeIds") or []
        if not node_ids:
            raise RuntimeError("没找到豆包的文件上传入口（页面结构可能变了）")
        await self.conn.send(
            "DOM.setFileInputFiles", {"nodeId": node_ids[0], "files": files}, timeout=30
        )
        await asyncio.sleep(2.5)

    # ── 下载 ───────────────────────────────────────────────────────
    async def download(self, url: str, target: Path) -> Path:
        sess = await self._session()
        async with sess.get(
            url,
            headers={"User-Agent": UA, "Referer": "https://www.doubao.com/"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"下载图片失败 HTTP {resp.status}")
            blob = await resp.read()
            ctype = (resp.headers.get("content-type") or "").lower()
        ext = ".webp" if "webp" in ctype else ".jpg" if ("jpeg" in ctype or "jpg" in ctype) else ".png" if "png" in ctype else target.suffix
        real = target.with_suffix(ext or ".png")
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(blob)
        return real

    # ── 主流程 ─────────────────────────────────────────────────────
    async def generate(
        self,
        prompt: str,
        *,
        ref_images: list[str] | None = None,
        new_chat: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise RuntimeError("提示词为空")
        prompt = self._ensure_draw_intent(prompt)
        timeout = float(timeout or self.timeout)

        if self.conn is None:
            await self.connect()
        await self.wait_composer(15)

        if new_chat:
            try:
                await self.new_chat()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DoubaoCDP] 开新对话失败（继续用当前对话）：%s", exc)

        if ref_images:
            await self.attach_images(list(ref_images))

        await self.wait_images_stable()
        before = [image_key(u) for u in await self.collect_urls()]

        await self.type_text(prompt)
        await self.submit()

        baseline = set(before)
        await asyncio.sleep(1.5)
        for u in await self.collect_urls():
            baseline.add(image_key(u))

        result = await self.wait_new_images(list(baseline), timeout)
        if not result["urls"]:
            reason = result.get("error") or ("等待超时" if result.get("timeout") else "没有返回图片")
            raise RuntimeError(
                f"豆包没出图：{reason}\n页面尾部：{str(result.get('page_text') or '')[-200:]}"
            )

        urls = result["urls"]
        resolution = "thumb"
        try:
            originals = await self.ensure_originals(result["keys"])
            key_set = set(result["keys"])
            matched = [u for u in originals if image_key(u) in key_set]
            if matched:
                urls, resolution = matched, "original"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DoubaoCDP] 拿原图失败，先用缩略图：%s", exc)

        if resolution == "thumb":
            try:
                await asyncio.sleep(1.2)
                originals = await self.ensure_originals(result["keys"])
                key_set = set(result["keys"])
                matched = [u for u in originals if image_key(u) in key_set]
                if matched:
                    urls, resolution = matched, "original"
                    logger.info("[DoubaoCDP] 第二次尝试拿到原图")
            except Exception:  # noqa: BLE001
                pass

        stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
        files: list[str] = []
        for idx, url in enumerate(urls, 1):
            target = self.out_dir / f"doubao-{stamp}-{idx}.png"
            try:
                real = await self.download(url, target)
                files.append(str(real))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DoubaoCDP] 第 %s 张下载失败：%s", idx, exc)
                if idx == 1 and len(urls) == 1:
                    raise
        if not files:
            raise RuntimeError("图片 URL 拿到了，但下载全部失败")

        # 缩略图（384px）校验：太小就再试一次原图
        if resolution == "thumb":
            if await self._too_small(files[0]):
                logger.info("[DoubaoCDP] 只拿到缩略图，再试一次原图")
                try:
                    originals = await self.ensure_originals(result["keys"])
                    key_set = set(result["keys"])
                    matched = [u for u in originals if image_key(u) in key_set]
                    if matched:
                        files = []
                        for idx, url in enumerate(matched, 1):
                            target = self.out_dir / f"doubao-{stamp}-r{idx}.png"
                            files.append(str(await self.download(url, target)))
                        resolution = "original"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[DoubaoCDP] 重试原图失败：%s", exc)

        return {
            "files": files,
            "urls": urls,
            "resolution": resolution,
            "page_text": result.get("page_text") or "",
        }

    @staticmethod
    def _ensure_draw_intent(prompt: str) -> str:
        """豆包是「看意图」决定生图的：光写「一只猫趴在窗台」它会当成聊天。

        提示词里没有明确的「画 / 生成 / 插画」这类字眼时，补一个前缀把它掰到生图技能上。
        实测加了前缀就会一次出 4 张 1728×2304 的原图。
        """

        text = str(prompt or "").strip()
        marks = (
            "画", "绘制", "插画", "生成图", "生成一张", "生成图片", "出一张", "来一张",
            "图片", "图像", "海报", "壁纸", "封面",
        )
        if any(m in text for m in marks):
            return text
        return "画一张图：" + text

    @staticmethod
    async def _too_small(path: str) -> bool:
        try:
            from PIL import Image

            def _check() -> int:
                with Image.open(path) as im:
                    return max(im.size)

            side = await asyncio.to_thread(_check)
            return side <= 512
        except Exception:  # noqa: BLE001
            return False
