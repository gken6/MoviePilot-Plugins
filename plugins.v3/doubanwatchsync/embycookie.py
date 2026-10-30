"""把已确认有效的豆瓣 Cookie 同步到明确配置的 Emby 神医助手。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Body

from .doubanclient import DoubanClient

TARGET = "emby_cookie_target"
SESSION = "emby_cookie_session"
RESULT = "emby_cookie_result"
PLUGIN_ID = "63c322b7-a371-41a3-b11f-04f8418b37d8"


def target_url(value: str) -> str:
    """
    校验服务器地址，不接受带凭据、查询参数或片段的 URL。

    :param value: 管理员填写的 Emby 根地址。
    :return: 标准化后的 http/https 地址。
    """
    value = str(value or "").strip().rstrip("/")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Emby 地址不能包含控制字符")
    if value and "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Emby 端口格式不正确") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("请填写 Emby 的 http:// 或 https:// 服务器地址")
    return value


def target_key(config: dict) -> str:
    """
    将凭据绑定到目标地址和账号，防止复用到其他服务器。

    :param config: 仅用于目标服务器的连接配置。
    :return: 不含明文凭据的稳定摘要。
    """
    values = [config.get(name) or "" for name in ("url", "username", "password")]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    禁止认证及 Cookie 写入请求随重定向发送到另一地址。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """
        终止重定向，由上层显示固定的连接失败提示。
        """
        return None


class EmbyAssistantClient:
    """
    使用管理员用户登录态调用 Emby 官方通用配置界面。
    """

    def __init__(self, config: dict, session: dict, guard=lambda: True):
        """
        初始化仅面向当前目标的客户端，不进行网络请求。

        :param config: Emby 地址、管理员账号及可选密码。
        :param session: 此目标已有的管理员会话。
        :param guard: 每次发送前检查任务是否仍被允许。
        """
        self.config = copy.deepcopy(config)
        self.session = copy.deepcopy(session)
        self.guard = guard
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.token = ""

    def _request(self, path: str, data=None):
        """
        只向配置地址发送请求，凭据不放入 URL，不回显响应正文。

        :param path: Emby API 相对路径。
        :param data: 非空时使用 JSON POST；为空时使用 GET。
        :return: 已解析的响应或空字典。
        """
        if not self.guard():
            raise RuntimeError("配置已改变或插件已停用，本次同步已取消")
        base = target_url(self.config.get("url"))
        if base.endswith("/emby"):
            base = base[:-5]
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "X-Emby-Authorization": 'MediaBrowser Client="MoviePilot DoubanWatchSync", Device="MoviePilot", DeviceId="douban-cookie-forward", Version="2.3.11"'}
        if self.token:
            headers["X-Emby-Token"] = self.token
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        request = urllib.request.Request(base + "/emby/" + path.lstrip("/"), data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(4 * 1024 * 1024)
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise PermissionError("Emby 管理员授权无效，请检查账号密码后重试") from None
            raise RuntimeError(f"Emby 请求未完成（HTTP {error.code}）") from None
        except (OSError, ValueError):
            raise RuntimeError("无法读取 Emby 响应，请检查地址、网络或证书") from None

    def _is_admin(self, user) -> bool:
        """
        确认登录态属于配置的管理员账号，不能使用普通观看账号改配置。

        :param user: Emby 用户信息。
        :return: 用户名及管理员权限是否均匹配。
        """
        return (isinstance(user, dict) and user.get("Policy", {}).get("IsAdministrator") is True
                and str(user.get("Name") or "").casefold() == str(self.config.get("username") or "").casefold())

    def authenticate(self) -> dict:
        """
        优先复用目标会话，失效时按已配置的管理员账号密码重新登录。

        :return: 仅供后端保存的会话，不返回前端。
        """
        key = target_key(self.config)
        if self.session.get("target_key") == key and self.session.get("token") and self.session.get("user_id"):
            self.token = self.session["token"]
            try:
                if self._is_admin(self._request("Users/" + urllib.parse.quote(self.session["user_id"], safe=""))):
                    return self.session
            except PermissionError:
                pass
            self.token = ""
        if not self.config.get("password"):
            raise PermissionError("请填写 Emby 管理员密码后重试；已有会话不可用")
        result = self._request("Users/AuthenticateByName", {"Username": self.config.get("username"), "Pw": self.config["password"]})
        self.token = str(result.get("AccessToken") or "")
        if not self.token or not self._is_admin(result.get("User")):
            self.token = ""
            raise PermissionError("需要 Emby 管理员账号，普通观看账号不能保存神医助手配置")
        self.session = {"target_key": key, "token": self.token, "user_id": result["User"]["Id"]}
        return self.session

    def sync(self, cookie: str) -> None:
        """
        保存并回读神医助手 Cookie，仅修改目标字段。

        :param cookie: 已确认有效的豆瓣 Cookie。
        """
        if not cookie or not cookie.strip():
            raise RuntimeError("没有可同步的豆瓣 Cookie")
        params = urllib.parse.urlencode({"PageType": "PluginConfiguration", "EnableInUserMenu": "false",
                                        "UserId": self.session["user_id"]})
        pages = self._request("web/configurationpages?" + params)
        page = next((item for item in pages if item.get("PluginId", "").lower() == PLUGIN_ID
                     and item.get("Name") == "MetadataEnhancePageView"), None)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit((page or {}).get("Href") or "").query)
        page_id = (query.get("PageId") or [""])[0]
        if not page_id:
            raise RuntimeError("未找到神医助手元数据配置页，请确认已安装支持新版配置界面的神医助手")
        path = "UI/View?" + urllib.parse.urlencode({"PageId": page_id, "ClientLocale": "zh-CN"})
        view = self._request(path)
        original = view.get("EditObjectContainer", {}).get("Object")
        if (view.get("PageId") != page_id or not isinstance(original, dict)
                or "DoubanCookie" not in original.get("MetadataRefreshOptions", {})
                or not any(command.get("CommandId") == "PageSave" and command.get("IsEnabled") is True
                           for command in view.get("Commands", []))):
            raise RuntimeError("神医助手配置结构不兼容，本次未写入")
        updated = copy.deepcopy(original)
        updated["MetadataRefreshOptions"]["DoubanCookie"] = cookie
        if original != updated:
            self._request("UI/Command", {"PageId": page_id, "CommandId": "PageSave",
                                         "Data": json.dumps(updated, ensure_ascii=False), "ItemId": None,
                                         "ClientLocale": "zh-CN"})
        actual = self._request(path).get("EditObjectContainer", {}).get("Object")
        if not isinstance(actual, dict) or actual.get("MetadataRefreshOptions", {}).get("DoubanCookie") != cookie:
            raise RuntimeError("神医助手未保存预期 Cookie，请稍后重试")
        expected = copy.deepcopy(original)
        actual = copy.deepcopy(actual)
        expected["MetadataRefreshOptions"].pop("DoubanCookie")
        actual["MetadataRefreshOptions"].pop("DoubanCookie")
        if expected != actual:
            raise RuntimeError("Cookie 已保存，但配置回读有其他变化，请检查神医助手设置")


class EmbyCookieForwarding:
    """
    Cookie 更新事件和手动操作共用的后台同步状态机。
    """

    def _emby_cookie_init(self) -> None:
        """
        初始化实例锁；保存设置及重载插件均不自动发送 Cookie。
        """
        if not hasattr(self, "_cookie_forward_lock"):
            self._cookie_forward_lock = threading.RLock()
        self._cookie_forward_sequence = getattr(self, "_cookie_forward_sequence", 0) + 1
        self._cookie_forward_pending = None

    def _cookie_target_public(self) -> dict:
        """
        返回配置和同步结果，隐藏 Cookie、管理员密码和 Token。
        """
        config = self.get_data(TARGET) or {}
        result = self.get_data(RESULT) or {}
        public = {key: config.get(key) for key in ("url", "username", "enabled", "revision")}
        public.update(password_saved=bool(config.get("password")),
                      status=result.get("status") or "未同步", message=result.get("message") or "",
                      last_success=result.get("last_success") or 0)
        return public

    def api_cookie_target(self, data: dict = Body(...)) -> dict:
        """
        保存独立目标；手动同步同时保存当前输入，避免要求用户先点另一个保存按钮。

        :param data: 配置字段及 save/sync 操作。
        :return: 仅包含操作结果的字典。
        """
        action = data.get("action") or "save"
        if action not in {"save", "sync"}:
            return {"success": False, "message": "不支持的操作"}
        old = self.get_data(TARGET) or {}
        try:
            url = target_url(data.get("url") or "")
        except ValueError as error:
            return {"success": False, "message": str(error)}
        username = str(data.get("username") or "").strip()
        if not username:
            return {"success": False, "message": "请填写 Emby 管理员用户名"}
        password = str(data.get("password") or "")
        if not password and old.get("url") == url and old.get("username") == username:
            password = old.get("password") or ""
        config = {"url": url, "username": username, "password": password,
                  "enabled": bool(data.get("enabled")), "revision": int(old.get("revision") or 0) + 1}
        with self._lock:
            self._cookie_forward_sequence += 1
            self._cookie_forward_pending = None
            self.save_data(TARGET, config)
            if target_key(old) != target_key(config):
                self.save_data(SESSION, {})
            previous = self.get_data(RESULT) or {}
            self.save_data(RESULT, {"status": "未同步", "message": "", "last_success": previous.get("last_success", 0) if target_key(old) == target_key(config) else 0})
        if action == "sync":
            if not self._enabled:
                return {"success": False, "message": "目标已保存，请先启用豆瓣观看同步插件"}
            self._queue_cookie_forward("", manual=True)
            return {"success": True, "message": "已保存，正在后台同步当前有效 Cookie"}
        return {"success": True, "message": "已保存；新 Cookie 获取成功后会按开关自动同步"}

    def _on_verified_cookie(self, cookie: str) -> None:
        """
        转发故障只更新独立结果，不能让已成功的豆瓣登录或观看同步失败。

        :param cookie: 刚刚确认有效的 Cookie。
        """
        try:
            self._queue_cookie_forward(cookie)
        except Exception:
            try:
                self.save_data(RESULT, {**(self.get_data(RESULT) or {}), "status": "同步失败",
                                       "message": "神医助手同步任务未能排队，可稍后手动重试"})
            except Exception:
                pass

    def _queue_cookie_forward(self, cookie: str, manual=False) -> None:
        """
        合并重复的已验证 Cookie 更新，不阻塞豆瓣登录或影片同步。

        :param cookie: 自动更新已经验证的 Cookie；手动模式在后台读取当前有效值。
        :param manual: 明确的手动操作，允许关闭自动开关时执行。
        """
        config = self.get_data(TARGET) or {}
        if not self._enabled or not config.get("url") or not config.get("username") or (not manual and not config.get("enabled")):
            return
        fingerprint = hashlib.sha256(cookie.encode()).hexdigest() if cookie else "manual"
        key = target_key(config)
        with self._lock:
            previous = self.get_data(RESULT) or {}
            pending = (key, fingerprint, bool(manual))
            if self._cookie_forward_pending == pending:
                return
            if not manual and previous.get("target_key") == key and previous.get("cookie_hash") == fingerprint:
                if previous.get("status") == "同步成功" or time.time() - float(previous.get("attempted_at") or 0) < 300:
                    return
            self._cookie_forward_sequence += 1
            sequence = self._cookie_forward_sequence
            self._cookie_forward_pending = pending
            self.save_data(RESULT, {**previous, "status": "正在同步", "message": ""})
        try:
            self._schedule_once("emby_cookie_forward", lambda: self._run_cookie_forward(config, sequence, cookie, manual),
                                "同步豆瓣 Cookie 到神医助手", 1)
        except Exception:
            with self._lock:
                if sequence == self._cookie_forward_sequence:
                    self._cookie_forward_pending = None
                    self.save_data(RESULT, {**previous, "status": "同步失败", "target_key": key,
                                           "cookie_hash": fingerprint, "attempted_at": time.time()})
            raise

    def _manual_forward_cookie(self) -> str:
        """
        手动操作只检查已有 Cookie，不意外开启账号登录或发送扫码通知。
        """
        saved = self._auth_record()
        values = list(dict.fromkeys([self._cookie.strip(), str(saved.get("cookie") or "")]))
        for value in values:
            if not value:
                continue
            client = DoubanClient(cookie=value)
            if value == saved.get("cookie"):
                client.user_agent = saved.get("user_agent") or ""
            valid = client.login_state()
            if valid is True:
                return client.cookie_header()
            if valid is None:
                raise RuntimeError("暂时无法确认豆瓣登录态，请稍后重试")
        raise RuntimeError("没有有效的豆瓣 Cookie，请先获取登录 Cookie 或更新手填 Cookie")

    def _run_cookie_forward(self, config: dict, sequence: int, cookie: str, manual: bool) -> None:
        """
        串行更新目标，旧配置或旧 Cookie 任务不得在新任务之后覆盖结果。

        :param config: 排队时的目标配置快照。
        :param sequence: 当前实例的任务代次。
        :param cookie: 已验证的更新值。
        :param manual: 是否由管理员明确点击。
        """
        key = target_key(config)
        def allowed():
            current = self.get_data(TARGET) or {}
            return (self._enabled and sequence == self._cookie_forward_sequence
                    and current.get("revision") == config.get("revision") and target_key(current) == key
                    and (manual or current.get("enabled")))
        with self._cookie_forward_lock:
            if not allowed():
                return
            result = dict(self.get_data(RESULT) or {})
            try:
                cookie = self._manual_forward_cookie() if manual else cookie
                if not cookie:
                    raise RuntimeError("没有可同步的豆瓣 Cookie")
                client = EmbyAssistantClient(config, self.get_data(SESSION) or {}, guard=allowed)
                session = client.authenticate()
                if not allowed():
                    return
                self.save_data(SESSION, session)
                client.sync(cookie)
                result.update(status="同步成功", message="神医助手豆瓣 Cookie 已更新并回读确认", last_success=time.time())
            except (RuntimeError, PermissionError) as error:
                result.update(status="同步失败", message=str(error))
            except Exception:
                result.update(status="同步失败", message="同步未完成，请检查 Emby 地址、管理员授权及神医助手版本")
            if allowed():
                result.update(target_key=key, cookie_hash=hashlib.sha256(cookie.encode()).hexdigest(), attempted_at=time.time())
                self.save_data(RESULT, result)
                self._cookie_forward_pending = None

    def _stop_cookie_forward(self) -> None:
        """
        停用或重载时使旧任务失效，不清空 Emby 已有 Cookie。
        """
        self._cookie_forward_sequence = getattr(self, "_cookie_forward_sequence", 0) + 1
        self._remove_once("emby_cookie_forward")
