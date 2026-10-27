import asyncio
import json
import os
import re
from urllib.parse import urlsplit
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_KIT_PATH = Path(__file__).resolve().parent / "_feiyu_kit.py"
kit = _sys.modules.get("_feiyu_kit")
if kit is None:
    # 全进程只有一份 kit：动作注册表必须共享，所以谁先加载谁就是那份，后来者直接复用。
    # 磁盘上的 kit 改动要重启整个 AstrBot 进程才生效（面板重载插件不会换掉已经加载的模块）。
    import importlib.util as _ilu  # noqa: PLC0415

    _spec = _ilu.spec_from_file_location("_feiyu_kit", _KIT_PATH)
    _fresh = _ilu.module_from_spec(_spec)
    _sys.modules["_feiyu_kit"] = _fresh
    _spec.loader.exec_module(_fresh)
    kit = _fresh

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
        kit.register_capability("feiyu_browse", "能上网搜东西、能把网页上的字读出来、能截网页给你看", "all")
        kit.register_capability("feiyu_browse_admin",
                                 "调试用的浏览器操作口（点按钮、填表、上传），只有主人明确要求时才用，平时走看视频、搜、读网页、截图",
                                 "admin")
        kit.register_action("搜", self.do_search, "联网搜索，参数 query")
        kit.register_action("读网页", self.do_read, "读网页正文，参数 url")
        kit.register_action("截图", self.do_shot, "网页截图并发出去，参数 url")
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

    def _landing_url(self, sid: str) -> str:
        """拿到导航落地后的真实 URL（302/JS 跳转都可能把它带到别处）。"""
        try:
            js = "(() => ({href: location.href}))()"
            r = self._bsk("evaluate", "--session", sid, "--json", js, timeout=30)
            val = json.loads(r.stdout or "{}")
            inner = val.get("value") if isinstance(val.get("value"), dict) else val
            return str((inner or {}).get("href") or "").strip()
        except Exception:
            return ""

    def _blocked_landing(self, sid: str, event=None) -> str:
        """导航落地后再过一遍闸门：白名单站被注入跳转到内网时，这里拦住。"""
        href = self._landing_url(sid)
        if not href:
            return ""
        reason = self._blocked_reason(event, href) if event is not None else self._gate_url(href)
        if reason:
            return f"页面跳到了不安全的地址（{href[:80]}），内容我不看。"
        return ""

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
            blocked = self._blocked_landing(sid)
            if blocked:
                return blocked, ""
            # 先要人能读的正文，拿不到再退回无障碍树
            js = "(() => { const b = document.body; return {text: b ? b.innerText : ''}; })()"
            try:
                r2 = self._bsk("evaluate", "--session", sid, "--json", js, timeout=90)
                val = json.loads(r2.stdout or "{}")
                inner = val.get("value") if isinstance(val.get("value"), dict) else val
                text = str((inner or {}).get("text") or "").strip()
                if text:
                    return text, sid
            except Exception as e:
                logger.warning(f"[feiyu_browse] 取正文失败，退回无障碍树: {e}")
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
            blocked = self._blocked_landing(sid)
            if blocked:
                raise RuntimeError(blocked)
            self._bsk("screenshot", "--session", sid, "--out", str(out))
            return str(out)
        finally:
            self._bsk("session", "stop", sid, timeout=60)

    # ---------- LLM 工具 ----------

    # ---------- 联网搜索 ----------
    # 结果以「标题（来源 · 日期） + 链接 + 摘要」给出，AI 看完可以再用 read_webpage 打开最相关的那条。

    _SEARCH_CACHE: dict = {}
    _CACHE_TTL = 600

    def _search_api(self, query: str, count: int = 5, need_content: bool = True):
        key = str(self.cfg.get("search_key") or "").strip()
        if not key:
            raise RuntimeError("插件配置里没填 search_key")
        hits = kit.deepseek_search(query[:120], count=count, need_content=need_content,
                                 key=key, api=str(self.cfg.get("search_api") or ""))
        # 保持和历史字段一致，下游 _format_hits 不用改
        return [{
            "title": h.get("title", ""), "url": h.get("url", ""),
            "site_name": h.get("site", ""), "date": h.get("date", ""),
            "summary": h.get("summary", ""), "content": h.get("summary", ""),
        } for h in hits]

    def _format_hits(self, hits, limit: int = 1600) -> str:
        lines = []
        for i, h in enumerate(hits, 1):
            title = re.sub(r"\s+", " ", str(h.get("title") or "")).strip()
            url = str(h.get("url") or "").strip()
            if len(url) > 140:  # 带一堆追踪参数的链接看着累, 只留主干
                url = url.split("?", 1)[0]
            site = str(h.get("site_name") or "").strip()
            day = str(h.get("date") or "").strip()[:10]
            body = str(h.get("summary") or h.get("content") or h.get("snippet") or "")
            body = re.sub(r"<!\[CDATA\[|\]\]>", "", body)
            body = re.sub(r"(?m)^\s*#{1,6}\s*", "", body)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            body = re.sub(r"^(\.\.\.|…|\[?CDATA)\s*", "", body)
            body = re.sub(r"(\.\.\.|…)+", "…", body).strip(" .·…")
            body = body[:140]
            meta = " · ".join(x for x in (site, day) if x)
            lines.append(f"{i}. {title}" + (f"（{meta}）" if meta else ""))
            if url:
                lines.append(f"   {url}")
            if body:
                lines.append(f"   {body}")
        return "\n".join(lines)[:limit]

    async def do_search(self, event: AstrMessageEvent, query: str, count: int = 5) -> str:
        """联网搜索. 遇到不知道的、需要最新消息的、或者要查人查事的, 先用它搜.
        会返回每条结果的标题、来源、发布时间、链接和摘要; 想细看某条就用「读网页」打开它的链接.

        Args:
            query(string): 搜索关键词, 说具体点
            count(number): 要几条, 1-8, 默认 5
        """
        q = str(query or "").strip()
        if not q:
            return "没有关键词，搜不了。"
        try:
            n = max(1, min(8, int(count or 5)))
        except Exception:
            n = 5

        ck = f"{n}|{q}"
        hit = self._SEARCH_CACHE.get(ck)
        if hit and time.time() - hit[0] < self._CACHE_TTL:
            return hit[1]

        hits = []
        for attempt in (1, 2):
            try:
                hits = await asyncio.to_thread(self._search_api, q, n, True)
                break
            except Exception as e:
                logger.warning(f"[feiyu_browse] 搜索接口第 {attempt} 次失败: {e}")
                if attempt == 1:
                    await asyncio.sleep(1.5)

        if not hits:
            return "这会儿搜不动（接口没响应）。可以过一会儿再试，或者让用户直接把链接给你。"

        text = "搜到这些：\n" + self._format_hits(hits)
        self._SEARCH_CACHE[ck] = (time.time(), text)
        if len(self._SEARCH_CACHE) > 80:
            self._SEARCH_CACHE.clear()
        # 留一条素材：她查过什么、看到什么，会进日记，变成「我今天翻过的东西」
        top = hits[0] if hits else {}
        try:
            head = f"搜了《{q}》，看到「{str(top.get('title') or '').strip()}」"
            body = str(top.get("summary") or "").strip()[:120]
            kit.record_material(head + ("，" + body if body else ""),
                                kind="event", source="browse",
                                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                                mood={"P": 2.0, "Ar": 1.0})
        except Exception:
            pass
        return text

    # ---------- 链接闸门：这台浏览器是主人日常登录态，不能随便开 ----------
    _ALLOW_SUFFIX = (
        "bilibili.com", "b23.tv", "bili2233.cn", "zhihu.com", "zhimg.com",
        "weixin.qq.com", "mp.weixin.qq.com", "github.com", "gitee.com",
        "douyin.com", "xiaohongshu.com", "weibo.com", "baidu.com", "qq.com",
        "docs.python.org", "developer.mozilla.org", "stackoverflow.com",
    )

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return (urlsplit(url).hostname or "").lower()
        except Exception:
            return ""

    @classmethod
    def _private_host(cls, host: str) -> bool:
        if not host:
            return True
        try:
            import ipaddress

            return not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
            return True
        try:
            import socket as _s

            infos = _s.getaddrinfo(host, None)
        except Exception:
            return False
        for info in infos:
            try:
                ip = info[4][0]
                if not ipaddress.ip_address(ip.split("%")[0]).is_global:
                    return True
            except Exception:
                continue
        return False

    @classmethod
    def _trusted(cls, url: str) -> bool:
        host = cls._host_of(url)
        if not host or cls._private_host(host):
            return False
        return any(host == d or host.endswith("." + d) for d in cls._ALLOW_SUFFIX)

    @classmethod
    def _gate_url(cls, url: str) -> str:
        """只看 URL 的闸门：内网/本机一律拒；白名单之外的公网站放行（落地检查用）。"""
        host = cls._host_of(url)
        if not host or cls._private_host(host):
            return "本机/内网地址"
        return ""

    def _blocked_reason(self, event, url: str) -> str:
        host = self._host_of(url)
        if not host or self._private_host(host):
            return "这个地址我不会开（本机/内网），要看的话你自己打开截图给我。"
        if not self._trusted(url):
            try:
                admin = kit.is_owner(event, self.cfg)
            except Exception:
                admin = False
            if not admin:
                return "这个站我不随便开，让主人来。"
        return ""

    async def do_read(self, event: AstrMessageEvent, url: str) -> str:
        """用真实浏览器（带用户登录态）打开一个网页，读出页面文字内容。

        什么时候用：用户给了一个需要登录、或者普通抓取抓不到内容的网页链接，让你"看看/总结一下"。
        只读：只能读页面，不能点击、填写、登录或提交任何东西。

        Args:
            url(string): 完整的 http/https 链接
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "要一个完整的 http/https 链接才能打开。"
        why = self._blocked_reason(event, url)
        if why:
            return why
        try:
            text, _ = await asyncio.to_thread(self._open_and_read, url)
        except Exception as e:
            logger.error(f"[feiyu_browse] 读网页失败: {e}")
            return f"网页打不开（{type(e).__name__}）。"
        text = (text or "").strip()
        if not text:
            return "页面打开了，但没读出文字（可能是纯图片，或者要登录）。"
        try:
            kit.record_material(f"读了 {url} 这一页：{text[:160]}", kind="event", source="browse",
                                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                                mood={"P": 1.2, "Ar": 0.8})
        except Exception:
            pass
        limit = int(self.cfg.get("max_chars", 3000))
        if len(text) > limit:
            text = text[:limit] + "\n…（后面截断了）"
        return text

    async def do_shot(self, event: AstrMessageEvent, url: str) -> str:
        """用真实浏览器打开网页并截图发出去。只在用户明确要"截图/看看长什么样"时用。

        Args:
            url(string): 完整的 http/https 链接
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "要一个完整的 http/https 链接才能打开。"
        why = self._blocked_reason(event, url)
        if why:
            return why
        try:
            path = await asyncio.to_thread(self._screenshot, url)
        except Exception as e:
            logger.error(f"[feiyu_browse] 截图失败: {e}")
            return f"截图失败（{type(e).__name__}）。"
        img = Comp.Image.fromFileSystem(path)
        try:
            await event.send(MessageChain([img]))
        except Exception as e:
            logger.error(f"[feiyu_browse] 发图失败: {e}")
            return f"截好图了但没发出去（{type(e).__name__}），你再试一次。"
        return "截图已经发出去了。你自己清楚拍到的是什么，照着说就行，别一句句描述。"

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

    async def _page_open(self, event: AstrMessageEvent, url: str) -> str:
        """打开一个网页并保持会话，后续可以点击、填表、上传。仅管理员可用，浏览器在后台运行不抢焦点。

        Args:
            url(string): http/https 链接
        """
        if not kit.is_owner(event, self.cfg):
            return "这个得他本人来，我这边只做看得见的那部分：搜、读、截图。"
        reason = self._blocked_reason(event, url)
        if reason:
            return reason
        try:
            sid = await asyncio.to_thread(self._session_start)
            await asyncio.to_thread(self._bsk_do, "navigate", url, "--session", sid)
            text = await asyncio.to_thread(self._read_text, sid)
        except Exception as e:
            return f"打开失败（{type(e).__name__}）。"
        self._sessions[event.unified_msg_origin] = sid
        body = (text or "").strip()
        if len(body) > 600:
            body = body[:600] + "…"
        return f"打开了（会话 {sid}）。页面上的字：\n{body or '（空页面）'}"

    async def _page_act(self, event: AstrMessageEvent, action: str, target: str = "", value: str = "") -> str:
        """在已打开的页面上操作。仅管理员可用。

        action 取值：click（点 selector）、fill（往 selector 填 value）、upload（往 selector 传本地文件）、read（读页面文字）、close（关会话）。

        Args:
            action(string): click / fill / upload / read / close
            target(string): CSS 选择器；upload 时是文件输入框的选择器
            value(string): fill 时填的内容；upload 时是本地文件路径
        """
        if not kit.is_owner(event, self.cfg):
            return "这个得他本人来，我这边只做看得见的那部分：搜、读、截图。"
        sid = self._sessions.get(event.unified_msg_origin)
        if not sid:
            return "还没来得及打开页面。先用 page_do(action=open, target=链接) 开一个。"
        try:
            if action == "click":
                out = await asyncio.to_thread(self._bsk_do, "click", target, "--session", sid)
            elif action == "fill":
                out = await asyncio.to_thread(self._bsk_do, "fill", target, "--value", value, "--session", sid)
            elif action == "upload":
                bad = self._bad_upload_path(value)
                if bad:
                    return bad
                out = await asyncio.to_thread(self._bsk_do, "upload", target, "--file", value, "--session", sid)
            elif action == "read":
                out = await asyncio.to_thread(self._read_text, sid, target)
            elif action == "close":
                await asyncio.to_thread(self._bsk_do, "session", "stop", sid)
                self._sessions.pop(event.unified_msg_origin, None)
                return "页面关了。"
            else:
                return "action 只能是 click / fill / upload / read / close。"
        except Exception as e:
            return f"操作失败（{type(e).__name__}: {e}）。"
        out = (out or "").strip() or "操作完成了，页面上没什么特别的变化。"
        if len(out) > 800:
            out = out[:800] + "…"
        return out

    _UPLOAD_SUFFIX = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".txt", ".md",
                      ".csv", ".json", ".docx", ".xlsx", ".pptx", ".zip", ".mp4", ".mp3")

    def _bad_upload_path(self, value: str) -> str:
        """上传口不能变成「把任意本地文件塞进网页」的后门：只允许普通文档/图片，且不许指到家目录与系统目录。"""
        raw = (value or "").strip()
        if not raw:
            return "要传哪个文件？给我个路径。"
        try:
            p = Path(os.path.expanduser(raw)).resolve()
        except Exception:
            return "这个路径我看不懂。"
        if not p.is_file():
            return f"没找到这个文件：{p.name}"
        if p.suffix.lower() not in self._UPLOAD_SUFFIX:
            return f"这种文件我不传（{p.suffix or '无扩展名'}），只能传图片、文档和表格。"
        home = Path.home().resolve()
        roots = [self.data_dir / "uploads", home / "Desktop", home / "Downloads"]
        name_low = p.name.lower()
        banned_name = ("credential", "cookie", "token", "secret", ".env", "2fa", "恢复码", "密码")
        if any(b in name_low for b in banned_name):
            return "这个文件名看着像密钥/恢复码，我不传。"
        if not any(str(p).startswith(str(r.resolve())) for r in roots):
            return "只能传桌面、下载或插件 uploads 目录里的文件（换个位置放一份再传）。"
        if p == home:
            return "这个位置的文件我不能传。"
        if p.stat().st_size > 60 * 1024 * 1024:
            return "文件太大了（超过 60MB），先压缩一下。"
        return ""

    @filter.llm_tool(name="page_do")
    async def page_do(self, event: AstrMessageEvent, action: str, target: str = "", value: str = "") -> str:
        """调试用：在真实浏览器里打开或操作页面，只有主人用得着。
        平时不要调它——看视频用「看视频」，查东西用「搜」，读文章用「读网页」，要截图用「截图」。
        只有主人明确说「点这个按钮/填这个表/传这个文件」时才用它。

        Args:
            action(string): open（target 填链接）/ click / fill / upload / read / close
            target(string): 链接或 CSS 选择器
            value(string): fill 填的内容；upload 填本地文件路径
        """
        if not kit.is_owner(event, self.cfg):
            return "这个得他本人来，我这边只做看得见的那部分：搜、读、截图。"
        if (action or "").strip().lower() in ("open", "打开"):
            return await self._page_open(event, target)
        return await self._page_act(event, action, target, value)

    def _wait_dom(self, sid: str, seconds: float = 8.0) -> None:
        """等页面把字渲染出来（B 站是 SPA，navigate 返回时正文常常还是空的）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                r = self._bsk("evaluate", "--session", sid, "--json",
                              "(() => ({len: (document.body ? document.body.innerText.length : 0), "
                              "ready: document.readyState}))()", timeout=30)
                val = json.loads(r.stdout or "{}")
                info = val.get("value") if isinstance(val.get("value"), dict) else val
                if isinstance(info, dict) and int(info.get("len") or 0) > 40:
                    return
            except Exception:
                pass
            time.sleep(0.8)

    def _read_text(self, sid: str, selector: str = "") -> str:
        """读页面正文。无障碍树那套 @vom 机器话对她没用，只拿 innerText。"""
        sel = (selector or "").strip()
        self._wait_dom(sid)
        js = (
            "(() => { const e = " + ("document.querySelector(" + repr(sel) + ")" if sel else "document.body")
            + "; return {text: e ? (e.innerText || '') : '', body: (document.body ? document.body.innerText.slice(0, 300) : '')}; })()"
        )
        try:
            r = self._bsk("evaluate", "--session", sid, "--json", js, timeout=90)
            val = json.loads(r.stdout or "{}")
            inner = val.get("value") if isinstance(val.get("value"), dict) else val
            text = str((inner or {}).get("text") or "").strip()
            if text:
                return text
            if sel:
                sample = str((inner or {}).get("body") or "").strip()
                sample = re.sub(r"\n{2,}", "\n", sample)[:200]
                return f"页面上找不到「{sel}」这个位置。现在能看到的开头是：{sample or '（空）'}\n别猜里面有什么，换一个能看见的地方读。"
        except Exception as e:
            return f"读页面失败（{type(e).__name__}）。别猜内容，如实说没读到。"
        return "页面这会儿还没渲染出字（可能要登录或还在加载），别猜内容。"

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
