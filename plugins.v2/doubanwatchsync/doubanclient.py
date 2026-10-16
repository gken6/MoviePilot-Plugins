"""豆瓣接口：负责搜索条目、维护登录态（ck）以及写入书影音档案状态。"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional, Tuple

from app.log import logger
from app.utils.http import RequestUtils

# 豆瓣接口地址
_DOUBAN_HOME = "https://www.douban.com/"
_DOUBAN_INTEREST = "https://movie.douban.com/j/subject/%s/interest"

# 状态取值：do=在看，collect=看过；只同步已经发生的观看。
_STATUS_ALIAS = {
    "do": "do",
    "doing": "do",
    "collect": "collect",
    "done": "collect",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"
)


class DoubanClient:
    """封装豆瓣搜索与状态写入，所有请求显式超时，失败返回空值而不抛异常。"""

    def __init__(self, cookie: str = "", timeout: int = 15) -> None:
        """
        :param cookie: 豆瓣 cookie 字符串，可为空（空时仅能搜索，不能写入）
        :param timeout: 网络请求超时秒数
        """
        self._timeout = timeout
        self.cookies: Dict[str, str] = self._parse_cookie(cookie)
        self._ck = self.cookies.get("ck") or ""
        self.last_error = ""
        self.user_agent = ""

    # ---------------- 对外能力 ----------------

    def has_login(self) -> bool:
        """判断当前是否具备写入豆瓣的登录态。"""
        return bool(self.cookies)

    def login_state(self) -> Optional[bool]:
        """只读检查电影站登录态；None 表示网络/风控异常，不能据此认定过期。"""
        if not self.cookies.get("dbcl2"):
            return False
        try:
            from bs4 import BeautifulSoup
            from urllib.parse import urlsplit
            response = RequestUtils(headers=self._build_base_headers(), timeout=self._timeout).get_res(
                "https://movie.douban.com/")
            if response is None:
                return None
            if response.status_code == 401:
                return False
            if response.status_code != 200:
                return None
            address = str(getattr(response, "url", "") or "")
            if address and urlsplit(address).hostname == "accounts.douban.com" and "/passport/login" in urlsplit(address).path:
                return False
            soup = BeautifulSoup(response.text, "html.parser")
            if soup.select_one("#db-global-nav .nav-user-account"):
                return True
            if soup.select_one('#db-global-nav a[href*="accounts.douban.com/passport/login"]'):
                return False
        except Exception:
            pass
        return None

    def search(self, title: str, media_type: str = "TV") -> Tuple[Optional[str], Optional[str]]:
        """
        搜索豆瓣影视条目，返回 (条目标题, subject_id)。
        优先使用 MP 内置的 frodo API（与「豆瓣想看」同通道，反爬宽松），
        不可用时回退网页搜索。
        """
        result = self._search_by_frodo(title, media_type)
        if result is not None:
            return result
        return self._search_by_web(title)

    def set_status(self, subject_id: str, status: str = "do", private: bool = True) -> bool:
        """
        写入豆瓣书影音档案状态。
        :param subject_id: 豆瓣条目 ID
        :param status: do/在看、collect/看过
        :param private: 是否仅自己可见
        """
        self.last_error = ""
        interest = _STATUS_ALIAS.get(status)
        if interest is None:
            self.last_error = "不支持的观看状态，只能同步在看或看过"
            return False
        if not self.cookies:
            self.last_error = "豆瓣登录态为空，请检查 Cookie 或 CookieCloud"
            logger.error("豆瓣 cookie 为空，无法写入状态，请检查插件配置或 CookieCloud")
            return False

        if not self._ck:
            self._refresh_ck()

        payload = {
            "ck": self._ck,
            "interest": interest,
            "rating": "",
            "foldcollect": "U",
            "tags": "",
            "comment": "",
        }
        if private:
            payload["private"] = "on"

        return self._post_interest(subject_id, payload, retry=True)

    def get_subject_image(self, subject_id: str, media_type: str = "TV") -> Optional[str]:
        """读取豆瓣条目的海报地址，取不到返回 None。"""
        try:
            from app.modules.douban.apiv2 import DoubanApi
        except Exception:
            return None

        try:
            api = DoubanApi()
            detail = api.tv_detail(subject_id) if media_type == "TV" else api.movie_detail(subject_id)
        except Exception as error:
            logger.debug(f"读取豆瓣条目 {subject_id} 详情失败：{error}")
            return None

        if not isinstance(detail, dict):
            return None
        pic = detail.get("pic")
        if isinstance(pic, dict):
            for key in ("large", "normal", "medium", "small"):
                value = pic.get(key)
                if value:
                    return str(value)
        for key in ("pic", "cover", "cover_url"):
            value = detail.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None

    @staticmethod
    def is_public_image_url(url: str) -> bool:
        """仅允许官方公开海报域名，不代理媒体服务器凭据或任意地址。"""
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            return (parsed.scheme == "https" and not parsed.username and not parsed.password
                    and parsed.port in (None, 443) and not parsed.query
                    and (host == "image.tmdb.org" or host == "img.doubanio.com"
                         or (host.startswith("img") and host[3:].split(".")[0].isdigit()
                             and host.endswith(".doubanio.com") and host.count(".") == 2)))
        except (TypeError, ValueError):
            return False

    def fetch_image_data_uri(self, url: str, prefer_small: bool = True) -> Optional[str]:
        """匿名取回豆瓣/TMDB 小海报，避免浏览器防盗链与网络差异。"""
        if not self.is_public_image_url(url):
            return None
        candidates = []
        if prefer_small:
            small = url.replace("/l_ratio_poster/", "/s_ratio_poster/") \
                       .replace("/m_ratio_poster/", "/s_ratio_poster/")
            if urlsplit(url).hostname == "image.tmdb.org":
                parts = url.split("/")
                if len(parts) > 6 and parts[3:5] == ["t", "p"]:
                    parts[5] = "w185"
                    small = "/".join(parts)
            if small != url:
                candidates.append(small)
        candidates.append(url)
        for target in candidates:
            result = self._download_image(target)
            if result:
                return result
        return None

    def _download_image(self, url: str) -> Optional[str]:
        """公开图片请求不携带 Cookie，不跟随重定向，只缓存栅格图片。"""
        if not self.is_public_image_url(url):
            return None
        headers = {"User-Agent": _USER_AGENT}
        if urlsplit(url).hostname.endswith("doubanio.com"):
            headers["Referer"] = "https://movie.douban.com/"
        try:
            response = RequestUtils(headers=headers, timeout=self._timeout).get_res(url, allow_redirects=False)
            if response is None or response.status_code != 200:
                return None
            content = getattr(response, "content", None)
            if not content or len(content) > 2 * 1024 * 1024:
                return None
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(content)) as image:
                content_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(image.format)
                if not content_type or image.width * image.height > 20_000_000:
                    return None
                image.verify()
            return f"data:{content_type};base64," + base64.b64encode(content).decode("ascii")
        except Exception:
            return None

    def _post_interest(self, subject_id: str, payload: Dict[str, Any], retry: bool = False) -> bool:
        """提交状态写入请求，403 时刷新 ck 后重试一次。"""
        headers = self._build_headers(subject_id)
        try:
            response = RequestUtils(headers=headers, timeout=self._timeout).post_res(
                _DOUBAN_INTEREST % subject_id, data=payload)
        except Exception:
            self.last_error = "请求豆瓣失败，请稍后重试"
            logger.error(self.last_error)
            return False

        if response is None:
            self.last_error = "豆瓣无响应，请稍后重试"
            logger.error(f"写入豆瓣状态失败：无响应（可能被拦截或超时）")
            return False

        if response.status_code == 403 and retry:
            logger.warn("豆瓣返回 403，刷新 ck 后重试")
            self._refresh_ck()
            payload["ck"] = self._ck
            return self._post_interest(subject_id, payload, retry=False)

        if response.status_code != 200:
            self.last_error = f"豆瓣返回 HTTP {response.status_code}，请检查登录态或稍后重试"
            logger.error(f"写入豆瓣状态失败，状态码：{response.status_code}")
            return False

        try:
            result = response.json()
        except Exception:
            self.last_error = "豆瓣返回了无法解析的响应，未确认同步成功"
            logger.error("豆瓣响应解析失败")
            return False

        # bool 是 int 的子类：False == 0，但豆瓣的 false 表示失败。
        if isinstance(result, dict) and type(result.get("r")) is int and result["r"] == 0:
            return True
        self.last_error = "豆瓣未确认写入成功，请检查条目、所选状态及登录态"
        logger.error(self.last_error)
        return False

    def _refresh_ck(self) -> None:
        """从豆瓣首页获取 ck，失败时保留旧值，保证登录态不被清空。"""
        old_ck = self._ck
        try:
            response = RequestUtils(headers=self._build_base_headers(), timeout=self._timeout).get_res(_DOUBAN_HOME)
        except Exception as error:
            logger.error(f"刷新豆瓣 ck 失败：{error}")
            return
        if response is None:
            logger.error("刷新豆瓣 ck 失败：无响应")
            return

        cookie = SimpleCookie()
        try:
            cookie.load(response.headers.get("Set-Cookie", ""))
        except Exception:
            cookie = SimpleCookie()

        ck = cookie.get("ck")
        if ck and ck.value and ck.value != '"deleted"':
            self._ck = ck.value
            self.cookies["ck"] = ck.value
        elif not old_ck:
            self._ck = ""

    def _search_by_frodo(self, title: str, media_type: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """使用 frodo API 搜索，接口不可用返回 None，无结果返回 (None, None)。"""
        try:
            from app.modules.douban.apiv2 import DoubanApi
        except Exception as error:
            logger.debug(f"豆瓣 frodo API 不可用，回退网页搜索：{error}")
            return None

        try:
            api = DoubanApi()
            result = api.tv_search(title) if media_type == "TV" else api.movie_search(title)
        except Exception as error:
            logger.warn(f"豆瓣 API 搜索 {title} 失败，回退网页搜索：{error}")
            return None

        for item in self._iter_items(result):
            subject_id = self._pick_text(item, "id")
            subject_title = self._pick_text(item, "title")
            if subject_id:
                logger.debug(f"豆瓣命中：{subject_title} {subject_id}")
                return subject_title or title, subject_id
        logger.info(f"豆瓣未找到「{title}」对应的影视条目")
        return None, None

    def _search_by_web(self, title: str) -> Tuple[Optional[str], Optional[str]]:
        """回退方案：解析豆瓣网页搜索结果。"""
        url = f"https://www.douban.com/search?cat=1002&q={title}"
        try:
            response = RequestUtils(headers=self._build_base_headers(), timeout=self._timeout).get_res(url)
        except Exception as error:
            logger.error(f"豆瓣网页搜索 {title} 失败：{error}")
            return None, None
        if response is None or response.status_code != 200:
            logger.error(f"豆瓣网页搜索 {title} 失败：无响应或状态码异常")
            return None, None

        try:
            from bs4 import BeautifulSoup
        except Exception:
            logger.error("豆瓣网页搜索需要 BeautifulSoup，当前环境不可用")
            return None, None

        soup = BeautifulSoup(response.text, "html.parser")
        for div in soup.select("div.result"):
            link = div.find("a", href=True)
            if not link or "subject" not in link["href"]:
                continue
            subject_id = ""
            parts = [part for part in link["href"].rstrip("/").split("/") if part]
            if len(parts) >= 2 and parts[-2] == "subject":
                subject_id = parts[-1]
            if subject_id.isdigit():
                return link.get_text(strip=True) or title, subject_id
        return None, None

    @staticmethod
    def _iter_items(result: Any):
        """兼容不同版本的 frodo 返回结构，逐个产出条目对象。"""
        if not isinstance(result, dict):
            return []
        items = result.get("items")
        if not isinstance(items, list):
            return []
        targets = []
        for item in items:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            targets.append(target if isinstance(target, dict) else item)
        return targets

    @staticmethod
    def _pick_text(data: Dict[str, Any], key: str) -> Optional[str]:
        """读取并清洗条目字段。"""
        value = data.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_cookie(cookie: str) -> Dict[str, str]:
        """把 cookie 字符串（支持 key=value; 形式或 JSON）解析为字典。"""
        if not cookie:
            return {}
        text = cookie.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v}
            except Exception:
                pass
        try:
            parsed = SimpleCookie()
            parsed.load(text)
            return {k: v.value for k, v in parsed.items()}
        except Exception:
            return {}

    def _build_base_headers(self) -> Dict[str, str]:
        """构造豆瓣通用请求头。"""
        return {
            "User-Agent": self.user_agent or _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Cookie": self.cookie_header(),
        }

    def _build_headers(self, subject_id: str) -> Dict[str, str]:
        """构造状态写入请求头。"""
        headers = self._build_base_headers()
        headers.update({
            "Referer": f"https://movie.douban.com/subject/{subject_id}/",
            "Origin": "https://movie.douban.com",
            "Host": "movie.douban.com",
        })
        return headers

    def cookie_header(self) -> str:
        """把当前 cookie 拼成请求头字符串。"""
        return ";".join(f"{key}={value}" for key, value in self.cookies.items())

    def get_subject_info(self, subject_id: str, media_type: str = "TV") -> Optional[Dict[str, Any]]:
        """读取人工编号对应的真实标题和封面；未取得条目详情时不写入。"""
        fallback = None
        try:
            from app.modules.douban.apiv2 import DoubanApi
            api = DoubanApi()
            detail = api.tv_detail(subject_id) if media_type == "TV" else api.movie_detail(subject_id)
            if isinstance(detail, dict) and detail.get("title"):
                pic = detail.get("pic") or {}
                image = next((pic.get(key) for key in ("large", "normal", "medium", "small") if pic.get(key)), "") if isinstance(pic, dict) else ""
                fallback = {"title": str(detail["title"]), "image": str(image), "episodes_count": detail.get("episodes_count") or 0}
                if media_type != "TV" or fallback["episodes_count"]:
                    return fallback
        except Exception:
            pass
        try:
            from bs4 import BeautifulSoup
            response = RequestUtils(headers=self._build_headers(subject_id), timeout=self._timeout).get_res(
                f"https://movie.douban.com/subject/{subject_id}/")
            if response is None or response.status_code != 200:
                return fallback
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.select_one('h1 span[property="v:itemreviewed"]')
            image = soup.select_one('#mainpic img')
            if title and title.get_text(strip=True):
                import re
                info = soup.select_one('#info')
                count = re.search(r"集数\s*[:：]\s*(\d+)", info.get_text(" ", strip=True)) if info else None
                return {"title": title.get_text(strip=True), "image": str(image.get("src") or "") if image else "",
                        "episodes_count": int(count.group(1)) if count else 0}
        except Exception:
            pass
        return fallback

    def remove_status(self, subject_id: str) -> bool:
        """仅按豆瓣官方页面脚本中可核实的撤销端点取消旧标记；无法核实时保留待处理。"""
        import re
        from urllib.parse import urljoin, urlsplit
        self.last_error = ""
        if not self.cookies:
            self.last_error = "豆瓣登录态不可用"
            return False
        try:
            from bs4 import BeautifulSoup
            url = f"https://movie.douban.com/subject/{subject_id}/"
            response = RequestUtils(headers=self._build_headers(subject_id), timeout=self._timeout).get_res(url)
            if response is None or response.status_code != 200:
                self.last_error = "无法读取豆瓣撤销接口"
                return False
            soup = BeautifulSoup(response.text, "html.parser")
            sources = [node.get_text() for node in soup.find_all("script") if not node.get("src")]
            # Static assets never receive the user's Cookie.
            for script in soup.find_all("script", src=True)[:16]:
                script_url = urljoin(url, script.get("src") or "")
                host = urlsplit(script_url).hostname or ""
                if not (host == "movie.douban.com" or host.endswith(".doubanio.com")):
                    continue
                if "interest" not in script_url and "/f/movie/" not in script_url and "/f/shire/" not in script_url:
                    continue
                result = RequestUtils(headers={"User-Agent": _USER_AGENT, "Referer": url}, timeout=self._timeout).get_res(script_url)
                if result is not None and result.status_code == 200 and len(result.content) <= 2 * 1024 * 1024:
                    sources.append(result.text)
            suffix = None
            for source in sources:
                # Official shire/movie scripts use a concatenated subject route.
                match = re.search(r'''["']/j/subject/["']\s*\+\s*[\w.]+\s*\+\s*["'](/(?:removeinterest|remove_interest|remove))["']''', source)
                if match:
                    suffix = match.group(1)
                    break
                exact = re.search(r'''["'](/j/subject/''' + re.escape(subject_id) + r'''/(?:removeinterest|remove_interest|remove))["']''', source)
                if exact:
                    suffix = exact.group(1).rsplit("/", 1)[-1]
                    suffix = "/" + suffix
                    break
            if not suffix:
                self.last_error = "未能核实豆瓣官方撤销端点"
                return False
            if not self._ck:
                self._refresh_ck()
            endpoint = f"https://movie.douban.com/j/subject/{subject_id}{suffix}"
            for retry in range(2):
                result = RequestUtils(headers=self._build_headers(subject_id), timeout=self._timeout).post_res(
                    endpoint, data={"ck": self._ck})
                if result is not None and result.status_code == 403 and retry == 0:
                    self._refresh_ck()
                    continue
                if result is not None and result.status_code == 200:
                    body = result.json()
                    if isinstance(body, dict) and type(body.get("r")) is int and body["r"] == 0:
                        return True
                break
        except Exception:
            self.last_error = "撤销请求未完成"
        if not self.last_error:
            self.last_error = "豆瓣未确认撤销成功"
        return False
