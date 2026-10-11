"""豆瓣账号自动续登：只有明确过期才重新登录，网络异常不触发登录风暴。"""
from __future__ import annotations

import hashlib
import threading
import time
from urllib.parse import urlsplit

from app.sdk.logging import logger

AUTH_DATA = "douban_login"
LOGIN_URL = "https://accounts.douban.com/passport/login?redir=https%3A%2F%2Fmovie.douban.com%2F"
MOVIE_HOME = "https://movie.douban.com/"


def credential_key(username: str, password: str) -> str:
    return hashlib.sha256((username + "\0" + password).encode()).hexdigest()


def _verified_cookie(context, page) -> dict | None:
    """Cookie 和电影站账号导航均确认后才接受登录。"""
    domains = {"douban.com", "movie.douban.com"}
    if not any(c.get("name") == "dbcl2" and c.get("value")
               and str(c.get("domain") or "").lstrip(".") in domains for c in context.cookies()):
        return None
    page.goto(MOVIE_HOME, wait_until="domcontentloaded", timeout=30000)
    if urlsplit(page.url).hostname != "movie.douban.com" or not page.query_selector("#db-global-nav .nav-user-account"):
        return None
    values = {}
    for cookie in sorted(context.cookies(), key=lambda c: str(c.get("domain") or "").lstrip(".") == "movie.douban.com"):
        if str(cookie.get("domain") or "").lstrip(".") in domains and cookie.get("value") and cookie.get("path", "/") == "/":
            values[str(cookie["name"])] = str(cookie["value"])
    if values.get("dbcl2") and values.get("ck"):
        return {"status": "登录正常", "cookie": "; ".join(f"{k}={v}" for k, v in values.items()),
                "user_agent": page.evaluate("navigator.userAgent")}
    return None


def _qr_image_url(value) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and port in (None, 443) and not parsed.fragment
            and (host == "accounts.douban.com" or host.endswith(".doubanio.com")))


def _qr_login(context, page, on_qr, should_continue) -> dict:
    """官方页面负责扫码状态轮询；不提交或识别验证码。"""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    if urlsplit(page.url).scheme != "https" or urlsplit(page.url).hostname != "accounts.douban.com":
        return {"status": "登录失败", "message": "扫码登录页面地址异常"}
    with page.expect_response(lambda response: urlsplit(response.url).hostname == "accounts.douban.com"
                              and urlsplit(response.url).path.endswith("/qrlogin_code"), timeout=15000) as pending:
        page.click(".account-tab-switch a.quick")
    body = pending.value.json()
    payload = body.get("payload") if isinstance(body, dict) else None
    image = payload.get("img") if isinstance(payload, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(payload, dict) or not payload.get("code") or not _qr_image_url(image):
        return {"status": "登录失败", "message": "豆瓣未提供可用登录二维码，下次同步时重试"}
    page.wait_for_selector(".account-qr-scan img", timeout=15000)
    expires_at = time.time() + 360  # 与官方页面 120 次、每 3 秒轮询的上限一致。
    if not should_continue() or on_qr(image, expires_at) is False:
        return {"status": "登录取消", "message": "扫码登录已取消"}
    while time.time() < expires_at:
        if not should_continue():
            return {"status": "登录取消", "message": "配置已改变或插件已停用，扫码登录取消"}
        result = _verified_cookie(context, page)
        if result:
            return result
        failed = page.query_selector(".account-qr-fail")
        if failed and failed.is_visible():
            return {"status": "二维码已过期", "message": "登录二维码已失效，观看记录保留待同步；下次重试会发送新二维码"}
        page.wait_for_timeout(1500)
    return {"status": "二维码已过期", "message": "未在有效期内确认扫码登录，观看记录保留待同步"}


def browser_login(username: str, password: str, *, on_qr=None, should_continue=None) -> dict:
    """先尝试账号密码；无法确认登录时，通知二维码并等待用户扫码。"""
    context = None
    try:
        from app.sdk.browser import launch_browser_context
        context = launch_browser_context(headless=True)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(".account-tab-account")
        if urlsplit(page.url).scheme != "https" or urlsplit(page.url).hostname != "accounts.douban.com":
            return {"status": "登录失败", "message": "登录页面地址异常，未提交账号密码"}
        failure = {"status": "登录失败", "message": "豆瓣未确认账号登录成功"}
        try:
            page.click(".account-tab-account")
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            with page.expect_response(lambda response: urlsplit(response.url).hostname == "accounts.douban.com"
                                      and urlsplit(response.url).path.endswith("/login/basic"), timeout=15000) as pending:
                page.click(".account-form-field-submit .btn-account")
            body = pending.value.json()
            if isinstance(body, dict) and body.get("status") == "success":
                try:
                    page.wait_for_function("() => location.pathname !== '/passport/login'", timeout=15000)
                except Exception:
                    pass
                result = _verified_cookie(context, page)
                if result:
                    return result
            else:
                text = " ".join(str(body.get(key) or "") for key in ("message", "description", "localized_message", "msg")).lower() if isinstance(body, dict) else ""
                payload = body.get("payload") if isinstance(body, dict) else None
                if any(word in text for word in ("captcha", "verify", "verification", "challenge", "验证码", "验证")) or (
                        isinstance(payload, dict) and payload.get("touch_cap_url") and payload.get("tc_app_id")):
                    failure = {"status": "需要验证", "message": "豆瓣要求安全验证，请使用扫码登录"}
                elif any(word in text for word in ("password", "credential", "密码")):
                    failure = {"status": "登录失败", "message": "豆瓣拒绝账号密码登录，请检查账号或使用扫码登录"}
        except Exception:
            failure = {"status": "登录失败", "message": "账号登录未完成，请检查 NAS 网络或使用扫码登录"}
        if on_qr:
            return _qr_login(context, page, on_qr, should_continue or (lambda: True))
        return failure
    except Exception:
        return {"status": "登录失败", "message": "无法完成浏览器登录，请检查 NAS 网络和浏览器运行环境"}
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


class AutomaticLogin:
    """账号续登与写入失败恢复；Cookie/密码只保存在 NAS 插件空间。"""

    def _auth_init(self, config: dict) -> None:
        if not hasattr(self, "_login_lock"):
            self._login_lock = threading.RLock()
        self._auto_login = bool(config.get("auto_login", False))
        self._douban_username = str(config.get("douban_username") or "").strip()
        self._douban_password = str(config.get("douban_password") or "")
        old_key = getattr(self, "_credential_key", "")
        self._credential_key = credential_key(self._douban_username, self._douban_password)
        if old_key != self._credential_key or not self._auto_login:
            self._login_qr = None

    def _auth_ready(self) -> bool:
        return self._auto_login and self._enabled and bool(self._douban_username and self._douban_password)

    def _auth_record(self) -> dict:
        record = self.get_data(AUTH_DATA) or {}
        return record if record.get("credential_key") == self._credential_key else {}

    def _auth_public(self) -> dict:
        if not self._auto_login:
            return {"status": "未启用自动续登"}
        if not self._douban_username or not self._douban_password:
            return {"status": "未配置账号", "message": "请填写豆瓣账号和密码后保存"}
        record = self._auth_record()
        public = {key: record.get(key) for key in ("status", "message", "last_login", "checked_at", "next_attempt", "qr_expires_at")
                  if record.get(key) is not None} or {"status": "等待同步触发"}
        qr = getattr(self, "_login_qr", None)
        if qr and qr["key"] == self._credential_key and qr["expires_at"] > time.time() and self._auth_ready():
            public["qr_image"] = qr["image"]
        elif public.get("status") == "正在登录" and getattr(self, "_login_active_key", None) != self._credential_key:
            public.update(status="等待同步触发", message="上次登录会话已结束，下次同步时重新获取登录态")
        elif public.get("status") == "等待扫码":
            public.update(status="扫码已结束", message="扫码会话已结束，请在下一次同步或失败重试时重新获取二维码")
        return public

    def _notify_login_qr(self, image: str, expires_at: float, key: str) -> bool:
        if not self._auth_ready() or key != self._credential_key:
            return False
        self._login_qr = {"key": key, "image": image, "expires_at": expires_at}
        message = "账号登录未成功，请打开豆瓣 App 扫描登录二维码，并在手机上确认。二维码最多有效 6 分钟，确认后继续同步。"
        self.save_data(AUTH_DATA, {"credential_key": key, "status": "等待扫码", "message": message,
                                  "qr_expires_at": expires_at, "next_attempt": 0})
        try:
            from app.schemas.types import MessageType
            self.post_message(mtype=MessageType.Plugin, title="豆瓣观看同步：请扫码登录", text=message, image=image)
        except Exception:
            self.save_data(AUTH_DATA, {"credential_key": key, "status": "等待扫码", "message": "二维码通知提交失败，请打开插件的豆瓣登录设置扫码确认",
                                      "qr_expires_at": expires_at, "next_attempt": 0})
            logger.warn("豆瓣登录二维码通知提交失败，可在插件设置中查看二维码")
        return True

    def _save_auth_result(self, result: dict, key: str, previous: dict) -> bool:
        if not self._auth_ready() or key != self._credential_key:
            return False  # 配置在后台登录期间被修改/禁用，不保存迟到结果。
        stamp = time.time()
        successful = result.get("status") == "登录正常" and bool(result.get("cookie"))
        record = {"credential_key": key, "status": result.get("status") or "登录失败",
                  "message": result.get("message") or "", "checked_at": stamp,
                  "next_attempt": 0 if successful else stamp + (21600 if result.get("status") == "需要验证" else 1800)}
        if successful:
            record.update(cookie=result["cookie"], user_agent=result.get("user_agent") or "", last_login=stamp)
            logger.info("豆瓣自动续登成功，新 Cookie 已保存")
        else:
            # 失效 Cookie 不再用于写入。登录异常与 Cookie 内容均不回显。
            logger.warn("豆瓣自动续登未成功：" + record["status"])
            if previous.get("status") != record["status"]:
                messages = getattr(self, "systemmessage", None)
                if messages:
                    messages.put("豆瓣观看同步：" + record["message"])
        self.save_data(AUTH_DATA, record)
        return successful

    def _renew_login(self, client) -> bool:
        with self._login_lock:
            if not self._auth_ready():
                client.last_error = "自动续登需要启用插件并填写豆瓣账号密码"
                return False
            record = self._auth_record()
            # 另一个同步任务已经换了 Cookie，复用而不是再次登录。
            if record.get("cookie") and client._parse_cookie(record["cookie"]) != client.cookies:
                self._install_login(client, record)
                return True
            if float(record.get("next_attempt") or 0) > time.time():
                client.last_error = record.get("message") or "豆瓣登录失败，冷却结束后在下次同步时重试"
                return False
            key, username, password = self._credential_key, self._douban_username, self._douban_password
            self.save_data(AUTH_DATA, {**record, "credential_key": key, "status": "正在登录", "message": ""})
            self._login_active_key = key
            try:
                result = browser_login(username, password,
                                       on_qr=lambda image, expiry: self._notify_login_qr(image, expiry, key),
                                       should_continue=lambda: self._auth_ready() and key == self._credential_key)
            except Exception:
                result = {"status": "登录失败", "message": "登录流程未完成，请稍后重试"}
            finally:
                self._login_qr = None
                self._login_active_key = None
            if self._save_auth_result(result, key, record):
                self._install_login(client, self._auth_record())
                return True
            client.last_error = self._auth_public().get("message") or "自动续登未完成"
            return False

    @staticmethod
    def _install_login(client, record: dict) -> None:
        client.cookies = client._parse_cookie(record.get("cookie") or "")
        client._ck = client.cookies.get("ck") or ""
        client.user_agent = record.get("user_agent") or ""

    def _prepare_login(self, client, force: bool = False) -> bool:
        if not self._auto_login:
            return True
        if not self._auth_ready():
            client.last_error = "自动续登需要启用插件并填写豆瓣账号密码"
            return False
        with self._login_lock:
            record = self._auth_record()
            if record.get("cookie") or client.cookies:
                if record.get("cookie"):
                    self._install_login(client, record)
                if not force and record.get("status") == "登录正常" and time.time() - float(record.get("checked_at") or 0) < 600:
                    return True
                valid = client.login_state()
                if valid is True:
                    self.save_data(AUTH_DATA, {**record, "credential_key": self._credential_key,
                                              "cookie": client.cookie_header(), "user_agent": client.user_agent,
                                              "status": "登录正常", "message": "", "checked_at": time.time()})
                    return True
                if valid is None:
                    # 检测异常保留 Cookie；仅写入本身的成功响应才归档。
                    self.save_data(AUTH_DATA, {**record, "status": "检查失败", "message": "暂时无法检查豆瓣登录态，下次同步时再检查"})
                    return True
            return self._renew_login(client)

    def _write_douban(self, client, method: str, *args, **kwargs) -> bool:
        if not self._prepare_login(client):
            return False
        if getattr(client, method)(*args, **kwargs):
            return True
        if self._auto_login and client.login_state() is False and self._renew_login(client):
            # 重登后只重试本次操作一次，不重复更新本地档案。
            return bool(getattr(client, method)(*args, **kwargs))
        return False

