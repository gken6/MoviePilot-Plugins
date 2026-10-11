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


def browser_login(username: str, password: str) -> dict:
    """在独立无头浏览器中登录；不注入现有 Cookie，不自动操作验证码。"""
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
        page.click(".account-tab-account")
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        page.click(".account-form-field-submit .btn-account")
        # 豆瓣官方页面：成功后跳转；失败留在表单，或进入验证页面。
        # 不保存截图、页面正文、响应或密码，也不把其内容写入日志。
        try:
            page.wait_for_function("""() => location.pathname !== '/passport/login' ||
                [...document.querySelectorAll('.account-form-error')].some(e => e.innerText.trim()) ||
                [...document.querySelectorAll('iframe[src*="captcha"], .geetest_panel')]
                    .some(e => e.getBoundingClientRect().height > 0)""", timeout=30000)
        except Exception:
            pass
        candidate = {str(c.get("name")): str(c.get("value")) for c in context.cookies()
                     if str(c.get("domain") or "").lstrip(".") in {"douban.com", "movie.douban.com"}
                     and c.get("value") and c.get("name") in {"dbcl2", "ck"}}
        if candidate.get("dbcl2"):
            page.goto(MOVIE_HOME, wait_until="domcontentloaded", timeout=30000)
            if urlsplit(page.url).hostname == "movie.douban.com" and page.query_selector("#db-global-nav .nav-user-account"):
                values = {}
                # 根域的登录 Cookie 可供电影站使用，电影站同名 Cookie 优先。
                cookies = sorted(context.cookies(), key=lambda c: str(c.get("domain") or "").lstrip(".") == "movie.douban.com")
                for cookie in cookies:
                    if str(cookie.get("domain") or "").lstrip(".") in {"douban.com", "movie.douban.com"} and cookie.get("value") and cookie.get("path", "/") == "/":
                        values[str(cookie["name"])] = str(cookie["value"])
                if values.get("dbcl2") and values.get("ck"):
                    return {"status": "登录正常", "cookie": "; ".join(f"{k}={v}" for k, v in values.items()),
                            "user_agent": page.evaluate("navigator.userAgent")}
        # 只把页面文本用于分类，返回预定义消息，防止豆瓣回显账号进入日志。
        text = page.inner_text("body")
        if any(word in text for word in ("验证码", "短信验证", "安全验证", "异常登录", "滑动", "拖动", "安全检测")) or "abnormal" in page.url:
            return {"status": "需要验证", "message": "豆瓣要求安全验证，自动续登暂停；请完成验证后恢复登录"}
        return {"status": "登录失败", "message": "豆瓣未确认登录成功，请检查账号密码或稍后重试"}
    except Exception:
        # 浏览器异常可能带有页面/参数，不输出异常内容。
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
        self._credential_key = credential_key(self._douban_username, self._douban_password)

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
        return {key: record.get(key) for key in ("status", "message", "last_login", "checked_at", "next_attempt")
                if record.get(key) is not None} or {"status": "等待同步触发"}

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
            result = browser_login(username, password)
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

