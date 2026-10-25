import asyncio
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_feiyu_browse"
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 机器相关的路径和密钥都不写进代码：走插件配置（_conf_schema.json）或 plugin_data/config.json。
DEFAULT_CONFIG = {
    # bsk.exe（browser-skill CLI）绝对路径；留空则直接用 PATH 里的 bsk
    "bsk_path": "",
    # 浏览器实例 id；留空自动取第一个在线实例
    "browser": "",
    "timeout": 120,
    "max_chars": 3000,
    # 联网搜索走的 DuckDuckGo 兼容接口（key 留空则不启用 web_search 工具）
    "search_key": "",
    "search_api": "https://api.deepseek.com/v1/tools/search",
}

URL_RE = re.compile(r"https?://[^\s，。；、）)】\]\"']+")

# 「看看这个网页 / 打开这个链接 / 这个网址讲了啥」
BROWSE_RE = re.compile(r"(看看|看一下|打开|读一下|访问|浏览).{0,6}(网页|链接|网址|页面|网站)")
SHOT_RE = re.compile(r"(截图|截个图|截屏)")


class FeiyuBrowse(Star):
    """大肥鱼·看网页：只读浏览器，用真实登录态读网页 / 截图，不点击、不填表。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self.plugin_cfg = dict(config or {})
        self.cfg = dict(DEFAULT_CONFIG)
        self._sessions: dict[str, str] = {}
        if self.cfg_path.exists():
            try:
                self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"[feiyu_browse] 配置读取失败: {e}")
        else:
            self.cfg_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.plugin_cfg:
            # 插件配置优先于本地 config.json，空值不覆盖
            self.cfg.update({k: v for k, v in self.plugin_cfg.items() if v not in (None, "")})

    # ---------- bsk 调用 ----------

    def _bsk_exe(self) -> str:
        """bsk 可执行文件：配置为空就交给 PATH 找。"""
        return str(self.cfg.get("bsk_path") or "bsk")

    def _env(self) -> dict:
        env = dict(os.environ)
        env["BSK_AUTO_START"] = "0"
        bsk_path = str(self.cfg.get("bsk_path") or "")
        if bsk_path:
            env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path(bsk_path).parent)
        return env

    def _browser_id(self) -> str:
        """拿当前在线的浏览器实例；配置里写了就用配置的。"""
        cfg = str(self.cfg.get("browser", "")).strip()
        if cfg:
            return cfg
        try:
            r = self._bsk("browsers", "--json", timeout=30)
            arr = json.loads(r.stdout or "[]")
            if arr:
                return arr[0]["instance_id"]
        except Exception as e:
            logger.warning(f"[feiyu_browse] 取浏览器列表失败: {e}")
        return ""

    def _bsk(self, *args, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._bsk_exe(), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout or int(self.cfg.get("timeout", 120)),
            env=self._env(),
            creationflags=SUBPROCESS_FLAGS,
        )

    def _open_and_read(self, url: str) -> tuple[str, str]:
        """返回 (正文文本, session_id 已清理)"""
        bid = self._browser_id()
        start_args = ["session", "start", "--json", "--no-focus"] + (["--browser", bid] if bid else [])
        r = self._bsk(*start_args)
        if r.returncode != 0:
            raise RuntimeError(f"bsk 会话起不来: {(r.stderr or r.stdout)[:200]}")
        sid = json.loads(r.stdout).get("session_id")
        if not sid:
            raise RuntimeError("拿不到 session_id")
        try:
            self._bsk("navigate", url, "--session", sid)
            obs = self._bsk("observe", "--session", sid, "--json")
            data = json.loads(obs.stdout or "{}")
            return data.get("text", ""), sid
        finally:
            self._bsk("session", "stop", sid, timeout=60)

    def _screenshot(self, url: str) -> str:
        out = self.data_dir / "shots" / f"s_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        bid = self._browser_id()
        start_args = ["session", "start", "--json", "--no-focus"] + (["--browser", bid] if bid else [])
        r = self._bsk(*start_args)
        if r.returncode != 0:
            raise RuntimeError("bsk 会话起不来，daemon 或扩展可能没连上")
        sid = json.loads(r.stdout).get("session_id")
        try:
            self._bsk("navigate", url, "--session", sid)
            self._bsk("screenshot", "--session", sid, "--out", str(out))
            return str(out)
        finally:
            self._bsk("session", "stop", sid, timeout=60)

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str):
        """联网搜索。当用户问"这是谁/哪里/什么时候/多少钱"这类需要最新信息的问题，或让你查一个人的资料时使用。

        Args:
            query(string): 搜索关键词，尽量具体
        """
        import urllib.request

        key = self.cfg.get("search_key", "")
        api = self.cfg.get("search_api", "https://api.deepseek.com/v1/tools/search")
        q = (query or "").strip()
        if not q:
            yield event.plain_result("搜什么？")
            return
        try:
            payload = json.dumps({"query": q[:120], "count": 5, "need_content": True}).encode()
            req = urllib.request.Request(api, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            })
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            data = json.loads(await asyncio.to_thread(lambda: opener.open(req, timeout=30).read()))
            lines = []
            for r in (data.get("results") or [])[:5]:
                t = (r.get("title") or "").strip()
                sm = (r.get("summary") or r.get("content") or "").strip().replace("\n", " ")
                if t or sm:
                    lines.append(f"- {t}：{sm[:120]}")
        except Exception as e:
            logger.warning(f"[feiyu_browse] 搜索失败: {e}")
            lines = []

        if not lines:
            yield event.plain_result("没搜到，换个关键词试试")
            return
        yield event.plain_result("搜到这些：\n" + "\n".join(lines)[:900])

    @filter.llm_tool(name="read_webpage")
    async def read_webpage(self, event: AstrMessageEvent, url: str):
        """用真实浏览器（带用户登录态）打开一个网页，读出页面文字内容。

        什么时候用：用户给了一个需要登录、或者普通抓取抓不到内容的网页链接，让你"看看/总结一下"。
        只读：只能读页面，不能点击、填写、登录或提交任何东西。

        Args:
            url(string): 完整的 http/https 链接
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            yield event.plain_result("给我一个完整的 http/https 链接")
            return
        try:
            text, _ = await asyncio.to_thread(self._open_and_read, url)
        except Exception as e:
            logger.error(f"[feiyu_browse] 读网页失败: {e}")
            yield event.plain_result(f"网页打不开：{type(e).__name__}")
            return
        text = (text or "").strip()
        if not text:
            yield event.plain_result("页面打开了，但没读出文字（可能是纯图片或需要登录）")
            return
        limit = int(self.cfg.get("max_chars", 3000))
        yield event.plain_result(text[:limit] + ("\n…（截断）" if len(text) > limit else ""))

    @filter.llm_tool(name="screenshot_webpage")
    async def screenshot_webpage(self, event: AstrMessageEvent, url: str):
        """用真实浏览器打开网页并截图，把图片发给用户。只在用户明确要"截图/看看长什么样"时用。

        Args:
            url(string): 完整的 http/https 链接
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            yield event.plain_result("给我一个完整的 http/https 链接")
            return
        try:
            path = await asyncio.to_thread(self._screenshot, url)
        except Exception as e:
            logger.error(f"[feiyu_browse] 截图失败: {e}")
            yield event.plain_result(f"截图失败：{type(e).__name__}")
            return
        img = Comp.Image.fromFileSystem(path)
        try:
            await event.send(MessageChain([img]))
        except Exception as e:
            logger.error(f"[feiyu_browse] 发图失败: {e}")
            yield event.chain_result([img])

    # ---------- 交互工具（仅管理员） ----------

    def _session_start(self) -> str:
        bid = self._browser_id()
        args = ["session", "start", "--json", "--no-focus"] + (["--browser", bid] if bid else [])
        r = self._bsk(*args)
        if r.returncode != 0:
            # 浏览器实例可能变了：重新发现一次再试
            try:
                rr = self._bsk("browsers", "--json", timeout=30)
                arr = json.loads(rr.stdout or "[]")
                if arr:
                    fresh = arr[0]["instance_id"]
                    self.cfg["browser"] = fresh
                    args = ["session", "start", "--json", "--no-focus", "--browser", fresh]
                    r = self._bsk(*args)
            except Exception:
                pass
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[:200])
        return json.loads(r.stdout)["session_id"]

    def _bsk_do(self, *args) -> str:
        r = self._bsk(*args, timeout=120)
        return (r.stdout or "") + (r.stderr or "")

    @filter.llm_tool(name="browse_open")
    async def browse_open(self, event: AstrMessageEvent, url: str):
        """打开一个网页并保持会话，后续可以点击、填表、上传。仅管理员可用，浏览器在后台运行不抢焦点。

        Args:
            url(string): http/https 链接
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用")
            return
        try:
            sid = await asyncio.to_thread(self._session_start)
            await asyncio.to_thread(self._bsk_do, "navigate", url, "--session", sid)
            text = await asyncio.to_thread(self._read_text, sid)
        except Exception as e:
            yield event.plain_result(f"打开失败：{type(e).__name__}")
            return
        self._sessions[event.unified_msg_origin] = sid
        yield event.plain_result(f"已打开（会话 {sid}）\n{(text or '')[:600]}")

    @filter.llm_tool(name="browse_act")
    async def browse_act(self, event: AstrMessageEvent, action: str, target: str = "", value: str = ""):
        """在已打开的页面上操作。仅管理员可用。

        action 取值：click（点 selector）、fill（往 selector 填 value）、upload（往 selector 传本地文件）、read（读页面文字）、close（关会话）。

        Args:
            action(string): click / fill / upload / read / close
            target(string): CSS 选择器；upload 时是文件输入框的选择器
            value(string): fill 时填的内容；upload 时是本地文件路径
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用")
            return
        sid = self._sessions.get(event.unified_msg_origin)
        if not sid:
            yield event.plain_result("先让我打开一个网页")
            return
        try:
            if action == "click":
                out = await asyncio.to_thread(self._bsk_do, "click", target, "--session", sid)
            elif action == "fill":
                out = await asyncio.to_thread(self._bsk_do, "fill", target, "--value", value, "--session", sid)
            elif action == "upload":
                out = await asyncio.to_thread(self._bsk_do, "upload", target, "--file", value, "--session", sid)
            elif action == "read":
                out = await asyncio.to_thread(self._read_text, sid)
            elif action == "close":
                await asyncio.to_thread(self._bsk_do, "session", "stop", sid)
                self._sessions.pop(event.unified_msg_origin, None)
                yield event.plain_result("关了")
                return
            else:
                yield event.plain_result("action 只能是 click/fill/upload/read/close")
                return
        except Exception as e:
            yield event.plain_result(f"操作失败：{type(e).__name__}: {e}")
            return
        yield event.plain_result((out or "ok")[:800])

    def _read_text(self, sid: str) -> str:
        r = self._bsk("observe", "--session", sid, "--json", timeout=90)
        try:
            return json.loads(r.stdout or "{}").get("text", "")
        except Exception:
            return r.stdout or ""

    # ---------- 指令 ----------

    @filter.command("看网页")
    async def cmd_read(self, event: AstrMessageEvent):
        """读网页：/看网页 https://..."""
        m = URL_RE.search(event.message_str or "")
        if not m:
            yield event.plain_result("用法：/看网页 https://example.com")
            return
        yield event.plain_result("打开中…")
        try:
            text, _ = await asyncio.to_thread(self._open_and_read, m.group(0))
        except Exception as e:
            yield event.plain_result(f"失败：{type(e).__name__}: {e}")
            return
        limit = int(self.cfg.get("max_chars", 3000))
        yield event.plain_result((text or "（空页面）")[:limit])

    @filter.command("网页截图")
    async def cmd_shot(self, event: AstrMessageEvent):
        """网页截图：/网页截图 https://..."""
        m = URL_RE.search(event.message_str or "")
        if not m:
            yield event.plain_result("用法：/网页截图 https://example.com")
            return
        yield event.plain_result("截图中…")
        try:
            path = await asyncio.to_thread(self._screenshot, m.group(0))
        except Exception as e:
            yield event.plain_result(f"失败：{type(e).__name__}: {e}")
            return
        yield event.chain_result([Comp.Image.fromFileSystem(path)])

    # ---------- 自然语言触发（模型不主动调时的兜底） ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_browse(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            return
        m = URL_RE.search(text)
        if not m:
            return
        if not (BROWSE_RE.search(text) or SHOT_RE.search(text)):
            return
        url = m.group(0)
        event.stop_event()
        if SHOT_RE.search(text):
            try:
                path = await asyncio.to_thread(self._screenshot, url)
            except Exception as e:
                yield event.plain_result(f"截图失败：{type(e).__name__}")
                return
            img = Comp.Image.fromFileSystem(path)
            try:
                await event.send(MessageChain([img]))
            except Exception:
                yield event.chain_result([img])
            return
        try:
            body, _ = await asyncio.to_thread(self._open_and_read, url)
        except Exception as e:
            yield event.plain_result(f"网页打不开：{type(e).__name__}")
            return
        limit = int(self.cfg.get("max_chars", 3000))
        yield event.plain_result((body or "（空页面）")[:limit])
