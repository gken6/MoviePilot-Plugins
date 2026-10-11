"""豆瓣观看同步：把媒体服务器的播放进度同步到豆瓣「书影音档案」。

设计要点：
1. 不依赖 TMDB 识别，TMDB 不可用时仍能完成同步；
2. 豆瓣 ID 优先取媒体服务器已刮削的标识，取不到才用豆瓣搜索；
3. 网络请求全部放到宿主的延后任务里执行，不阻塞 webhook 事件线程；
4. 同步失败的条目进入待处理队列，由定时服务重试。
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.schemas import WebhookEventInfo
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import MediaServerHelper
from app.sdk import scheduler as scheduler_sdk

try:  # 包内相对导入，兼容宿主重建实例的命名空间
    from .auth import AutomaticLogin
    from .doubanclient import DoubanClient
    from .management import WatchManagement
    from .mediaserver import MediaServerReader
except Exception:  # pragma: no cover - 极端加载路径兜底
    from app.plugins.doubanwatchsync.auth import AutomaticLogin
    from app.plugins.doubanwatchsync.doubanclient import DoubanClient
    from app.plugins.doubanwatchsync.management import WatchManagement
    from app.plugins.doubanwatchsync.mediaserver import MediaServerReader

# 播放开始事件：开播那一刻先记一条「在看」
_PLAY_START = {"playback.start", "media.play", "PlaybackStart"}
# 播放结束事件：播完 / 暂停 / 退出都会触发，是判定「看完」的关键时机
_PLAY_STOP = {"playback.stop", "media.stop", "PlaybackStop", "PlaybackPause"}
# 标记已播放事件：用户在媒体服务器里手动打勾
_PLAYED = {"item.markplayed", "media.scrobble", "UserDataSaved"}
# 电影播完即视为看完
_MOVIE_TYPES = {"MOV", "MOVIE"}
# 剧集集号解析失败时的兜底：豆瓣只区分「在看 / 看过」，不做百分比

# 存档键，避免字符串散落各处
_KEY_ARCHIVE = "archive"
# 待重试队列键
_KEY_PENDING = "pending"
# 仪表盘 key，宿主用它区分同一插件的多个仪表盘
_KEY_DASHBOARD = "archive"
# 重扫诊断结果键，便于排查「为什么没有升级」
_KEY_DIAG = "diagnose"
# 豆瓣图片 data URI 缓存键（避免每次刷新都回源）
_KEY_IMAGES = "images"
# 单次渲染最多回源几张图，避免仪表盘请求太久
_INLINE_BUDGET = 8
# 轮询播放会话的去重状态
_KEY_POLL = "poll"
# 图片缓存最多保留多少张
_IMAGE_CACHE_MAX = 200


class DoubanWatchSync(AutomaticLogin, WatchManagement, _PluginBase):
    """把剧集/电影的观看进度写入豆瓣书影音档案。"""

    # 插件基本信息
    plugin_name = "豆瓣观看同步"
    plugin_desc = "将在看、看完状态同步到豆瓣书影音档案；支持多媒体服务器路由、独立档案与重试队列。"
    plugin_icon = "https://raw.githubusercontent.com/gken6/MoviePilot-Plugins/main/icons/doubanwatchsync.svg"
    plugin_version = "2.3.3"
    plugin_author = "gken6"
    author_url = "https://github.com/gken6"
    plugin_config_prefix = "doubanwatchsync_"
    plugin_order = 20
    auth_level = 1

    # 运行期状态
    _enabled = False
    _private = True
    _skip_first = True
    _users = ""
    _user_ids = ""
    _direct_servers = ""
    _direct_accounts = ""
    _direct_poll_servers: Any = []
    _exclude = ""
    _cookie = ""
    _use_cookiecloud = True
    _cc_host = ""
    _cc_key = ""
    _cc_password = ""
    _pc_month = 3
    _pc_num = 50
    _mobile_month = 2
    _mobile_num = 15
    # 只同步这些媒体库，留空表示全部；多选组件存库名数组
    _libraries: Any = ()
    # 指定媒体服务器名称。v1.1.0 起配置项 server 为多选列表；仍兼容旧版字符串。
    _server: Any = []
    # 豆瓣图片有防盗链，仪表盘用后端取回的 data URI，这里做内存缓存
    _inline_image = True
    _inline_limit = 20

    # 保护存档读写，避免并发任务互相覆盖
    _lock = threading.RLock()
    _record_locks = {}
    # 图片地址 -> data URI，进程内共享，重启后按需重新拉取
    _image_cache: Dict[str, str] = {}
    # 渲染期临时状态：图片映射、是否有新增、本次还能拉几张
    _image_map: Dict[str, str] = {}
    _image_dirty = False
    _image_budget = 0
    _image_failed: set = set()

    # ---------------- 生命周期 ----------------

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并初始化运行期状态，可重复调用。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._private = bool(config.get("private", True))
        self._skip_first = bool(config.get("skip_first", True))
        self._users = str(config.get("users") or "")
        self._user_ids = str(config.get("user_ids") or "")
        self._direct_servers = str(config.get("direct_servers") or "")
        self._direct_accounts = str(config.get("direct_accounts") or "")
        self._direct_poll_servers = config.get("direct_poll_servers") or []
        self._exclude = str(config.get("exclude") or "")
        self._cookie = str(config.get("cookie") or "")
        self._use_cookiecloud = bool(config.get("use_cookiecloud", True))
        self._cc_host = str(config.get("cookiecloud_host") or "")
        self._cc_key = str(config.get("cookiecloud_key") or "")
        self._cc_password = str(config.get("cookiecloud_password") or "")
        self._pc_month = self._to_int(config.get("pc_month"), 3, 2)
        self._pc_num = self._to_int(config.get("pc_num"), 50, 1)
        self._mobile_month = self._to_int(config.get("mobile_month"), 2, 2)
        self._mobile_num = self._to_int(config.get("mobile_num"), 15, 1)
        self._libraries = config.get("libraries") or []
        self._server = config.get("server") or []
        self._inline_image = bool(config.get("inline_image", True))
        self._inline_limit = self._to_int(config.get("inline_limit"), 20, 1)

        self._auth_init(config)
        self._management_init(config)

        if config.get("onlyonce"):
            # 勾选立即运行一次：延后执行待处理队列重试
            self._schedule_once("retry_pending_once", self.retry_pending, "重试豆瓣同步失败队列", 3)
            config["onlyonce"] = False
            self.update_config(config)

        if config.get("rescan"):
            # 勾选重扫档案：按媒体服务器真实播放状态校正历史条目。
            # 这里不立刻改回 False，而是由重扫任务自身收尾时清除，
            # 避免前端开关保存失败时任务被静默取消。
            self._schedule_once("rescan_archive_once", self.rescan_archive, "重扫豆瓣档案", 5)
            logger.info("已登记重扫档案任务，稍后执行")

        if self._enabled:
            logger.info("豆瓣观看同步插件已启用")
        else:
            logger.info("豆瓣观看同步插件未启用")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册重试失败队列与重扫档案的远程命令。"""
        return [
            {
                "cmd": "/douban_watch_retry",
                "event": EventType.PluginAction,
                "desc": "重试豆瓣同步失败队列",
                "category": "插件命令",
                "data": {"action": "douban_watch_retry"},
            },
            {
                "cmd": "/douban_watch_rescan",
                "event": EventType.PluginAction,
                "desc": "重扫豆瓣观看同步档案",
                "category": "插件命令",
                "data": {"action": "douban_watch_rescan"},
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册每 30 分钟重试失败队列的周期任务。"""
        if not self._enabled:
            return []
        return [
            {
                "id": "DoubanWatchSync.Retry",
                "name": "豆瓣观看同步失败重试",
                "trigger": CronTrigger.from_crontab("*/30 * * * *"),
                "func": self.retry_pending,
                "kwargs": {},
            },
            {
                "id": "DoubanWatchSync.PollPlaying",
                "name": "豆瓣观看同步播放轮询",
                "trigger": CronTrigger.from_crontab("*/2 * * * *"),
                "func": self.poll_playing_sessions,
                "kwargs": {},
            },
        ]

    def _original_api(self) -> List[Dict[str, Any]]:
        """注册档案查询与重扫接口。"""
        return [
            {
                "path": "/archive",
                "endpoint": self.query_archive,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询豆瓣同步档案",
            },
            {
                "path": "/rescan",
                "endpoint": self.api_rescan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重扫档案并校正状态",
            },
            {
                "path": "/diagnose",
                "endpoint": self.api_diagnose,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "重扫诊断：逐条展示每个环节的读取结果",
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return self._original_api() + self.management_api()

    def api_rescan(self) -> Dict[str, Any]:
        """
        重扫接口：同步执行并直接返回逐条结果。

        早期版本把重扫丢进延后任务，接口立即返回「已提交」，
        失败时无处可查——实际排查时无法确认任务是否跑、卡在哪一步。
        这里改为同步执行：档案条数有限（通常几十条），耗时可接受，
        调用方能直接拿到每一档的处理结果。
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        return self.rescan_archive()

    def api_diagnose(self) -> Dict[str, Any]:
        """
        重扫诊断接口。

        重扫依赖「搜索剧集 → 读集列表 → 读播放状态 → 写豆瓣」四个环节，
        任一环失败都只会体现为「没升级」，难以定位。本接口把每一步的
        实际返回都记录下来，便于确认真实原因。
        """
        report: Dict[str, Any] = {"servers": self._selected_servers(), "items": []}
        archive = dict(self.get_data(_KEY_ARCHIVE) or {})
        report["libraries"] = self._available_libraries()

        for key, record in archive.items():
            if self._manual_guard(key) or not self._direct_record_allowed(record):
                summary["skipped"] += 1
                continue
            if not isinstance(record, dict):
                continue
            entry: Dict[str, Any] = {
                "key": key,
                "title": record.get("subject_name") or record.get("title") or key,
                "type": record.get("type"),
                "season": record.get("season"),
                "status": record.get("status"),
            }
            if not str(record.get("type") or "").startswith("电视剧"):
                entry["result"] = "跳过：非剧集"
                report["items"].append(entry)
                continue
            try:
                server_name = self._record_server(record)
                entry["server_name"] = server_name or ""
                reader = self._reader(server_name)
                series_id = reader.search_series(entry["title"], self._to_int(record.get("season"), 0, 0))
                entry["series_id"] = series_id or ""
                if not series_id:
                    entry["candidates"] = reader.build_title_candidates(
                        entry["title"], self._to_int(record.get("season"), 0, 0))
                    entry["result"] = "未搜索到剧集条目"
                    report["items"].append(entry)
                    continue

                season = self._to_int(record.get("season"), 0, 0)
                episodes = reader.get_season_episodes(series_id, season)
                entry["episodes_count"] = len(episodes)
                entry["episodes"] = episodes[:60]

                state = self._play_state_of_any_user(series_id, season, server_name)
                entry["played_true"] = sum(1 for value in state.values() if value)
                entry["played_false"] = sum(1 for value in state.values() if not value)
                entry["played_total"] = len(state)
                entry["users"] = self._target_users()
                if not state:
                    entry["result"] = "未读取到任何集"
                elif all(state.values()):
                    entry["result"] = "整季已看完，应升级为看过"
                else:
                    entry["result"] = "存在未看完的集，保持在看"
            except Exception as error:
                entry["result"] = f"异常：{type(error).__name__}: {error}"
            report["items"].append(entry)

        self.save_data(_KEY_DIAG, report)
        return report

    def stop_service(self) -> None:
        """停止时取消未执行的延后任务并关闭状态。"""
        self._remove_once("retry_pending_once")
        self._enabled = False

    # ---------------- 事件处理 ----------------

    @eventmanager.register(EventType.WebhookMessage)
    def handle_webhook(self, event: Event) -> None:
        """接收媒体服务器播放事件，登记需要同步的任务。"""
        info = getattr(event, "event_data", None)
        if not isinstance(info, WebhookEventInfo) or not self._enabled:
            return

        logger.info(
            f"收到事件：{info.event or '-'} | 标题：{info.item_name or '-'} | "
            f"类型：{info.item_type or '-'} | 用户：{info.user_name or '-'} | 服务器：{info.server_name or '-'}")
        if not self._is_target_user(info.user_name):
            logger.debug(f"用户 {info.user_name} 不在配置的媒体库用户名内，忽略")
            return
        if not self._is_sync_event(info):
            logger.debug(f"事件 {info.event} 不是播放或标记已观看事件，忽略")
            return
        event_server = self._event_server_name(info)
        if event_server is None:
            logger.debug(f"事件服务器 {info.server_name or '-'} 不在所选媒体服务器内，忽略")
            return

        title, media_type, season, episode = self._parse_media(info)
        if not title or not media_type:
            return

        # 媒体库过滤放在解析之后，用 item_id 反查物理路径，比匹配事件原文更可靠
        if not self._is_allowed_library(info):
            logger.debug(f"{title} 不在所选媒体库内，忽略")
            return
        if not self._is_allowed_path(info.item_path):
            logger.debug(f"路径 {info.item_path} 命中排除关键词，忽略")
            return

        # 首集跳过只影响「是否写入档案」，不能影响末集升级为看过
        if media_type == "TV" and self._skip_first and episode == 1:
            logger.info(f"{title} 第 1 集不同步到豆瓣档案")
            return

        payload = {
            "title": title,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "item_id": info.item_id or "",
            "server_name": event_server or "",
            "user_name": info.user_name or "",
            "image": info.image_url or "",
            "event": info.event or "",
            "percentage": info.percentage,
        }
        job_id = f"sync_{media_type}_{re.sub(r'[^0-9A-Za-z]+', '', title)}_{season}_{episode}"
        self._schedule_once(job_id, lambda: self._sync_item(payload), f"同步 {title} 到豆瓣", 3)

    def poll_playing_sessions(self) -> Dict[str, Any]:
        """轮询媒体服务器当前播放会话，弥补外部 Emby 无法配置 Webhook 的场景。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        servers = self._selected_servers()
        if not servers:
            servers = [item.get("value") for item in self._server_items() if item.get("value")]
        if not servers:
            servers = [None]

        poll_state = dict(self.get_data(_KEY_POLL) or {})
        scheduled = 0
        seen_now = set()

        for server_name in servers:
            server = str(server_name or "").strip()
            try:
                reader = self._reader(server or None)
                sessions = reader.get_playing_sessions()
            except Exception as error:
                logger.debug(f"轮询媒体服务器 {server or '-'} 播放会话失败：{error}")
                continue

            for session in sessions:
                payload = self._payload_from_session(session, server)
                if not payload:
                    continue
                if not self._is_target_user(payload.get("user_name")):
                    continue

                # 同一条目同一状态只同步一次；进度跨过 90% 会再次同步为看过。
                bucket = "collect" if self._is_finished_event(payload) else "do"
                marker_key = f"{server}|{payload.get('user_name')}|{payload.get('item_id')}|{bucket}"
                seen_now.add(marker_key)
                if poll_state.get(marker_key):
                    continue
                poll_state[marker_key] = datetime.now().isoformat(timespec="seconds")

                title = payload.get("title") or payload.get("item_id") or "媒体"
                job_id = f"poll_{re.sub(r'[^0-9A-Za-z]+', '', str(server))}_{re.sub(r'[^0-9A-Za-z]+', '', str(payload.get('item_id') or title))}_{bucket}"
                self._schedule_once(job_id, lambda p=dict(payload): self._sync_item(p), f"轮询同步 {title} 到豆瓣", 3)
                scheduled += 1

        for direct in self._direct_server_configs():
            server = direct.get("name") or ""
            try:
                sessions = self._direct_emby_sessions(direct)
            except Exception as error:
                logger.debug(f"直连轮询媒体服务器 {server or '-'} 播放会话失败：{error}")
                continue
            for session in sessions:
                payload = self._payload_from_session(session, server)
                if not payload:
                    continue
                payload["direct_id"] = direct.get("id") or ""
                if not self._direct_payload_allowed(payload, direct):
                    continue
                bucket = "collect" if self._is_finished_event(payload) else "do"
                marker_key = f"direct|{server}|{payload.get('user_name')}|{payload.get('item_id')}|{bucket}"
                seen_now.add(marker_key)
                if poll_state.get(marker_key):
                    continue
                poll_state[marker_key] = datetime.now().isoformat(timespec="seconds")
                title = payload.get("title") or payload.get("item_id") or "媒体"
                job_id = f"directpoll_{re.sub(r'[^0-9A-Za-z]+', '', str(server))}_{re.sub(r'[^0-9A-Za-z]+', '', str(payload.get('item_id') or title))}_{bucket}"
                self._schedule_once(job_id, lambda p=dict(payload): self._sync_item(p), f"直连轮询同步 {title} 到豆瓣", 3)
                scheduled += 1

        # 控制状态大小：只保留最近 200 个标记。
        if len(poll_state) > 200:
            items = list(poll_state.items())[-200:]
            poll_state = dict(items)
        self.save_data(_KEY_POLL, poll_state)
        if scheduled:
            logger.info(f"播放轮询发现 {scheduled} 条需要同步的记录")
        return {"success": True, "scheduled": scheduled}

    def _direct_account_lines(self) -> List[str]:
        """直连账号配置：每行 服务器名|地址|用户名|密码。"""
        return [line.strip() for line in re.split(r"[\n]+", self._direct_accounts or "") if line.strip()]

    def _direct_account_configs(self) -> List[Dict[str, str]]:
        result: List[Dict[str, str]] = []
        for line in self._direct_account_lines():
            parts = [item.strip() for item in line.split("|")]
            if len(parts) < 4:
                parts = [item.strip() for item in line.split(",")]
            if len(parts) < 4:
                continue
            result.append({"name": parts[0], "url": parts[1].rstrip("/"),
                           "username": parts[2], "password": "|".join(parts[3:]) if "|" in line else parts[3]})
        return result

    def _ensure_direct_tokens(self, config: Optional[dict] = None) -> None:
        """保存配置后自动用账号密码换取 token / userId，并写入 direct_servers。"""
        accounts = self._direct_account_configs()
        if not accounts:
            return
        existing = {item.get("name"): item for item in self._direct_server_configs()}
        changed = False
        for account in accounts:
            name = account.get("name") or ""
            if not name:
                continue
            current = existing.get(name) or {}
            if current.get("token") and current.get("user_id") and current.get("url") == account.get("url"):
                continue
            auth = self._direct_emby_authenticate(account)
            if not auth:
                logger.warn(f"直连服务器 {name} 登录失败，无法自动获取 token / userId")
                continue
            existing[name] = {**account, **auth}
            changed = True

        if not changed:
            return
        lines = []
        for name, item in existing.items():
            if not item.get("url") or not item.get("token"):
                continue
            user_id = item.get("user_id") or ""
            username = item.get("username") or ""
            # 格式：name|url|token|userId|username
            lines.append(f"{name}|{item.get('url')}|{item.get('token')}|{user_id}|{username}")
        self._direct_servers = "\n".join(lines)
        if config is not None:
            config["direct_servers"] = self._direct_servers
            # 直连账号里的用户名自动加入媒体用户名，避免用户重复维护。
            users = self._target_users()
            for account in accounts:
                username = account.get("username") or ""
                if username and username not in users:
                    users.append(username)
            config["users"] = ",".join(users)
            self._users = config["users"]
            self.update_config(config)

    def _direct_emby_authenticate(self, account: Dict[str, str]) -> Optional[Dict[str, str]]:
        base = (account.get("url") or "").rstrip("/")
        username = account.get("username") or ""
        password = account.get("password") or ""
        if not base or not username or not password:
            return None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Origin": base,
            "Referer": base + "/web/index.html",
            "X-Emby-Authorization": 'MediaBrowser Client="Emby Web", Device="MoviePilot DoubanWatchSync", DeviceId="doubanwatchsync-' + re.sub(r"[^0-9A-Za-z]+", "", account.get("name") or username) + '", Version="1.0.0"',
        }
        body = json.dumps({"Username": username, "Pw": password}).encode()
        for path in ("/emby/Users/AuthenticateByName", "/Users/AuthenticateByName"):
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=headers, method="POST"),
                                            context=ssl.create_default_context(), timeout=20) as response:
                    data = json.loads(response.read().decode("utf-8", "replace"))
                token = str(data.get("AccessToken") or "")
                user = data.get("User") if isinstance(data.get("User"), dict) else {}
                user_id = str(user.get("Id") or "")
                if token:
                    logger.info(f"直连服务器 {account.get('name')} 已自动获取 token / userId")
                    return {"token": token, "user_id": user_id, "username": str(user.get("Name") or username)}
            except Exception as error:
                logger.debug(f"直连服务器 {account.get('name')} 登录接口 {path} 失败：{error}")
        return None

    def _direct_server_status_text(self) -> str:
        """生成配置页直连服务器状态说明。"""
        accounts = self._direct_account_configs()
        if not accounts:
            return "未配置直连 Emby 服务器。普通 MP 媒体服务器只需填写媒体用户名即可。"
        direct = {item.get("name"): item for item in self._direct_server_configs()}
        lines: List[str] = []
        for account in accounts:
            name = account.get("name") or "未命名"
            item = direct.get(name) or {}
            if item.get("token") and item.get("user_id"):
                username = item.get("username") or account.get("username") or "-"
                lines.append(f"✅ {name}：账号正常（{username}）")
            elif item.get("token"):
                username = item.get("username") or account.get("username") or "-"
                lines.append(f"⚠️ {name}：已获取 Token，但未获取到 UserId（{username}）")
            else:
                lines.append(f"❌ {name}：无法获取 Token 或 UserId，请检查地址/用户名/密码/反代限制")
        return "\n".join(lines)

    def _selected_direct_poll_servers(self) -> List[str]:
        """解析需要启用直连播放轮询的服务器名。留空表示所有直连配置都参与轮询。"""
        value = self._direct_poll_servers
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,\n]+", value) if item.strip()]
        return []

    def _direct_poll_server_items(self) -> List[Dict[str, str]]:
        """生成直连轮询服务器多选项。优先来自直连配置，辅以已选媒体服务器。"""
        names: List[str] = []
        for item in self._direct_server_configs():
            name = item.get("name") or ""
            if name and name not in names:
                names.append(name)
        for item in self._direct_account_configs():
            name = item.get("name") or ""
            if name and name not in names:
                names.append(name)
        for name in self._selected_servers():
            if name and name not in names:
                names.append(name)
        return [{"title": name, "value": name} for name in names]

    def _direct_server_configs_legacy(self) -> List[Dict[str, str]]:
        """解析直连媒体服务器配置。每行：服务器名|地址|Token。"""
        result: List[Dict[str, str]] = []
        text = (self._direct_servers or "").strip()
        if not text:
            return result
        for line in re.split(r"[\n]+", text):
            line = line.strip()
            if not line:
                continue
            parts = [item.strip() for item in line.split("|")]
            if len(parts) < 3:
                parts = [item.strip() for item in line.split(",")]
            if len(parts) < 3:
                continue
            result.append({"name": parts[0], "url": parts[1].rstrip("/"), "token": parts[2],
                           "user_id": parts[3] if len(parts) > 3 else "",
                           "username": parts[4] if len(parts) > 4 else ""})
        return result

    def _direct_server_configs(self) -> List[Dict[str, str]]:
        return self._managed_direct_configs()

    def _direct_emby_sessions_legacy(self, config: Dict[str, str]) -> List[Dict[str, Any]]:
        """绕过 MoviePilot 媒体服务器实例，直接用 Token 查询 Emby Sessions。"""
        base = (config.get("url") or "").rstrip("/")
        token = (config.get("token") or "").strip()
        if not base or not token:
            return []
        url = f"{base}/emby/Sessions"
        headers = {
            "X-Emby-Token": token,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Referer": f"{base}/web/index.html",
            "Origin": base,
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        context=ssl.create_default_context(), timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            logger.debug(f"直连 Emby Sessions 返回异常状态：{error.code}")
            return []
        except Exception as error:
            logger.debug(f"直连 Emby Sessions 失败：{error}")
            return []
        return [item for item in (data or []) if isinstance(item, dict)] if isinstance(data, list) else []

    def _payload_from_session(self, session: Dict[str, Any], server_name: str) -> Optional[Dict[str, Any]]:
        """把 Emby Sessions 条目转换成本插件内部 payload。"""
        item = session.get("NowPlayingItem") if isinstance(session, dict) else None
        if not isinstance(item, dict):
            return None
        item_id = str(item.get("Id") or "").strip()
        item_type = str(item.get("Type") or "").strip()
        if not item_id or item_type not in {"Movie", "Episode", "Series"}:
            return None

        user_name = str(session.get("UserName") or (session.get("User") or {}).get("Name") or "").strip()
        title = str(item.get("Name") or "").strip()
        season = self._to_int(item.get("ParentIndexNumber"), 0, 0)
        episode = self._to_int(item.get("IndexNumber"), 0, 0)
        media_type = "MOV" if item_type == "Movie" else "TV"
        if media_type == "TV":
            title = str(item.get("SeriesName") or item.get("Series") or title).strip()

        play_state = session.get("PlayState") if isinstance(session.get("PlayState"), dict) else {}
        pos = self._to_int(play_state.get("PositionTicks"), 0, 0)
        runtime = self._to_int(item.get("RunTimeTicks"), 0, 0)
        percentage = round(pos * 100 / runtime, 2) if pos and runtime else None

        return {
            "title": title,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "item_id": item_id,
            "server_name": server_name or "",
            "user_name": user_name,
            "image": "",
            "event": "PlaybackStop" if percentage is not None and percentage >= 90 else "PlaybackStart",
            "percentage": percentage,
        }

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """处理属于本插件的远程命令。"""
        data = getattr(event, "event_data", None) or {}
        action = data.get("action")
        if action == "douban_watch_retry":
            self.retry_pending()
        elif action == "douban_watch_rescan":
            self.rescan_archive()

    # ---------------- 同步主流程 ----------------

    def _sync_item_original(self, payload: Dict[str, Any]) -> None:
        """执行一次同步，失败写入待重试队列。"""
        title = payload.get("title") or ""
        media_type = payload.get("media_type") or ""
        season = self._to_int(payload.get("season"), 0, 0)
        episode = self._to_int(payload.get("episode"), 0, 0)
        server_name = str(payload.get("server_name") or "")
        key = self._archive_key(title, media_type, season, server_name)

        reader = self._reader(server_name, username=str(payload.get("user_name") or "") or None, user_id=self._user_id_for(str(payload.get("user_name") or "") or None, server_name))
        status = self._resolve_status(payload, reader, media_type, season, episode)

        with self._lock:
            archive = self.get_data(_KEY_ARCHIVE) or {}
            record = archive.get(key)
            # 已经是「看过」就不再重复写入，但仍在看时允许升级为看过
            if isinstance(record, dict) and record.get("status") == "collect" \
                    and status != "collect":
                logger.info(f"{self._display_title(title, season)} 已标记为看完，跳过")
                return

        subject_id = self._manual_subject(key) or payload.get("subject_id") or reader.get_douban_id(payload.get("item_id") or "")
        subject_name = title

        if not subject_id:
            subject_name, subject_id = self._search_subject(title, season, media_type)
        if not subject_id:
            logger.warn(f"{title} 未找到豆瓣条目，跳过本次同步")
            self._save_pending(key, payload, title, season, media_type, status)
            return

        client = DoubanClient(cookie=self._load_cookie())

        display = self._display_title(subject_name or title, season)
        if self._write_douban(client, "set_status", subject_id=subject_id, status=status, private=self._private):
            logger.info(f"{display} 同步到档案成功（{self._status_text(status)}）")
            # 海报优先用豆瓣官方竖版，取不到再退回媒体服务器的图
            image = client.get_subject_image(subject_id, media_type) or payload.get("image") or ""
            self._save_archive(key, payload, subject_name or title, subject_id,
                               season, media_type, status, display, image=image)
            self._drop_pending(key)
            return

        logger.error(f"{display} 同步到档案失败，加入待重试队列")
        self._save_pending(key, payload, subject_name or title, season, media_type, status, subject_id)

    def retry_pending(self) -> None:
        """重试待处理队列中的条目，成功后移出队列，并顺带补齐缺失的海报。"""
        if not self._enabled:
            return

        self.retry_management()

        # 仪表盘海报墙依赖封面图，历史缺图条目在这里慢慢补齐
        try:
            self._fill_missing_images()
        except Exception as error:
            logger.debug(f"补齐豆瓣海报失败：{error}")

        pending = dict(self.get_data(_KEY_PENDING) or {})
        if not pending:
            logger.info("豆瓣同步失败队列为空")
            return

        logger.info(f"开始重试豆瓣同步失败队列，共 {len(pending)} 条")
        client = DoubanClient(cookie=self._load_cookie())

        for key, record in pending.items():
            with self._get_record_lock(key):
                record = (self.get_data(_KEY_PENDING) or {}).get(key)
                if not isinstance(record, dict):
                    continue
                if self._manual_guard(key) or not self._direct_record_allowed(record):
                    continue
                if not isinstance(record, dict):
                    continue
                subject_id = self._manual_subject(key) or str(record.get("subject_id") or "")
                status = str(record.get("status") or "do")
                display = str(record.get("display") or key)
                if not subject_id:
                    # 之前没搜到条目，重新尝试搜索
                    subject_name, found = self._search_subject(
                        str(record.get("title") or key),
                        self._to_int(record.get("season"), 0, 0),
                        str(record.get("type") or "TV"),
                    )
                    if not found:
                        logger.warn(f"{display} 仍未找到豆瓣条目")
                        continue
                    subject_id, record["subject_id"] = found, found
                    record["subject_name"] = subject_name
                if self._write_douban(client, "set_status", subject_id=subject_id, status=status, private=self._private):
                    record["subject_id"] = subject_id
                    logger.info(f"{display} 重试同步成功")
                    self._move_pending_to_archive(key, record)
                else:
                    logger.warn(f"{display} 重试仍然失败")

    def rescan_archive(self) -> Dict[str, Any]:
        """
        重扫历史档案，按媒体服务器的真实播放状态校正豆瓣状态。

        背景：v1.3.0 之前插件只认开播事件，历史条目的状态普遍停留在「在看」，
        即使早已看完（如征途 S1E28 共 28 集）。本方法按条目在媒体服务器里的
        已播放标记重新判定，把看完的补标为「看过」。

        安全约束：
        - 只升级为「看过」，不把已是「看过」的降级，避免误伤；
        - 电影不做批量重扫（没有集号概念，直接看事件更准）；
        - 媒体服务器查不到的条目原样保留，不猜测。
        """
        summary = {"total": 0, "upgraded": 0, "skipped": 0, "failed": 0, "details": []}
        if not self._enabled:
            logger.warn("插件未启用，无法重扫档案")
            return summary

        with self._lock:
            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
        if not archive:
            logger.info("档案为空，无需重扫")
            return summary

        client = DoubanClient(cookie=self._load_cookie())

        summary["total"] = len(archive)
        logger.info(f"开始重扫豆瓣档案，共 {summary['total']} 条")

        updates: Dict[str, Dict[str, Any]] = {}
        trace: List[Dict[str, Any]] = []
        for key, record in archive.items():
            with self._get_record_lock(key):
                current_record = (self.get_data(_KEY_ARCHIVE) or {}).get(key)
                if not current_record or self._manual_guard(key) or not self._direct_record_allowed(current_record) or current_record.get("subject_id") != record.get("subject_id"):
                    summary["skipped"] += 1
                    continue
                if not isinstance(record, dict):
                    continue
                title = str(record.get("subject_name") or record.get("title") or key)
                step: Dict[str, Any] = {"title": title, "season": record.get("season")}
                trace.append(step)

                if record.get("status") == "collect":
                    step["result"] = "已是看过，跳过"
                    summary["skipped"] += 1
                    continue
                # 电影没有集号概念，不参与重扫
                if not str(record.get("type") or "").startswith("电视剧"):
                    step["result"] = "非剧集，跳过"
                    summary["skipped"] += 1
                    continue

                season = self._to_int(record.get("season"), 0, 0)
                subject_id = str(record.get("subject_id") or "")
                if not subject_id:
                    step["result"] = "档案缺少豆瓣 ID，跳过"
                    summary["skipped"] += 1
                    continue

                try:
                    server_name = self._record_server(record)
                    step["server_name"] = server_name or ""
                    if server_name is None:
                        step["result"] = "多服务器配置下旧档案缺少原服务器，跳过以免误判"
                        summary["skipped"] += 1
                        continue
                    reader = self._reader(server_name)
                    series_id = reader.search_series(title, season)
                    step["series_id"] = series_id or ""
                    if not series_id:
                        step["result"] = "媒体服务器未搜到该剧集"
                        logger.warn(f"{title} 在媒体服务器中未找到对应剧集，跳过")
                        summary["skipped"] += 1
                        continue

                    episodes = reader.get_season_episodes(series_id, season)
                    # 播放状态按用户隔离，这里汇总所有配置用户
                    state = self._play_state_of_any_user(series_id, season, server_name)
                    step["users"] = self._target_users()
                    step["user_ids"] = self._target_user_ids()
                    step["episodes_count"] = len(episodes)
                    step["played_total"] = len(state)
                    step["played_true"] = sum(1 for value in state.values() if value)
                    step["played_false"] = sum(1 for value in state.values() if not value)

                    if not state:
                        step["result"] = "未读取到任何集"
                        logger.warn(f"{title} 第{season}季未读取到任何集，跳过")
                        summary["skipped"] += 1
                        continue

                    played_true = sum(1 for value in state.values() if value)
                    if played_true < len(state):
                        # 把未看完的集号记下来，便于判断是哪几集没看
                        unfinished = [index for index, value in sorted(state.items()) if not value]
                        step["unfinished"] = unfinished[:20]
                        step["result"] = f"未看完 {len(unfinished)} 集，保持在看"
                        summary["skipped"] += 1
                        continue

                    step["result"] = "整季已看完，尝试写入豆瓣"
                except Exception as error:
                    step["result"] = f"读取异常：{type(error).__name__}: {error}"
                    logger.warn(f"重扫 {title} 读取播放状态失败：{error}")
                    summary["failed"] += 1
                    continue

                if self._write_douban(client, "set_status", subject_id=subject_id, status="collect", private=self._private):
                    step["result"] = "写入豆瓣成功，已升级为看过"
                    logger.info(f"{title} 第{season}季已看完，重扫后标记为看过")
                    updates[key] = record
                    summary["upgraded"] += 1
                    summary["details"].append(title)
                else:
                    step["result"] = "写入豆瓣失败"
                    logger.warn(f"{title} 重扫后写入豆瓣失败")
                    summary["failed"] += 1

        summary["trace"] = trace
        self.save_data(_KEY_DIAG, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {k: v for k, v in summary.items() if k != "trace"},
            "trace": trace,
        })
        # 任务已执行，主动清掉配置里的开关，避免下次启用插件时重复触发
        try:
            current = self.get_config() or {}
            if isinstance(current, dict) and current.get("rescan"):
                current["rescan"] = False
                self.update_config(current)
        except Exception as error:
            logger.debug(f"清除重扫开关失败：{error}")

        if updates:
            with self._lock:
                current = dict(self.get_data(_KEY_ARCHIVE) or {})
                for key in updates:
                    if key not in current or self._manual_guard(key) or current[key].get("subject_id") != updates[key].get("subject_id"):
                        continue
                    current[key]["status"] = "collect"
                    current[key]["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.save_data(_KEY_ARCHIVE, current)
            logger.info(f"重扫完成：{summary['upgraded']} 条升级为看过")
        else:
            logger.info("重扫完成：没有需要升级的条目")

        return summary

    # ---------------- 数据读写 ----------------

    def _save_archive(self, key: str, payload: Dict[str, Any], subject_name: str, subject_id: str,
                      season: int, media_type: str, status: str, display: str,
                      image: str = "") -> None:
        """写入已同步档案。"""
        with self._lock:
            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            archive[key] = {
                "title": display,
                "subject_name": subject_name,
                "subject_id": subject_id,
                "season": season,
                "episode": self._to_int(payload.get("episode"), 0, 0),
                "type": "电视剧" if media_type == "TV" else "电影",
                "status": status,
                "image": image or payload.get("image") or "",
                "server_name": payload.get("server_name") or "",
                "user_name": payload.get("user_name") or "",
                "item_id": payload.get("item_id") or "",
                "source_title": payload.get("title") or display,
                "direct_id": payload.get("direct_id") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_ARCHIVE, archive)

    def _fill_missing_images(self, limit: int = 5) -> None:
        """补齐档案里缺失的海报，单次最多处理 limit 条，失败不影响其它逻辑。"""
        with self._lock:
            snapshot = dict(self.get_data(_KEY_ARCHIVE) or {})
            todo = [key for key, item in snapshot.items()
                    if isinstance(item, dict) and item.get("subject_id") and not item.get("image")][:limit]
            if not todo:
                return

        client = DoubanClient(cookie=self._load_cookie())
        filled: Dict[str, str] = {}
        for key in todo:
            item = snapshot.get(key) or {}
            media_type = "MOV" if str(item.get("type") or "").startswith("电影") else "TV"
            image = client.get_subject_image(str(item.get("subject_id") or ""), media_type)
            if image:
                filled[key] = image

        if not filled:
            return
        with self._lock:
            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            for key, image in filled.items():
                if key in archive and archive[key].get("subject_id") == snapshot[key].get("subject_id") and not archive[key].get("image"):
                    archive[key]["image"] = image
            self.save_data(_KEY_ARCHIVE, archive)
        logger.info(f"豆瓣档案补齐海报 {len(filled)} 条")

    def _save_pending(self, key: str, payload: Dict[str, Any], subject_name: str, season: int,
                      media_type: str, status: str, subject_id: str = "") -> None:
        """写入待重试队列。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            pending[key] = {
                "title": self._display_title(subject_name, season),
                "display": self._display_title(subject_name, season),
                "subject_name": subject_name,
                "subject_id": subject_id,
                "season": season,
                "episode": self._to_int(payload.get("episode"), 0, 0),
                "type": media_type,
                "status": status,
                "image": payload.get("image") or "",
                "server_name": payload.get("server_name") or "",
                "item_id": payload.get("item_id") or "",
                "user_name": payload.get("user_name") or "",
                "source_title": payload.get("title") or subject_name,
                "direct_id": payload.get("direct_id") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_PENDING, pending)

    def _move_pending_to_archive(self, key: str, record: Dict[str, Any]) -> None:
        """条目重试成功后，从队列移出并写入档案。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            pending.pop(key, None)
            self.save_data(_KEY_PENDING, pending)

            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            archive[key] = {
                "title": record.get("display") or key,
                "subject_name": record.get("subject_name") or "",
                "subject_id": record.get("subject_id") or "",
                "season": record.get("season") or 0,
                "episode": record.get("episode") or 0,
                "type": "电视剧" if record.get("type") == "TV" else "电影",
                "status": record.get("status") or "do",
                "image": record.get("image") or "",
                "server_name": record.get("server_name") or "",
                "user_name": record.get("user_name") or "",
                "item_id": record.get("item_id") or "",
                "source_title": record.get("source_title") or record.get("title") or "",
                "direct_id": record.get("direct_id") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_ARCHIVE, archive)

    def _drop_pending(self, key: str) -> None:
        """同步成功后清理同名待处理记录。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            if key in pending:
                pending.pop(key)
                self.save_data(_KEY_PENDING, pending)

    def query_archive(self) -> Dict[str, Any]:
        """返回当前档案与失败队列，供插件页面和外部调用使用。"""
        return {
            "archive": self.get_data(_KEY_ARCHIVE) or {},
            "pending": self.get_data(_KEY_PENDING) or {},
            "enabled": self._enabled,
        }

    # ---------------- 辅助能力 ----------------

    def _search_subject(self, title: str, season: int, media_type: str) -> Tuple[Optional[str], Optional[str]]:
        """
        按标题搜索豆瓣条目。
        先尝试带季数的标题（部分剧集按季单独建条目），再退回主标题。
        """
        client = DoubanClient()
        candidates = []
        season_title = self._display_title(title, season)
        if season_title != title:
            candidates.append(season_title)
        candidates.append(title)

        for candidate in candidates:
            subject_name, subject_id = client.search(candidate, media_type)
            if subject_id:
                return subject_name, subject_id
        return None, None

    def _resolve_status(self, payload: Dict[str, Any], reader: MediaServerReader,
                        media_type: str, season: int, episode: int) -> str:
        """
        判断应写入的状态：看完→collect，在看→do。

        判定顺序：
        1. 电影：播放结束事件直接算看过，开播事件先记在看；
        2. 剧集：读媒体服务器里该集的真实已播放标记，为真即看过；
        3. 已播放标记取不到时，回退到「是否本季末集」判断；
        4. 仍取不到（正在更新的剧集、集数元数据缺失），保守记为在看。
        """
        if media_type != "TV":
            return "collect" if self._is_finished_event(payload) else "do"

        item_id = str(payload.get("item_id") or "")
        series_id = reader.get_series_id(item_id)
        episode_no = self._to_int(episode, 0, 0)

        if series_id and episode_no:
            # 播放状态按用户隔离，汇总所有配置用户：任一看完即算看完
            played = self._is_played_by_any_user(series_id, season, episode_no, str(payload.get("server_name") or ""), str(payload.get("user_name") or "") or None)
            if played is True:
                return "collect"
            if played is False:
                # 媒体服务器明确记录了「未看完」，且不是末集，直接判定在看
                return "do"

        if series_id and episode_no and self._is_last_episode(reader, series_id, season, episode_no):
            return "collect"

        logger.debug(
            f"{payload.get('title')} S{season}E{episode_no} 未能确认为末集，"
            f"按在看处理")
        return "do"

    @staticmethod
    def _is_last_episode(reader: MediaServerReader, series_id: str,
                         season: int, episode: int) -> bool:
        """
        判断是否为该季最后一集。
        集号列表来自 Shows/Id/Episodes 的真实结果，不依赖 Season 元数据。

        必须要求该集确实存在于列表中：正在更新的剧集里，用户播放的集号
        可能大于当前已收录的最大集号（如只更到 4 集却看了第 5 集），
        这种情况属于「在看」而非「看完」，不能按大于等于处理。
        """
        episodes = reader.get_season_episodes(series_id, season)
        if not episodes or episode not in episodes:
            return False
        return episode == max(episodes)

    @staticmethod
    def _is_finished_event(payload: Dict[str, Any]) -> bool:
        """
        判断事件是否代表「已看完」。
        播完、暂停退出、标记已播放都算；开播不算。
        """
        event = str(payload.get("event") or "")
        if event in _PLAY_STOP or event in _PLAYED:
            return True
        # 播放进度超过 90% 视为看完，兜底部分客户端不回传结束事件的情况
        percentage = payload.get("percentage")
        try:
            return percentage is not None and float(percentage) >= 90
        except (TypeError, ValueError):
            return False

    def _load_cookie(self) -> str:
        """只读取插件本地 Cookie；失效后在实际写入流程中读取 CookieCloud。"""
        current = str(self._auth_record().get("cookie") or "")
        if current:
            return current
        saved = self.get_data("douban_login") or {}
        if saved and saved.get("credential_key") != self._credential_key:
            return ""  # 账号改变后不复用原账号遗留的手填 Cookie。
        return self._cookie.strip()

    def _load_cookiecloud(self) -> str:
        """复用宿主下载、解密和域名归并；关闭开关时不读取。"""
        if not self._use_cookiecloud:
            return ""
        cookies, error = self._download_cookiecloud_cookies()
        if error:
            logger.warn(f"从 CookieCloud 获取 cookie 失败：{error}")
        cookie = self._pick_douban_cookie(cookies or {})
        if cookie:
            logger.info("已从 CookieCloud 获取豆瓣 cookie")
        return cookie

    def _download_cookiecloud_cookies(self) -> Tuple[Optional[Dict[str, str]], str]:
        """通过 MoviePilot 内置兼容层下载 CookieCloud Cookie。"""
        try:
            from app.adapters.external.cookiecloud import CookieCloudHelper
        except Exception as error:  # pragma: no cover - 仅兼容异常宿主
            return None, f"CookieCloudHelper 不可用：{type(error).__name__}"

        try:
            plugin = self

            class ConfiguredCookieCloudHelper(CookieCloudHelper):
                def _CookieCloudHelper__sync_setting(self):
                    super()._CookieCloudHelper__sync_setting()
                    if plugin._cc_host.strip():
                        from app.foundation.url import UrlUtils
                        self._server = UrlUtils.standardize_base_url(plugin._cc_host.strip())
                        self._enable_local = False
                    if plugin._cc_key.strip():
                        self._key = plugin._cc_key.strip()
                    if plugin._cc_password.strip():
                        self._password = plugin._cc_password.strip()

            helper = ConfiguredCookieCloudHelper()
            # 兼容旧版本配置里保存的独立 CookieCloud 参数。正常情况下应留空，
            # 让 helper 使用 MoviePilot 全局配置（含 COOKIECLOUD_ENABLE_LOCAL）。
            if self._cc_host.strip():
                try:
                    from app.foundation.url import UrlUtils
                    helper._server = UrlUtils.standardize_base_url(self._cc_host.strip())
                except Exception:
                    helper._server = self._cc_host.strip().rstrip("/")
                helper._enable_local = False
            if self._cc_key.strip():
                helper._key = self._cc_key.strip()
            if self._cc_password.strip():
                helper._password = self._cc_password.strip()
            cookies, error = helper.download()
            if error:
                return cookies, "CookieCloud 下载或解密失败，请检查本地服务或连接配置"
            return cookies, ""
        except Exception as error:
            return None, f"CookieCloud 读取异常：{type(error).__name__}"

    @staticmethod
    def _pick_douban_cookie(cookies: Dict[str, str]) -> str:
        """从已归并的 CookieCloud 数据中选择豆瓣 Cookie。"""
        if not cookies:
            return ""
        for domain in ("douban.com", "movie.douban.com", "www.douban.com", "accounts.douban.com"):
            cookie = cookies.get(domain) or cookies.get(f".{domain}")
            if cookie:
                return str(cookie)
        for domain, cookie in cookies.items():
            if (str(domain).lstrip(".") == "douban.com" or str(domain).endswith(".douban.com")) and cookie:
                return str(cookie)
        return ""

    def _schedule_once(self, job_id: str, func: Any, name: str, delay: int) -> None:
        """把任务交给宿主的延后任务队列，不占用事件线程。"""
        adder = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if not adder:
            threading.Thread(target=func, daemon=True).start()
            return
        try:
            adder(self.__class__.__name__, job_id, func, name, delay_seconds=delay)
        except Exception as error:
            logger.warn(f"登记延后任务失败，改为立即执行：{error}")
            threading.Thread(target=func, daemon=True).start()

    def _remove_once(self, job_id: str) -> None:
        """移除尚未执行的延后任务。"""
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if not remover:
            return
        try:
            remover(self.__class__.__name__, job_id)
        except Exception as error:
            logger.debug(f"移除延后任务失败：{error}")

    def _target_users(self) -> List[str]:
        """返回配置中允许触发同步的用户名列表。"""
        return [item.strip() for item in self._users.split(",") if item.strip()]

    def _target_user_ids(self) -> List[str]:
        """解析配置里的媒体服务器用户 ID 列表。

        支持三种写法：
        1. 纯 ID：多个时按「媒体用户名」顺序对应，兼容 1.1.1；
        2. 服务器名=用户ID / 服务器名:用户ID：指定某个服务器使用的用户 ID；
        3. 用户名@服务器名=用户ID 或 服务器名/用户名=用户ID：多用户、多服务器时精确匹配。
        """
        return [item.strip() for item in re.split(r"[,\n]+", self._user_ids) if item.strip()]

    @staticmethod
    def _split_user_id_entry(entry: str) -> Tuple[str, str]:
        """把单个用户 ID 配置拆成 (匹配键, userId)。纯 ID 返回 ("", entry)。"""
        text = (entry or "").strip()
        if not text:
            return "", ""
        if "=" in text:
            key, value = text.split("=", 1)
            return key.strip(), value.strip()
        if ":" in text and not text.lower().startswith(("http://", "https://")):
            key, value = text.rsplit(":", 1)
            # Emby/Jellyfin UserId 通常不含冒号；若右侧为空则按纯 ID 处理
            if value.strip():
                return key.strip(), value.strip()
        return "", text

    def _user_id_for(self, username: Optional[str], server_name: Optional[str] = None) -> Optional[str]:
        """按服务器名/用户名匹配配置的用户 ID；普通用户 token 无权列 /Users 时使用。"""
        entries = self._target_user_ids()
        if not entries:
            return None

        name = (username or "").strip()
        server = (server_name or "").strip()
        users = self._target_users()
        plain_ids: List[str] = []

        for entry in entries:
            key, value = self._split_user_id_entry(entry)
            if not value:
                continue
            if not key:
                plain_ids.append(value)
                continue

            # 支持：服务器名=ID / 服务器名:ID
            if server and key == server:
                return value
            # 支持：用户名=ID（多服务器同 ID 或只关心用户名时）
            if name and key == name:
                return value
            # 支持：用户名@服务器名=ID
            if name and server and key == f"{name}@{server}":
                return value
            # 支持：服务器名/用户名=ID 或 服务器名|用户名=ID
            if name and server and key in (f"{server}/{name}", f"{server}|{name}"):
                return value

        # 兼容旧写法：纯 ID 按媒体用户名顺序对应。
        if plain_ids:
            if name and users:
                for index, item in enumerate(users):
                    if item == name and index < len(plain_ids):
                        return plain_ids[index]
            return plain_ids[0] if len(plain_ids) == 1 else None
        return None

    def _is_target_user(self, user_name: Optional[str]) -> bool:
        """判断事件用户是否在配置的媒体库用户名列表中。"""
        allowed = self._target_users()
        if not allowed:
            return False
        return (user_name or "") in allowed

    def _play_state_of_any_user(self, series_id: str, season: int,
                                server_name: Optional[str] = None) -> Dict[int, bool]:
        """
        汇总所有配置用户的播放状态，同一集只要有人看完就算看完。

        播放状态按用户隔离，多个账号各自维护进度。合并时取「或」，
        避免因为某个账号没看而漏判成未看完。
        未配置用户名时退回单用户查询（使用宿主默认账号）。
        """
        direct = next((item for item in self._direct_server_configs() if item.get("name") == server_name), None)
        if direct:
            return self._reader(server_name, username=direct.get("username"), user_id=direct.get("user_id")).get_season_play_state(series_id, season)
        users = self._target_users()
        if not users:
            return self._reader(server_name).get_season_play_state(series_id, season)

        merged: Dict[int, bool] = {}
        for username in users:
            try:
                state = self._reader(server_name, username=username, user_id=self._user_id_for(username, server_name)).get_season_play_state(series_id, season)
            except Exception as error:
                logger.debug(f"读取用户 {username} 的播放状态失败：{error}")
                continue
            for index, played in state.items():
                merged[index] = merged.get(index, False) or played
        return merged

    def _is_played_by_any_user(self, series_id: str, season: int, episode: int,
                               server_name: Optional[str] = None,
                               event_user: Optional[str] = None) -> Optional[bool]:
        """任一配置用户已看完该集即返回 True；全部明确未看完返回 False；查不到返回 None。"""
        users = [event_user.strip()] if event_user and event_user.strip() else self._target_users()
        if not users:
            return self._reader(server_name).is_episode_played(series_id, season, episode)

        seen = False
        for username in users:
            try:
                played = self._reader(server_name, username=username, user_id=self._user_id_for(username, server_name)).is_episode_played(series_id, season, episode)
            except Exception as error:
                logger.debug(f"读取用户 {username} 的单集播放状态失败：{error}")
                continue
            if played is None:
                continue
            if played:
                return True
            seen = True
        return False if seen else None

    def _is_sync_event(self, info: WebhookEventInfo) -> bool:
        """
        判断是否为需要处理的播放或标记已观看事件。
        事件的 event 字段是宿主从媒体服务器原始报文透传过来的，
        Emby 用 PlaybackStart / PlaybackStop / UserDataSaved，
        Jellyfin 用 PlaybackStart / PlaybackStop 与 UserDataSaved(SaveReason)。
        """
        event = info.event or ""
        if event in _PLAY_START or event in _PLAY_STOP or event in _PLAYED:
            # UserDataSaved 也会被收藏等操作触发，用 SaveReason 收窄到手動标记已播放
            if event == "UserDataSaved" and info.save_reason \
                    and info.save_reason != "TogglePlayed":
                return False
            return True
        return False

    def _is_allowed_path(self, item_path: Optional[str]) -> bool:
        """按路径关键词过滤不需要同步的媒体。"""
        keywords = [item.strip() for item in self._exclude.split(",") if item.strip()]
        if not keywords:
            return True
        path = item_path or ""
        if not path:
            return True
        return not any(keyword in path for keyword in keywords)

    def _reader(self, event_server_name: Optional[str] = None,
                username: Optional[str] = None,
                user_id: Optional[str] = None) -> MediaServerReader:
        """
        构造媒体服务器读取器。

        宿主的 webhook 事件不带服务器名（server_name 字段始终为空），
        因此优先使用配置里指定的服务器；配置为空时，
        若系统里只有一个 Emby/Jellyfin 就用它，多个则回退到第一个并告警。

        播放状态按用户隔离，username 决定查询哪个账号的观看记录；
        为空时读取器会退回宿主默认（管理员），可能读不到真实进度。
        """
        name = (event_server_name or "").strip()
        if not name:
            selected_servers = self._selected_servers()
            if len(selected_servers) == 1:
                name = selected_servers[0]
        if not name:
            try:
                servers = [
                    service for service in MediaServerHelper().iterate_module_instances()
                    if (service.type or "").lower() in ("emby", "jellyfin") and service.instance
                ]
            except Exception:
                servers = []
            if len(servers) > 1:
                names = "、".join(sorted(service.name for service in servers))
                logger.warn(
                    f"检测到多个媒体服务器（{names}），已回退到「{servers[0].name}」。"
                    f"如需指定请在插件配置里选择媒体服务器。")
            if servers:
                name = servers[0].name
        direct_config = None
        for item in self._direct_server_configs():
            if name and item.get("name") == name:
                direct_config = item
                if not user_id:
                    user_id = item.get("user_id") or None
                if not username:
                    username = item.get("username") or None
                break
        return MediaServerReader(server_name=name or None, username=username or None, user_id=user_id or None,
                                 direct_config=direct_config)

    def _selected_libraries(self) -> List[str]:
        """返回配置中选中的媒体库名列表，兼容数组与逗号分隔字符串两种存储。"""
        raw = self._libraries
        if isinstance(raw, (list, tuple, set)):
            values = [str(item).strip() for item in raw]
        else:
            values = [item.strip() for item in str(raw or "").split(",")]
        return [item for item in values if item]

    def _selected_servers(self) -> List[str]:
        """返回配置中选中的媒体服务器名，兼容旧版单选字符串。"""
        raw = self._server
        if isinstance(raw, (list, tuple, set)):
            values = [str(item).strip() for item in raw]
        else:
            values = [item.strip() for item in str(raw or "").split(",")]
        result: List[str] = []
        for item in values:
            if item and item not in result:
                result.append(item)
        return result

    def _event_server_name(self, info: WebhookEventInfo) -> Optional[str]:
        """
        按配置判断事件来自哪个媒体服务器。

        返回值含义：
        - 字符串：明确使用该服务器；
        - 空字符串：未限制服务器，后续读取器按单服务器/回退规则定位；
        - None：事件服务器不在所选范围内，应忽略。
        """
        selected = self._selected_servers()
        event_server = str(getattr(info, "server_name", None) or "").strip()
        if not selected:
            return event_server
        if event_server:
            return event_server if event_server in selected else None
        if len(selected) == 1:
            return selected[0]
        logger.warn("收到未携带 server_name 的播放事件，但插件选择了多个媒体服务器；已忽略以避免同步到错误服务器")
        return None

    def _record_server(self, record: Dict[str, Any]) -> Optional[str]:
        """读取档案记录的原服务器；多服务器配置下旧记录缺失服务器时拒绝猜测。"""
        server = str(record.get("server_name") or "").strip()
        if server:
            return server
        selected = self._selected_servers()
        if len(selected) <= 1:
            return selected[0] if selected else ""
        return None

    def _server_items(self) -> List[Dict[str, Any]]:
        """生成媒体服务器下拉选项，值为配置中的服务器名称。"""
        items: List[Dict[str, Any]] = []
        try:
            for service in MediaServerHelper().iterate_module_instances():
                if not service.instance or not service.name:
                    continue
                if (service.type or "").lower() not in ("emby", "jellyfin"):
                    continue
                items.append({"title": service.name, "value": service.name})
        except Exception as error:
            logger.debug(f"读取媒体服务器列表失败：{error}")
        return items

    def _library_items(self) -> List[Dict[str, Any]]:
        """生成 VSelect 的选项，值为媒体库名，标题带上类型便于区分同名库。"""
        items: List[Dict[str, Any]] = []
        try:
            libraries = self._available_libraries()
        except Exception as error:
            logger.debug(f"读取媒体库选项失败：{error}")
            libraries = []
        for library in libraries:
            title = library["name"]
            if library.get("type"):
                title = f"{title}（{library['type']}）"
            items.append({"title": title, "value": library["name"]})
        return items

    def _available_libraries(self) -> List[Dict[str, Any]]:
        """
        读取媒体服务器实际存在的媒体库，供配置页渲染下拉选项。
        读取失败时返回空列表，此时配置页只提示手工填写。
        """
        try:
            servers = self._selected_servers()
            if not servers:
                servers = [item["value"] for item in self._server_items() if item.get("value")]
            libraries: List[Dict[str, Any]] = []
            seen = set()
            for server in servers or [None]:
                for library in self._reader(server).get_librarys():
                    item = dict(library)
                    item["server_name"] = server or ""
                    marker = (item.get("server_name"), item.get("id"), item.get("name"), item.get("type"))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    libraries.append(item)
            return libraries
        except Exception as error:
            logger.warn(f"获取媒体库列表失败：{error}")
            return []

    def _is_allowed_library(self, info: WebhookEventInfo) -> bool:
        """
        按媒体库过滤事件。
        未选择任何库时放行；选择了库时判定条目归属的库是否在所选范围内。

        判定方式：先沿 ParentId 溯源到媒体库 ID 再比对；取不到时回退到
        物理路径前缀比对。两条路都取不到证据时保守放行，避免误杀正常条目。
        """
        selected = self._selected_libraries()
        if not selected:
            return True

        try:
            server_name = self._event_server_name(info)
            if server_name is None:
                return False
            reader = self._reader(server_name)
            libraries = reader.get_librarys()
        except Exception as error:
            logger.debug(f"按媒体库过滤时读取库列表失败：{error}")
            return True

        if not libraries:
            return True

        # 库名 → 库 ID，允许同名库（跨服务器）同时命中
        selected_ids = {lib["id"] for lib in libraries
                        if lib["name"] in selected and lib.get("id")}

        item_id = str(info.item_id or "")
        if item_id and selected_ids:
            try:
                library_id = reader.get_item_library_id(item_id)
            except Exception as error:
                logger.debug(f"溯源条目所属媒体库失败：{error}")
                library_id = None
            if library_id:
                if library_id in selected_ids:
                    return True
                logger.debug(f"条目 {item_id} 属于未选中的媒体库，忽略")
                return False

        # 溯源失败时回退到路径前缀比对
        item_path = info.item_path or ""
        if not item_path and item_id:
            try:
                item_path = reader.get_item_path(item_id) or ""
            except Exception:
                item_path = ""
        if not item_path:
            return True

        normalized = item_path.replace("\\", "/").rstrip("/").lower()
        for library in libraries:
            if library["name"] not in selected:
                continue
            for path in library["paths"]:
                prefix = path.replace("\\", "/").rstrip("/").lower()
                if prefix and (normalized == prefix or normalized.startswith(f"{prefix}/")):
                    return True
        logger.debug(f"条目 {item_id or item_path or '-'} 未命中所选媒体库，忽略")
        return False

    def _parse_media(self, info: WebhookEventInfo) -> Tuple[str, str, int, int]:
        """
        从事件解析标题、类型、季和集。
        :return: (标题, 类型 TV/MOV, 季, 集)，无法识别时类型为空字符串
        """
        raw = (info.item_name or "").strip()
        title = re.split(r"\s*[-–]\s*S\d|\s+S\d", raw)[0].strip() or raw
        # 去掉结尾的年份，例如「功夫女足 (2026)」
        title = re.sub(r"\s*[（(]\s*(19|20)\d{2}\s*[）)]\s*$", "", title).strip() or raw
        season = self._to_int(info.season_id, 0, 0)
        episode = self._to_int(info.episode_id, 0, 0)

        item_type = (info.item_type or "").upper()
        if item_type.startswith("TV") or season or episode:
            if item_type.startswith("MOV") and not season:
                return title, "MOV", season, episode
            return title, "TV", season, episode
        if item_type.startswith("MOV"):
            return title, "MOV", season, episode
        return title, "", season, episode

    def _archive_key(self, title: str, media_type: str, season: int,
                     server_name: Optional[str] = None) -> str:
        """生成档案键。跨服务器同名条目按服务器分开保存；旧档案键保持兼容。"""
        base = f"{title}_S{season}" if media_type == "TV" and season else title
        server = str(server_name or "").strip()
        return f"{base}@{server}" if server else base

    @staticmethod
    def _display_title(title: str, season: int) -> str:
        """按季生成展示标题。"""
        if season and season > 1:
            return f"{title} 第{season}季"
        return title

    @staticmethod
    def _status_text(status: str) -> str:
        """把状态取值转换为中文说明。"""
        return {"collect": "看过", "do": "在看", "wish": "想看"}.get(status, status)

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int) -> int:
        """安全转换为整数并按最小值收敛。"""
        try:
            result = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(result, minimum)

    # ---------------- 页面 ----------------

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回配置页面与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "private", "label": "仅自己可见"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "skip_first", "label": "不标记第一集"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "users",
                                        "label": "媒体用户名（Emby/Jellyfin）",
                                        "placeholder": "如：ken,gken",
                                        "hint": "用于筛选 MP 媒体服务器的观看用户；多个用英文逗号分隔。直连账号在下方独立管理。",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "exclude",
                                        "label": "媒体路径排除关键词",
                                        "placeholder": "如：纪录片,演唱会",
                                        "hint": "路径包含这些关键词时跳过；多个用英文逗号分隔。",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "server",
                                        "label": "MoviePilot 媒体服务器",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "items": self._server_items(),
                                        "placeholder": "只选择 MP 已添加的服务器；直连服务器不要选在这里",
                                        "hint": "这里用于 MP 原生 Webhook/媒体服务器读取；直连 Emby 请在下方添加账号。",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "libraries",
                                        "label": "同步的媒体库",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "items": self._library_items(),
                                        "placeholder": "不选表示全部媒体库",
                                        "hint": "只同步选中媒体库的内容；直连服务器读取不到库列表时默认不过滤。",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "direct_accounts",
                                        "label": "直连 Emby 服务器账号（可选）",
                                        "placeholder": "每行一个：服务器名|Emby地址|用户名|密码\nserver-a|https://emby.example.com|user|password",
                                        "hint": "保存后自动获取 Token 和 UserId；成功的直连服务器会自动轮询播放状态，不需要添加到 MP，也不需要填写用户ID映射。",
                                        "persistent-hint": True,
                                        "rows": 3,
                                        "auto-grow": True,
                                        "clearable": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": self._direct_server_status_text(),
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12},
                             "content": [{"component": "VTextField", "props": {"model": "cookie", "label": "豆瓣cookie", "placeholder": "可留空，成功登录后由插件保存"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "use_cookiecloud", "label": "使用CookieCloud"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即重试失败队列"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4},
                             "content": [{"component": "VSwitch", "props": {"model": "rescan", "label": "重扫档案（按已播放状态校正）"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "pc_month", "label": "大屏显示月份数", "placeholder": "3"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "pc_num", "label": "大屏每月最多显示", "placeholder": "50"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "mobile_month", "label": "小屏显示月份数", "placeholder": "2"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "mobile_num", "label": "小屏每月最多显示", "placeholder": "15"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "inline_image", "label": "内联豆瓣图片（防盗链）"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "inline_limit", "label": "最多内联图片数", "placeholder": "20"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "普通 MP 媒体服务器：填写媒体用户名即可；直连 Emby：填写服务器名/地址/账号/密码，插件自动获取 Token/UserId 并轮询播放状态。同步失败会进入队列，每 30 分钟自动重试。",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow", "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSwitch", "props": {"model": "auto_login", "label": "Cookie 过期自动重新登录"}},
                            ]},
                        ],
                    },
                    {
                        "component": "VRow", "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "douban_username", "label": "豆瓣账号", "placeholder": "手机号或邮箱", "autocomplete": "username"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "douban_password", "label": "豆瓣密码", "type": "password", "autocomplete": "current-password"}},
                            ]},
                        ],
                    },

                ],
            }
        ], {
            "enabled": False,
            "private": True,
            "skip_first": True,
            "users": "",
            "user_ids": "",
            "direct_servers": "",
            "direct_accounts": "",
            "direct_poll_servers": [],
            "exclude": "",
            "cookie": "",
            "auto_login": False,
            "douban_username": "",
            "douban_password": "",
            "use_cookiecloud": True,
            "cookiecloud_host": "",
            "cookiecloud_key": "",
            "cookiecloud_password": "",
            "onlyonce": False,
            "rescan": False,
            "pc_month": 3,
            "pc_num": 50,
            "mobile_month": 2,
            "mobile_num": 15,
            "libraries": [],
            "server": [],
            "inline_image": True,
            "inline_limit": 20,
        }

    def get_page(self) -> List[Dict[str, Any]]:
        """返回详情页，展示已同步档案与失败队列概况。"""
        archive = self.get_data(_KEY_ARCHIVE) or {}
        pending = self.get_data(_KEY_PENDING) or {}
        recent = sorted(
            [item for item in archive.values() if isinstance(item, dict) and item.get("timestamp")],
            key=lambda item: item["timestamp"],
            reverse=True,
        )[:20]

        rows = []
        for item in recent:
            rows.append({
                "component": "VListItem",
                "props": {
                    "title": item.get("title", ""),
                    "subtitle": f"{item.get('timestamp', '')} · {self._status_text(item.get('status', ''))}",
                },
            })
        if not rows:
            rows = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无同步记录"}}]

        watched = sum(1 for item in archive.values()
                      if isinstance(item, dict) and item.get("status") == "collect")
        watching = sum(1 for item in archive.values()
                       if isinstance(item, dict) and item.get("status") == "do")

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {"component": "VCard", "props": {"variant": "tonal"},
                             "content": [{"component": "VCardText",
                                          "text": f"已同步：{len(archive)} 条"}]}
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {"component": "VCard", "props": {"variant": "tonal"},
                             "content": [{"component": "VCardText",
                                          "text": f"看过：{watched} · 在看：{watching}"}]}
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {"component": "VCard", "props": {"variant": "tonal"},
                             "content": [{"component": "VCardText",
                                          "text": f"待重试：{len(pending)} 条"}]}
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "VAlert", "props": {
                                "type": "info", "variant": "tonal",
                                "text": "若「在看」的条目其实早已看完（旧版本未捕获播完事件），"
                                        "可在插件配置勾选「重扫档案」按媒体服务器的真实播放状态校正；"
                                        "也可发送命令 /douban_watch_rescan。重扫按档案记录的原服务器/用户读取播放状态，只升级为「看过」，不会降级已有记录。"}}
                        ],
                    }
                ],
            },
            {"component": "VList", "content": rows},
        ]

    @staticmethod
    def get_dashboard_meta() -> Optional[List[Dict[str, str]]]:
        """声明仪表盘 key 与名称，不声明时宿主只能给出空 key，组件标题会显示异常。"""
        return [{"key": _KEY_DASHBOARD, "name": "豆瓣观看档案"}]

    def get_dashboard(self, key: str, **kwargs: Any) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]]:
        """返回仪表盘：按月分组的豆瓣档案海报墙。"""
        if key and key != _KEY_DASHBOARD:
            return None
        mobile = self._is_mobile(kwargs.get("user_agent"))
        archive = self.get_data(_KEY_ARCHIVE) or {}
        limit_month = max(1, self._mobile_month if mobile else self._pc_month)
        limit_num = max(1, self._mobile_num if mobile else self._pc_num)

        # 显式给出标题，避免宿主用插件名兜底导致标题不一致
        attrs: Dict[str, Any] = {
            "refresh": 600,
            "border": True,
            "title": "豆瓣观看同步",
            "subtitle": f"共 {len(archive)} 部",
        }
        empty = [
            {"component": "VAlert",
             "props": {"type": "info", "variant": "tonal",
                       "text": "还没有同步记录，在 Emby / Jellyfin 里看一集后会自动出现在这里。"}}
        ]

        sorted_items = sorted(
            [item for item in archive.values() if isinstance(item, dict) and item.get("timestamp")],
            key=lambda item: item["timestamp"],
            reverse=True,
        )
        if not sorted_items:
            return {"cols": 12, "md": 6}, attrs, empty

        # 按 (年, 月) 分组，跨年时月份不会串在一起
        groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for record in sorted_items:
            time_object = self._parse_time(record.get("timestamp"))
            if time_object is None:
                continue
            groups.setdefault((time_object.year, time_object.month), []).append(record)

        self._prepare_images()
        try:
            timeline_items: List[Dict[str, Any]] = []
            for year, month in sorted(groups.keys(), reverse=True)[:limit_month]:
                cards = [card for card in (self._build_card(item, mobile) for item in groups[(year, month)]) if card]
                if not cards:
                    continue
                timeline_items.append({
                    "component": "VTimelineItem",
                    "props": {"size": "x-small", "dot-color": "primary"},
                    "content": [
                        {
                            "component": "h1",
                            "props": {"class": "text-base",
                                      "style": "padding:0rem 0rem 0.5rem 0rem;font-weight:bold;"},
                            "html": f"{year}年{month}月 <span class='text-sm font-normal'>共 {len(cards)} 部</span>",
                        },
                        {
                            "component": "VRow",
                            "props": {"class": "pa-0 ma-0", "style": "padding:0rem;"},
                            "content": cards[:limit_num],
                        },
                    ],
                })

            if not timeline_items:
                return {"cols": 12, "md": 6}, attrs, empty

            return (
                {"cols": 12, "md": 8},
                attrs,
                [
                    {
                        # VTimelineItem 必须放在 VTimeline 里，否则时间线渲染不出来
                        "component": "VTimeline",
                        "props": {"density": "compact"},
                        "content": timeline_items,
                    }
                ],
            )
        finally:
            self._flush_images()

    # ---------------- 仪表盘辅助 ----------------

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        """解析档案里的时间戳，格式不对返回 None。"""
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def _prepare_images(self) -> None:
        """渲染前准备好图片缓存，并为本次渲染分配回源预算。"""
        if not self._image_map:
            self._image_map = dict(self.get_data(_KEY_IMAGES) or {})
        self._image_cache = self._image_map
        self._image_budget = _INLINE_BUDGET
        self._image_dirty = False

    def _flush_images(self) -> None:
        """渲染结束后把新增的 data URI 落盘，下次直接使用。"""
        if not self._image_dirty:
            return
        mapping = dict(self._image_map)
        # 超量时丢掉最早写入的一批，避免缓存无限增长
        if len(mapping) > _IMAGE_CACHE_MAX:
            for key in list(mapping.keys())[:len(mapping) - _IMAGE_CACHE_MAX]:
                mapping.pop(key, None)
        self.save_data(_KEY_IMAGES, mapping)
        self._image_dirty = False

    def _resolve_image(self, record: Dict[str, Any]) -> str:
        """
        返回可直接展示的图片地址。
        豆瓣图片有防盗链（带本站 Referer 会 403），因此由后端取回后内联为 data URI。
        """
        image = str(record.get("image") or "")
        if not image or "doubanio.com" not in image:
            return image

        cached = self._image_map.get(image)
        if cached:
            return cached
        if not self._inline_image or image in self._image_failed:
            return ""
        if self._image_budget <= 0:
            # 超出上限或本次预算用尽，先占位，后续刷新继续补齐
            return ""

        self._image_budget -= 1
        uri = DoubanClient().fetch_image_data_uri(image)
        if not uri:
            self._image_failed.add(image)
            return ""
        self._image_map[image] = uri
        self._image_dirty = True
        return uri

    def _build_card(self, record: Dict[str, Any], mobile: bool) -> Optional[Dict[str, Any]]:
        """把一条档案记录渲染成海报卡片；没有豆瓣条目就无处跳转，直接跳过。"""
        subject_id = str(record.get("subject_id") or "")
        if not subject_id:
            return None

        title = str(record.get("title") or record.get("subject_name") or "")
        status = self._status_text(str(record.get("status") or ""))
        width, height = ("44px", "66px") if mobile else ("66px", "99px")
        image = self._resolve_image(record)

        if image:
            body = [
                {
                    "component": "VImg",
                    "props": {
                        "src": image,
                        "cover": True,
                        "aspect-ratio": "2/3",
                        "style": f"width:{width}; height:{height};",
                    },
                }
            ]
        else:
            # 缺封面时用文字占位，保证这条记录不会从海报墙上凭空消失
            body = [
                {
                    "component": "VCardText",
                    "props": {
                        "class": "text-caption text-center pa-1",
                        "style": f"width:{width}; height:{height};display:flex;"
                                 f"align-items:center;justify-content:center;",
                        "text": title[:4] or "豆瓣",
                    },
                }
            ]

        return {
            "component": "a",
            "props": {
                "href": f"https://movie.douban.com/subject/{subject_id}/",
                "target": "_blank",
                "title": f"{title} · {status}",
                "style": "padding: 0.2rem; text-decoration: none;",
            },
            "content": [
                {"component": "VCard", "props": {"class": "elevation-2"}, "content": body}
            ],
        }

    @staticmethod
    def _is_mobile(user_agent: Optional[str]) -> bool:
        """按 UA 判断是否为移动端。"""
        if not user_agent:
            return False
        return any(keyword.lower() in user_agent.lower()
                   for keyword in ("mobile", "android", "iphone", "ipad", "kindle"))
