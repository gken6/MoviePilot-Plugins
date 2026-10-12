"""直连服务器与人工匹配管理；数据通过插件存储隔离，不改 MP 媒体服务器配置。"""
from __future__ import annotations

import copy
import re
import uuid
import time
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Body, Depends, HTTPException
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.security import verify_token

from .doubanclient import DoubanClient

SERVERS = "direct_servers_v2"
OVERRIDES = "manual_matches"
IGNORED = "unmatched"
CORRECTIONS = "corrections"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def require_manager(token=Depends(verify_token)):
    """账号与人工匹配管理保持与宿主插件设置相同的管理员权限。"""
    if not getattr(token, "super_user", False):
        raise HTTPException(status_code=403, detail="仅管理员可以管理豆瓣观看同步")


class WatchManagement:
    """管理直连账号和人工匹配，外部操作先登记后异步执行，可重试。"""

    @staticmethod
    def get_render_mode():
        return "vue", "frontend/dist/assets"

    def _management_init(self, config: dict) -> None:
        with self._lock:
            stored = self.get_data(SERVERS)
            if stored is None:
                accounts = {item["name"]: item for item in self._direct_account_configs()}
                records = {}
                for item in self._direct_server_configs_legacy():
                    item = {**accounts.pop(item["name"], {}), **item}
                    identifier = uuid.uuid4().hex
                    records[identifier] = {**item, "id": identifier, "enabled": True,
                                           "status": "正常" if item.get("token") else "正在获取token",
                                           "movies": None, "series": None, "revision": 1}
                for item in accounts.values():
                    identifier = uuid.uuid4().hex
                    records[identifier] = {**item, "id": identifier, "enabled": True,
                                           "status": "正在获取token", "movies": None,
                                           "series": None, "revision": 1}
                self.save_data(SERVERS, records)
            records = copy.deepcopy(self.get_data(SERVERS) or {})
            by_name = {item.get("name"): identifier for identifier, item in records.items()}
            # 将旧直连历史绑定到稳定 ID，删除服务器后也不会回退到 MP。
            for bucket in ("archive", "pending", IGNORED):
                history = copy.deepcopy(self.get_data(bucket) or {})
                changed = False
                for record in history.values():
                    if isinstance(record, dict) and not record.get("direct_id") and record.get("server_name") in by_name:
                        record["direct_id"] = by_name[record["server_name"]]
                        changed = True
                if changed:
                    self.save_data(bucket, history)
        for identifier, item in records.items():
            if item.get("enabled", True) and (not item.get("token") or item.get("movies") is None):
                self._queue_server_check(identifier)
        for key in (self.get_data(CORRECTIONS) or {}):
            self._queue_correction(key)

    def management_api(self) -> list:
        return [
            {"path": "/manage", "endpoint": self.api_management, "methods": ["GET"], "auth": "bear", "dependencies": [Depends(require_manager)]},
            {"path": "/servers", "endpoint": self.api_server, "methods": ["POST"], "auth": "bear", "dependencies": [Depends(require_manager)]},
            {"path": "/login", "endpoint": self.api_login, "methods": ["POST"], "auth": "bear", "dependencies": [Depends(require_manager)]},
            {"path": "/match", "endpoint": self.api_match, "methods": ["POST"], "auth": "bear", "dependencies": [Depends(require_manager)]},
        ]

    def api_login(self) -> dict:
        """手动获取登录态，后台执行；不写入任何豆瓣观看记录。"""
        if not self._auth_ready():
            return {"success": False, "message": "请先启用插件及自动续登，填写豆瓣账号密码并保存"}
        with self._lock:
            if getattr(self, "_login_manual_pending", False) or getattr(self, "_login_active_key", None) == self._credential_key or getattr(self, "_login_qr", None):
                return {"success": True, "message": "已有登录任务，请等待账号登录或扫码确认"}
            self._login_manual_pending = True
        def run():
            try:
                with self._login_lock:
                    record = self._auth_record()
                    self.save_data("douban_login", {**record, "credential_key": self._credential_key, "next_attempt": 0})
                    self._prepare_login(DoubanClient(cookie=self._load_cookie()), force=True)
            finally:
                self._login_manual_pending = False
        self._schedule_once("login_only", run, "获取豆瓣登录 Cookie", 1)
        return {"success": True, "message": "已开始获取 Cookie；账号登录失败时将发送二维码通知"}

    def api_management(self) -> dict:
        """返回界面数据，不返回直连密码、Token、Cookie 或用户 ID。"""
        with self._lock:
            servers = [self._public_server(item) for item in (self.get_data(SERVERS) or {}).values()]
            corrections = copy.deepcopy(self.get_data(CORRECTIONS) or {})
            ignored = copy.deepcopy(self.get_data(IGNORED) or {})
            overrides = copy.deepcopy(self.get_data(OVERRIDES) or {})
            archive = copy.deepcopy(self.get_data("archive") or {})
            pending = copy.deepcopy(self.get_data("pending") or {})
            images = dict(self.get_data("images") or {})
        missing_images = set()
        def rows(mapping):
            result = []
            for key, record in mapping.items():
                operation = corrections.get(key) or {}
                image = str(record.get("image") or "")
                poster = ""
                if image and DoubanClient.is_public_image_url(image):
                    poster = images.get(image) or ""
                    if not poster:
                        missing_images.add(image)
                result.append({"key": key, **record, "image": poster,
                               "manual": bool((overrides.get(key) or {}).get("subject_id")),
                               "operation": operation.get("message", ""),
                               "processing": operation.get("state") == "running",
                               "needs_cleanup": bool(operation.get("cleanup_ids")),
                               "target_id": operation.get("subject_id", ""),
                               "target_status": (operation.get("record") or {}).get("status", "")})
            return sorted(result, key=lambda row: row.get("timestamp", ""), reverse=True)
        result = {"servers": servers, "archive": rows(archive), "pending": rows(pending),
                  "unmatched": rows(ignored), "enabled": self._enabled, "login": self._auth_public()}
        self._queue_record_images(missing_images)
        return result

    def _queue_record_images(self, urls: set) -> None:
        """海报回源延后执行，页面刷新只读缓存；失败五分钟后允许重试。"""
        with self._lock:
            if getattr(self, "_record_images_busy", False):
                return
            retry = getattr(self, "_record_image_retry", {})
            self._record_image_retry = retry
            urls = [url for url in sorted(urls) if retry.get(url, 0) <= time.monotonic()][:8]
            if not urls:
                return
            self._record_images_busy = True
        def fetch():
            try:
                client = DoubanClient(timeout=5)
                for url in urls:
                    uri = client.fetch_image_data_uri(url)
                    with self._lock:
                        if uri:
                            mapping = dict(self.get_data("images") or {})
                            mapping[url] = uri
                            mapping = dict(list(mapping.items())[-200:])
                            self.save_data("images", mapping)
                            self._image_map = mapping
                            self._image_failed.discard(url)
                            retry.pop(url, None)
                        else:
                            retry[url] = time.monotonic() + 300
                with self._lock:
                    # 冷却只保留本批及最近失败的地址，避免长期累积。
                    self._record_image_retry = {url: until for url, until in retry.items() if until > time.monotonic()}
            finally:
                with self._lock:
                    self._record_images_busy = False
        try:
            self._schedule_once("record_images", fetch, "缓存观看记录海报", 1)
        except Exception:
            with self._lock:
                self._record_images_busy = False
            logger.debug("登记海报缓存任务失败，下次打开记录时重试")

    @staticmethod
    def _public_server(item: dict) -> dict:
        return {key: item.get(key) for key in
                ("id", "name", "url", "username", "enabled", "status", "movies", "series", "error", "updated_at")}

    def api_server(self, data: dict = Body(...)) -> dict:
        """新增/修改/启停/删除直连服务器，账号检查在延后任务中执行。"""
        action = str(data.get("action") or "save")
        identifier = str(data.get("id") or "")
        with self._lock:
            records = copy.deepcopy(self.get_data(SERVERS) or {})
            previous = records.get(identifier)
            if action in {"delete", "toggle", "refresh"} and not previous:
                return {"success": False, "message": "服务器已不存在，请刷新列表"}
            if action == "delete":
                records.pop(identifier)
                self.save_data(SERVERS, records)
                self._clear_server_poll(previous["name"])
                return {"success": True, "message": "已删除服务器，历史同步记录保留"}
            if action == "toggle":
                previous["enabled"] = bool(data.get("enabled"))
                previous["revision"] = int(previous.get("revision") or 0) + 1
                previous["status"] = "正在获取token" if previous["enabled"] else "未启用"
                previous["error"] = ""
                self.save_data(SERVERS, records)
                self._clear_server_poll(previous["name"])
            elif action == "refresh":
                if not previous.get("enabled", True):
                    return {"success": False, "message": "请先启用服务器"}
                previous["revision"] = int(previous.get("revision") or 0) + 1
                previous["status"] = "正在获取token"
                previous["error"] = ""
                self.save_data(SERVERS, records)
            elif action == "save":
                if identifier and not previous:
                    return {"success": False, "message": "服务器已不存在，请刷新列表"}
                name = str(data.get("name") or "").strip()
                url = str(data.get("url") or "").strip().rstrip("/")
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "") or (previous or {}).get("password", "")
                try:
                    parsed = urlsplit(url)
                    parsed.port  # 校验非法端口和 IPv6 地址，返回友好错误。
                except ValueError:
                    return {"success": False, "message": "服务器地址格式不正确"}
                if not name or len(name) > 80 or any(char in name for char in "|@\n\r"):
                    return {"success": False, "message": "请填写服务器名（不能包含 |、@ 或换行）"}
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    return {"success": False, "message": "请填写 http:// 或 https:// 开头的 Emby 地址"}
                if not username or not password:
                    return {"success": False, "message": "请填写用户名和密码"}
                if name in [item.get("value") for item in self._server_items()]:
                    return {"success": False, "message": "名称与 MP 媒体服务器重复，请换一个直连服务器名"}
                if any(item.get("name") == name and key != identifier for key, item in records.items()):
                    return {"success": False, "message": "服务器名已存在，请换一个名称"}
                old_name = (previous or {}).get("name")
                identifier = identifier or uuid.uuid4().hex
                enabled = bool(data.get("enabled", True))
                records[identifier] = {"id": identifier, "name": name, "url": url, "username": username,
                                       "password": password, "enabled": enabled,
                                       "token": "", "user_id": "", "movies": None, "series": None,
                                       "status": "正在获取token" if enabled else "未启用", "error": "",
                                       "revision": int((previous or {}).get("revision") or 0) + 1}
                self.save_data(SERVERS, records)
                if old_name and old_name != name:
                    self._rename_direct_records(old_name, name)
                self._clear_server_poll(old_name or name)
            else:
                return {"success": False, "message": "不支持的操作"}
            item = records[identifier]
        if item.get("enabled", True):
            self._queue_server_check(identifier)
        return {"success": True, "message": "已保存", "server": self._public_server(item)}

    def _queue_server_check(self, identifier: str) -> None:
        self._schedule_once(f"direct_check_{identifier}", lambda: self._check_server(identifier), "检查直连 Emby 账号", 1)

    def _check_server(self, identifier: str) -> None:
        with self._lock:
            item = copy.deepcopy((self.get_data(SERVERS) or {}).get(identifier) or {})
        if not item or not item.get("enabled", True):
            return
        revision = item.get("revision")
        auth = self._authenticate_direct(item) if item.get("password") else (
            {"token": item.get("token"), "user_id": item.get("user_id"), "username": item.get("username")}
            if item.get("token") else None)
        error = ""
        counts = None
        if auth:
            counts = self._direct_counts({**item, **auth})
            if not auth.get("user_id"):
                error = "登录成功但没有返回用户 ID，请检查账号权限"
        else:
            error = "无法获取 Token，请检查地址、账号密码或服务器连接"
        with self._lock:
            records = copy.deepcopy(self.get_data(SERVERS) or {})
            current = records.get(identifier)
            if not current or not current.get("enabled", True) or current.get("revision") != revision:
                return
            if auth and not error:
                current.update(auth)
                current["status"] = "正常"
                current["error"] = "" if counts is not None else "账号正常，暂时无法读取媒体数量"
                if counts is not None:
                    current.update(counts)
            else:
                current.update({"token": "", "user_id": "", "status": "获取失败", "error": error})
            current["updated_at"] = now()
            self.save_data(SERVERS, records)

    @staticmethod
    def _authenticate_direct(item: dict):
        """用独立 Emby 账号换取 Token，不读取 MP 媒体服务器。"""
        base = item["url"].rstrip("/")
        headers = {"X-Emby-Authorization": 'MediaBrowser Client="MoviePilot DoubanWatchSync", Device="WatchSync", DeviceId="doubanwatchsync-' + item["id"] + '", Version="2.0.0"',
                   "Accept": "application/json", "Origin": base, "Referer": base + "/web/index.html"}
        for path in ("/emby/Users/AuthenticateByName", "/Users/AuthenticateByName"):
            try:
                response = RequestUtils(headers=headers, timeout=20).post_res(
                    base + path, json={"Username": item["username"], "Pw": item["password"]})
                result = response.json() if response is not None and response.status_code == 200 else {}
                if result.get("AccessToken"):
                    return {"token": str(result["AccessToken"]), "user_id": str((result.get("User") or {}).get("Id") or ""),
                            "username": str((result.get("User") or {}).get("Name") or item["username"])}
            except Exception:
                continue
        return None

    @staticmethod
    def _direct_counts(item: dict):
        """统计当前直连账号可见的影片和剧集数量，失败时不伪装成零。"""
        if not item.get("user_id"):
            return None
        from urllib.parse import quote
        headers = {"X-Emby-Token": item["token"], "Accept": "application/json"}
        counts = {}
        for item_type, name in (("Movie", "movies"), ("Series", "series")):
            try:
                url = (item["url"].rstrip("/") + "/emby/Users/" + quote(item["user_id"], safe="") +
                       f"/Items?IncludeItemTypes={item_type}&Recursive=true&Limit=0&EnableTotalRecordCount=true")
                response = RequestUtils(headers=headers, timeout=20).get_res(url)
                if response is None or response.status_code != 200:
                    return None
                data = response.json()
                if "TotalRecordCount" not in data:
                    return None
                counts[name] = int(data["TotalRecordCount"])
            except Exception:
                return None
        return counts

    def _managed_direct_configs(self, include_disabled=False) -> list:
        records = self.get_data(SERVERS)
        if records is None:
            return self._direct_server_configs_legacy()
        return [copy.deepcopy(item) for item in records.values()
                if include_disabled or (item.get("enabled", True) and item.get("token") and item.get("user_id"))]

    def _direct_enabled(self, server: str) -> bool:
        matches = [item for item in (self.get_data(SERVERS) or {}).values() if item.get("name") == server]
        return not matches or bool(matches[0].get("enabled", True))

    def _clear_server_poll(self, name: str) -> None:
        poll = dict(self.get_data("poll") or {})
        self.save_data("poll", {key: value for key, value in poll.items() if not key.startswith(f"direct|{name}|")})

    def _rename_direct_records(self, old: str, new: str) -> None:
        for store in ("archive", "pending", IGNORED, OVERRIDES, CORRECTIONS):
            records = copy.deepcopy(self.get_data(store) or {})
            for key in list(records):
                record = records[key]
                if key.endswith("@" + old) or record.get("server_name") == old:
                    record["server_name"] = new
                    new_key = key[:-len(old)] + new if key.endswith("@" + old) else key
                    records.pop(key)
                    records[new_key] = record
            self.save_data(store, records)

    def _record_for_key(self, key: str):
        for store in ("archive", "pending", IGNORED):
            record = (self.get_data(store) or {}).get(key)
            if isinstance(record, dict):
                return copy.deepcopy(record), store
        return None, None

    def api_match(self, data: dict = Body(...)) -> dict:
        """人工指定豆瓣编号或排除无条目内容，保存意图后异步同步。"""
        key = str(data.get("key") or "")
        action = str(data.get("action") or "sync")
        with self._lock:
            record, store = self._record_for_key(key)
            if not record:
                return {"success": False, "message": "记录已不存在，请刷新列表"}
            corrections = copy.deepcopy(self.get_data(CORRECTIONS) or {})
            if (corrections.get(key) or {}).get("state") == "running":
                return {"success": False, "message": "这条记录正在处理，请稍后再试"}
            overrides = copy.deepcopy(self.get_data(OVERRIDES) or {})
            ignored = copy.deepcopy(self.get_data(IGNORED) or {})
            if action == "restore":
                if store != IGNORED:
                    return {"success": False, "message": "这条记录不在无条目列表"}
                ignored.pop(key, None)
                overrides.pop(key, None)
                record["subject_id"] = ""
                record["timestamp"] = now().replace("T", " ")
                pending = dict(self.get_data("pending") or {})
                pending[key] = record
                self.save_data("pending", pending)
                self.save_data(IGNORED, ignored)
                self.save_data(OVERRIDES, overrides)
                self._clear_record_poll(record)
                return {"success": True, "message": "已移回待同步，将重新自动匹配"}
            if action == "retry" and corrections.get(key):
                corrections[key]["state"] = "running"
                corrections[key]["message"] = "正在重新同步"
            else:
                subject = str(data.get("subject_id") or "").strip()
                if action not in {"sync", "ignore"}:
                    return {"success": False, "message": "不支持的操作"}
                if action == "sync" and (not re.fullmatch(r"[0-9]{1,15}", subject) or int(subject) <= 0):
                    return {"success": False, "message": "请填写豆瓣条目页面中的数字编号"}
                if action == "sync":
                    is_tv = str(record.get("type") or "").startswith(("TV", "电视剧"))
                    allowed = {"collect", "do"} if is_tv else {"collect"}
                    requested = str(data.get("status") or record.get("status") or "collect")
                    # 兼容旧版电影待同步记录错误保存的 do；显式请求仍严格校验。
                    if "status" not in data and requested not in allowed:
                        requested = "collect"
                    if requested not in allowed:
                        return {"success": False, "message": "请选择有效状态：影片只能选看过，剧集可选在看或看过"}
                    record = {**record, "status": requested}
                old_operation = corrections.get(key) or {}
                cleanup = list(old_operation.get("cleanup_ids") or [])
                old_subject = str(record.get("subject_id") or "") if store == "archive" else ""
                if old_subject and old_subject != subject and old_subject not in cleanup:
                    cleanup.append(old_subject)
                if action == "sync":
                    cleanup = [identifier for identifier in cleanup if identifier != subject]
                corrections[key] = {"id": uuid.uuid4().hex, "record": record, "action": action,
                                    "subject_id": subject if action == "sync" else "",
                                    "cleanup_ids": cleanup, "new_synced": False, "state": "running",
                                    "message": "正在重新同步" if action == "sync" else "正在移入无条目", "created_at": now()}
                overrides[key] = {"subject_id": subject, "ignore": action == "ignore"}
                self.save_data(OVERRIDES, overrides)
                if action == "ignore":
                    ignored[key] = {**record, "timestamp": now().replace("T", " ")}
                    self.save_data(IGNORED, ignored)
                    for bucket in ("archive", "pending"):
                        records = dict(self.get_data(bucket) or {})
                        records.pop(key, None)
                        self.save_data(bucket, records)
            self.save_data(CORRECTIONS, corrections)
        self._queue_correction(key)
        return {"success": True, "message": "已提交", "queued": True}

    def _clear_record_poll(self, record: dict) -> None:
        server = str(record.get("server_name") or "")
        self._clear_server_poll(server)

    def _queue_correction(self, key: str) -> None:
        job_id = "manual_" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex
        self._schedule_once(job_id, lambda: self._run_correction(key), "人工修正豆瓣匹配", 1)

    def _apply_correction(self, key: str) -> None:
        """先写入正确条目再撤销错误标记；部分失败留队列，不谎报完全成功。"""
        with self._lock:
            operation = copy.deepcopy((self.get_data(CORRECTIONS) or {}).get(key) or {})
        if not operation:
            return
        record = operation["record"]
        with self._lock:
            latest = (self.get_data("archive") or {}).get(key) or {}
            old_subject = str(latest.get("subject_id") or "")
            if old_subject and old_subject != operation.get("subject_id") and old_subject not in operation["cleanup_ids"]:
                operation["cleanup_ids"].append(old_subject)
            if operation["action"] == "ignore":
                for bucket in ("archive", "pending"):
                    records = dict(self.get_data(bucket) or {})
                    records.pop(key, None)
                    self.save_data(bucket, records)
        success = False
        message = ""
        try:
            client = DoubanClient(cookie=self._load_cookie() if operation["action"] == "sync" or operation["cleanup_ids"] else "")
            needs_login = operation["action"] == "sync" or bool(operation["cleanup_ids"])
            if needs_login and not client.has_login() and not self._auto_login and not self._use_cookiecloud:
                message = "豆瓣登录态不可用，请检查 Cookie 或 CookieCloud"
            else:
                if operation["action"] == "sync" and not operation.get("new_synced"):
                    subject = operation["subject_id"]
                    media_type = "TV" if str(record.get("type") or "").startswith(("TV", "电视剧")) else "MOV"
                    # 验证真实条目，避免把任意数字当成有效豆瓣编号。
                    detail = client.get_subject_info(subject, media_type)
                    if not detail:
                        message = "无法读取该豆瓣条目，请核对编号或稍后重试"
                    elif self._write_douban(client, "set_status", subject, record.get("status") or "do", self._private):
                        payload = {"episode": record.get("episode", 0), "item_id": record.get("item_id", ""),
                                   "server_name": record.get("server_name", ""), "user_name": record.get("user_name", ""),
                                   "title": record.get("source_title") or record.get("title") or "", "direct_id": record.get("direct_id") or ""}
                        title = str(detail.get("title") or record.get("subject_name") or record.get("title") or key)
                        self._save_archive(key, payload, title, subject, int(record.get("season") or 0), media_type,
                                           record.get("status") or "do", title, image=str(detail.get("image") or ""))
                        self._drop_pending(key)
                        with self._lock:
                            ignored = dict(self.get_data(IGNORED) or {})
                            ignored.pop(key, None)
                            self.save_data(IGNORED, ignored)
                        operation["new_synced"] = True
                    else:
                        message = "写入豆瓣失败"
                        if getattr(client, "last_error", ""):
                            message += "：" + client.last_error
                if not message:
                    for subject in list(operation["cleanup_ids"]):
                        if self._subject_used_elsewhere(subject, key):
                            operation["cleanup_ids"].remove(subject)
                            continue
                        if self._write_douban(client, "remove_status", subject):
                            operation["cleanup_ids"].remove(subject)
                        else:
                            message = "新条目已同步，旧标记待撤销" if operation["action"] == "sync" else "已停止匹配，旧豆瓣标记待撤销"
                            if getattr(client, "last_error", ""):
                                message += "：" + client.last_error
                            break
                    success = not message
        except Exception as error:
            logger.warn(f"人工修正失败：{type(error).__name__}")
            message = "处理失败，请稍后重试"
        with self._lock:
            current = dict(self.get_data(CORRECTIONS) or {})
            if (current.get(key) or {}).get("id") != operation.get("id"):
                return
            if success:
                current.pop(key, None)
            else:
                operation.update({"state": "failed", "message": message, "updated_at": now()})
                current[key] = operation
            self.save_data(CORRECTIONS, current)

    def _subject_used_elsewhere(self, subject: str, key: str) -> bool:
        return any(other != key and str(record.get("subject_id") or "") == subject
                   for other, record in (self.get_data("archive") or {}).items())

    def _manual_guard(self, key: str) -> bool:
        override = (self.get_data(OVERRIDES) or {}).get(key) or {}
        return bool(override.get("ignore") or key in (self.get_data(CORRECTIONS) or {}))

    def _manual_subject(self, key: str) -> str:
        return str(((self.get_data(OVERRIDES) or {}).get(key) or {}).get("subject_id") or "")

    def retry_management(self) -> None:
        """重试人工修正任务和失败直连登录，停用的服务器不访问。"""
        for key in (self.get_data(CORRECTIONS) or {}):
            self._queue_correction(key)
        for identifier, item in (self.get_data(SERVERS) or {}).items():
            if item.get("enabled", True) and item.get("status") != "正在获取token":
                self._queue_server_check(identifier)

    def _get_record_lock(self, key: str):
        import threading
        with self._lock:
            return self._record_locks.setdefault(key, threading.RLock())

    def _sync_item(self, payload: dict) -> None:
        key = self._archive_key(payload.get("title") or "", payload.get("media_type") or "",
                                int(payload.get("season") or 0), payload.get("server_name"))
        with self._get_record_lock(key):
            if not self._enabled or self._manual_guard(key) or not self._direct_record_allowed(payload):
                return
            self._sync_item_original(payload)

    def _direct_record_allowed(self, record: dict) -> bool:
        identifier = record.get("direct_id")
        if not identifier:
            # Legacy archives can be recognized by their direct server name.
            items = [item for item in (self.get_data(SERVERS) or {}).values()
                     if item.get("name") == record.get("server_name")]
            return not items or bool(items[0].get("enabled", True))
        item = (self.get_data(SERVERS) or {}).get(identifier)
        return bool(item and item.get("enabled", True) and item.get("token"))

    def _direct_payload_allowed(self, payload: dict, server: dict) -> bool:
        # A direct account is isolated from the MP username allowlist.
        if server.get("username") and payload.get("user_name") != server["username"]:
            return False
        if payload.get("media_type") == "TV" and self._skip_first and payload.get("episode") == 1:
            return False
        reader = self._reader(server["name"], username=server.get("username"), user_id=server.get("user_id"))
        if self._exclude:
            if not self._is_allowed_path(reader.get_item_path(payload.get("item_id") or "")):
                return False
        key = self._archive_key(payload.get("title") or "", payload.get("media_type") or "",
                                int(payload.get("season") or 0), server["name"])
        return not self._manual_guard(key)

    def _run_correction(self, key: str) -> None:
        with self._get_record_lock(key):
            self._apply_correction(key)

    def _direct_emby_sessions(self, config: dict) -> list:
        """只访问启用的独立直连账号；认证失效时进入可见的失败状态。"""
        if not config.get("enabled", True) or not config.get("token"):
            return []
        try:
            response = RequestUtils(headers={"X-Emby-Token": config["token"], "Accept": "application/json"}, timeout=20).get_res(
                config["url"].rstrip("/") + "/emby/Sessions")
            if response is not None and response.status_code in (401, 403):
                with self._lock:
                    records = copy.deepcopy(self.get_data(SERVERS) or {})
                    current = records.get(config.get("id"))
                    if current and current.get("revision") == config.get("revision") and current.get("enabled", True):
                        current.update({"status": "获取失败", "token": "", "error": "登录已失效，将自动重试；也可点击重试"})
                        self.save_data(SERVERS, records)
                return []
            if response is None or response.status_code != 200:
                return []
            data = response.json()
            return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        except Exception:
            return []
