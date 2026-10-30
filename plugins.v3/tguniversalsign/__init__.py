import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_TELEGRAM_IMPORT_ERROR = ""


def _import_telegram_deps():
    """
    检测 Telegram 依赖，并记录不含凭据的失败类型。
    """
    global _TELEGRAM_IMPORT_ERROR
    try:
        import socks as socks_mod
        from telethon import TelegramClient as client_cls
        from telethon.errors import FloodWaitError as flood_cls, SessionPasswordNeededError as password_cls
        _TELEGRAM_IMPORT_ERROR = ""
        return socks_mod, client_cls, flood_cls, password_cls
    except ImportError as error:
        _TELEGRAM_IMPORT_ERROR = getattr(error, "name", None) or type(error).__name__
        return None, None, Exception, Exception


socks, TelegramClient, FloodWaitError, SessionPasswordNeededError = _import_telegram_deps()


def _refresh_telegram_deps() -> bool:
    """
    首次加载早于依赖安装时，在下一次使用前重新检测。
    """
    global socks, TelegramClient, FloodWaitError, SessionPasswordNeededError
    if socks is None or TelegramClient is None:
        socks, TelegramClient, FloodWaitError, SessionPasswordNeededError = _import_telegram_deps()
    return socks is not None and TelegramClient is not None

from apscheduler.triggers.cron import CronTrigger
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.security import verify_token
from fastapi import Body, Depends, HTTPException
from app.schemas.types import NotificationType


def require_manager(token=Depends(verify_token)):
    """个人 TG 账号的登录、状态查询与执行入口只对管理员开放。"""
    if not getattr(token, "super_user", False):
        raise HTTPException(status_code=403, detail="仅管理员可以管理 TG万能签到")


class TgUniversalSign(_PluginBase):
    plugin_name = "TG万能签到"
    plugin_desc = "使用 TG 个人账号，按频道逐行执行自定义命令。"
    plugin_icon = "https://raw.githubusercontent.com/gken6/MoviePilot-Plugins/main/icons/tguniversalsign.svg"
    plugin_version = "0.3.5"
    plugin_author = "gken6"
    author_url = "https://github.com/gken6"
    plugin_config_prefix = "tguniversalsign_"
    plugin_order = 50
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _api_id = ""
    _api_hash = ""
    _phone = ""
    _targets = ""
    _commands = ""
    _cron = "0 9 * * *"
    _proxy_type = "none"
    _proxy_host = ""
    _proxy_port = ""
    _reply_wait_seconds = "8"
    _command_interval_seconds = "2"
    _notify = True
    _notify_on_failure = True
    _send_code_once = False
    _login_code = ""
    _login_password = ""
    _last_result = "未执行"
    _phone_code_hash = ""
    _history_keep_days = 7

    @property
    def _data_dir(self) -> Path:
        """新安装使用宿主数据目录；已有旧目录时保留原会话位置。"""
        persistent = Path("/config/plugins/tguniversalsign/data")
        if persistent.is_dir():
            return persistent
        path = self.get_data_path() / "data"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    @property
    def _session_path(self) -> Path:
        return self._data_dir / "personal_account"

    @property
    def _history_path(self) -> Path:
        return self._data_dir / "history.json"

    @staticmethod
    def __now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def __load_history(self) -> List[Dict[str, Any]]:
        try:
            if self._history_path.exists():
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"【TG万能签到】读取历史失败: {e}")
        return []

    def __save_history(self, records: List[Dict[str, Any]]):
        cutoff = datetime.now() - timedelta(days=self._history_keep_days)
        kept = []
        for item in records:
            try:
                if datetime.fromisoformat(str(item.get("created_at"))) < cutoff:
                    continue
            except Exception:
                pass
            kept.append(item)
        try:
            self._history_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"【TG万能签到】保存历史失败: {e}")

    @staticmethod
    def __clean_output(text: str) -> str:
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        # Remove decorative emoji/symbols while retaining normal Chinese punctuation and numbers.
        text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)
        lines, seen = [], set()
        for raw in text.splitlines():
            line = re.sub(r"^[\s•·▪▫◆◇■□●○★☆▶▷➤➜→←↑↓✓✔✕✖⭕]+", "", raw).strip()
            line = re.sub(r"\s+", " ", line)
            if not line or line in {"---", "[空消息]", "[媒体/非文本消息]"} or line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return "\n".join(lines).strip() or "未获取到有效回复"

    def __append_channel_history(self, channel_results: List[Dict[str, Any]], trigger: str):
        records = self.__load_history()
        now_iso = datetime.now().isoformat(timespec="seconds")
        now_text = self.__now_text()
        new_records = []
        for item in channel_results:
            result = self.__clean_output((item.get("result") if item.get("success") else item.get("error") or item.get("result")) or "未获取到有效回复")
            new_records.append({"created_at": now_iso, "time": now_text, "trigger": trigger,
                                "target": str(item.get("target") or "未知目标"),
                                "success": bool(item.get("success")), "duration": round(float(item.get("duration") or 0), 2),
                                "summary": result, "detail": result})
        self.__save_history(new_records + records)
        return self.__format_history()

    def __format_history(self, fallback="未执行") -> str:
        records = self.__load_history()
        if not records:
            return fallback
        lines = []
        for item in records:
            target = str(item.get("target") or "历史任务")
            state = "成功" if item.get("success") else "失败"
            lines += [f"{target} · {state}", f"{item.get('time', '-')} · {float(item.get('duration', 0)):.1f} 秒",
                      self.__clean_output(item.get("summary") or item.get("detail") or "无结果"), ""]
        return "\n".join(lines).rstrip()

    def init_plugin(self, config: dict = None):
        self._last_result = self.__format_history()
        if config:
            for key, default in (("api_id", ""), ("api_hash", ""), ("phone", ""), ("targets", ""), ("commands", ""),
                                 ("cron", "0 9 * * *"), ("proxy_type", "none"), ("proxy_host", ""),
                                 ("proxy_port", ""), ("reply_wait_seconds", "8"), ("command_interval_seconds", "2"),
                                 ("login_code", ""), ("login_password", ""), ("phone_code_hash", "")):
                setattr(self, f"_{key}", str(config.get(key) or default).strip() if key not in {"commands", "targets", "login_password"} else str(config.get(key) or default))
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", True))
            self._notify_on_failure = bool(config.get("notify_on_failure", True))
            self._send_code_once = bool(config.get("send_code_once", False))

        if self._send_code_once:
            self._send_code_once = False
            try:
                self._last_result = "验证码发送成功：" + self.__run_async(self.__send_code())
            except Exception as e:
                self._last_result = f"验证码发送失败：{e}"
            self.__update_config()
        if self._login_code:
            code, self._login_code = self._login_code, ""
            try:
                self._last_result = self.__run_async(self.__login(code))
            except Exception as e:
                self._last_result = f"验证码登录失败：{e}"
            self.__update_config()
        if self._login_password:
            password, self._login_password = self._login_password, ""
            try:
                self._last_result = self.__run_async(self.__login_2fa(password))
            except Exception as e:
                self._last_result = f"二步验证登录失败：{e}"
            self.__update_config()
        if self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            self.run_tasks()

    def get_state(self):
        return bool(self._enabled)

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        routes = [
            {"path": "/send_code", "endpoint": self.api_send_code, "auth": "bear", "methods": ["POST"], "summary": "发送 Telegram 登录验证码"},
            {"path": "/login", "endpoint": self.api_login, "auth": "bear", "methods": ["POST"], "summary": "提交 Telegram 登录验证码"},
            {"path": "/login_2fa", "endpoint": self.api_login_2fa, "auth": "bear", "methods": ["POST"], "summary": "提交 Telegram 二步验证密码"},
            {"path": "/status", "endpoint": self.api_status, "auth": "bear", "methods": ["GET"], "summary": "查看 Telegram 登录状态"},
            {"path": "/run", "endpoint": self.api_run, "auth": "bear", "methods": ["POST"], "summary": "立即执行全部配置任务"},
        ]
        routes.append({"path": "/ui/overview", "endpoint": self.api_ui_overview, "auth": "bear", "methods": ["GET"]})
        for route in routes:
            route["dependencies"] = [Depends(require_manager)]
        return routes

    def get_service(self):
        if self._enabled and self._cron:
            return [{"id": "TgUniversalSign", "name": "TG万能签到", "trigger": CronTrigger.from_crontab(self._cron), "func": self.run_tasks, "kwargs": {}}]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        field = lambda model, label, **kw: {"component": "VTextField", "props": {"model": model, "label": label, **kw}}
        col = lambda content, md=4: {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [content]}
        return [{"component": "VForm", "content": [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "目标与动作严格按物理行对应，同一行用半角分号 ; 分隔。支持普通文本或 发送:文字、点击:按钮文字、等待:秒数。例如：/start;等待:2;点击:签到。命令空行表示跳过对应目标，请保留占位空行。"}},
            {"component": "VRow", "content": [col({"component": "VSwitch", "props": {"model": "enabled", "label": "启用定时任务"}}), col({"component": "VSwitch", "props": {"model": "onlyonce", "label": "保存后立即执行一次"}}), col(field("cron", "Cron 定时", placeholder="0 9 * * *"))]},
            {"component": "VRow", "content": [col(field("api_id", "Telegram API ID"), 3), col(field("api_hash", "Telegram API Hash", type="password"), 3), col(field("phone", "手机号", placeholder="+8613800000000"), 3), col(field("command_interval_seconds", "命令间隔秒数", placeholder="2"), 3)]},
            {"component": "VRow", "content": [
                col({"component": "VTextarea", "props": {"model": "targets", "label": "目标频道/机器人（每行一个）", "placeholder": "@bot_a\n-1001234567890\nhttps://t.me/example", "rows": 5, "auto-grow": True}}, 6),
                col({"component": "VTextarea", "props": {"model": "commands", "label": "对应命令（每行对应一个目标；分号分隔）", "placeholder": "/checkin;/bonus\n/sign", "rows": 5, "auto-grow": True}}, 6)]},
            {"component": "VRow", "content": [col({"component": "VSelect", "props": {"model": "proxy_type", "label": "代理类型", "items": [{"title": "SOCKS5", "value": "socks5"}, {"title": "HTTP", "value": "http"}, {"title": "不使用", "value": "none"}]}}, 3), col(field("proxy_host", "代理地址"), 3), col(field("proxy_port", "代理端口"), 3), col(field("reply_wait_seconds", "每条命令等待回复秒数", placeholder="8"), 3)]},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "首次使用先保存 API ID、API Hash 和手机号，再开启发送验证码并保存，收到后填写验证码并保存；若提示二步验证，再填写密码保存。已有有效登录会话时无需重复登录。"}},
            {"component": "VRow", "content": [col({"component": "VSwitch", "props": {"model": "send_code_once", "label": "保存后发送验证码"}}), col(field("login_code", "验证码")), col(field("login_password", "二步验证密码", type="password"))]},
            {"component": "VRow", "content": [col({"component": "VSwitch", "props": {"model": "notify", "label": "成功后发送 MP 通知"}}), col({"component": "VSwitch", "props": {"model": "notify_on_failure", "label": "失败也发送 MP 通知"}})]},
            {"component": "VRow", "content": [col({"component": "VTextarea", "props": {"model": "last_result", "label": "运行历史（最近7天）", "readonly": True, "rows": 10, "auto-grow": True}}, 12)]}
        ]}], self.__config_dict()

    def get_page(self):
        records = self.__load_history()
        content = [{"component": "VCardTitle", "text": f"运行历史（{len(records)}）"}]
        for item in records:
            ok = bool(item.get("success"))
            content.append({"component": "VCardText", "content": [
                {"component": "div", "props": {"class": "d-flex align-center flex-wrap ga-2 mb-2"}, "content": [
                    {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "success" if ok else "error"}, "text": "成功" if ok else "失败"},
                    {"component": "strong", "text": str(item.get("target") or "历史任务")},
                    {"component": "span", "props": {"class": "text-body-2 text-medium-emphasis"}, "text": f"{item.get('time', '-')} · {float(item.get('duration', 0)):.1f} 秒"}
                ]},
                {"component": "div", "props": {"class": "text-body-2", "style": "white-space: pre-wrap"}, "text": self.__clean_output(item.get("summary") or item.get("detail") or "无结果")}
            ]})
            content.append({"component": "VDivider"})
        if not records:
            content.append({"component": "VCardText", "text": "暂无运行记录"})
        return [{"component": "VCard", "content": content}]

    def stop_service(self):
        pass

    def __config_dict(self):
        return {"enabled": self._enabled, "onlyonce": False, "api_id": self._api_id, "api_hash": self._api_hash,
                "phone": self._phone, "targets": self._targets, "commands": self._commands, "cron": self._cron,
                "proxy_type": self._proxy_type, "proxy_host": self._proxy_host, "proxy_port": self._proxy_port,
                "reply_wait_seconds": self._reply_wait_seconds, "command_interval_seconds": self._command_interval_seconds,
                "notify": self._notify, "notify_on_failure": self._notify_on_failure, "send_code_once": False,
                "login_code": "", "login_password": "", "last_result": self._last_result, "phone_code_hash": self._phone_code_hash}

    def __update_config(self):
        self.update_config(self.__config_dict())

    def __proxy(self):
        """
        读取已配置的代理，不连接 Telegram。
        """
        if self._proxy_type == "none" or not self._proxy_host or not self._proxy_port:
            return None
        if socks is None and not _refresh_telegram_deps():
            raise RuntimeError("依赖尚未就绪：" + (_TELEGRAM_IMPORT_ERROR or "Telethon / PySocks"))
        return (socks.SOCKS5 if self._proxy_type == "socks5" else socks.HTTP, self._proxy_host, int(self._proxy_port))

    def __client(self):
        """
        确认依赖后创建客户端，保留已有登录会话。
        """
        if not _refresh_telegram_deps():
            raise RuntimeError("依赖尚未就绪：" + (_TELEGRAM_IMPORT_ERROR or "Telethon / PySocks") + "；依赖安装完成后可直接重试")
        if not self._api_id or not self._api_hash:
            raise RuntimeError("请先配置 Telegram API ID / API Hash")
        return TelegramClient(str(self._session_path), int(self._api_id), self._api_hash, proxy=self.__proxy())

    @staticmethod
    def __run_async(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        if loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        return loop.run_until_complete(coro)

    def __seconds(self, value, default, low, high):
        try:
            return max(low, min(high, float(value)))
        except Exception:
            return default

    def __task_rows(self):
        # split("\n") preserves interior and trailing physical empty lines; never filter before pairing.
        targets = self._targets.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        commands = self._commands.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        rows, errors = [], []
        for index in range(max(len(targets), len(commands))):
            target = targets[index].strip() if index < len(targets) else ""
            raw = commands[index] if index < len(commands) else ""
            cmds = [x.strip() for x in raw.split(";") if x.strip()]
            if not target and cmds:
                errors.append(f"第 {index + 1} 行有命令但目标为空")
            elif target and cmds:
                rows.append((index + 1, target, cmds))
        if errors:
            raise RuntimeError("；".join(errors))
        if not rows:
            raise RuntimeError("没有可执行任务，请检查目标和命令的逐行对应关系")
        return rows

    @staticmethod
    def __message_text(message):
        return (getattr(message, "message", None) or "").strip() or ("[媒体/非文本消息]" if getattr(message, "media", None) else "[空消息]")

    @staticmethod
    def __normalize_button_text(text):
        # Keep CJK/letters/numbers, discard emoji, punctuation and whitespace.
        return "".join(re.findall(r"[\w\u3400-\u9fff]+", str(text or "").casefold(), flags=re.UNICODE))

    async def __collect_replies(self, client, entity, after_id, own_command, wait_seconds):
        deadline = asyncio.get_running_loop().time() + wait_seconds
        seen, replies = set(), []
        while asyncio.get_running_loop().time() < deadline:
            async for message in client.iter_messages(entity, min_id=after_id, limit=20):
                mid = getattr(message, "id", 0)
                text = self.__message_text(message)
                if mid and mid > after_id and mid not in seen and text.strip() != own_command.strip():
                    seen.add(mid)
                    replies.append(text)
            if replies:
                await asyncio.sleep(1)
                break
            await asyncio.sleep(1)
        return replies

    async def __find_button(self, client, entity, wanted, limit=20):
        wanted_raw = str(wanted or "").strip()
        wanted_norm = self.__normalize_button_text(wanted_raw)
        if not wanted_norm:
            raise RuntimeError("点击动作缺少按钮文字")
        async for message in client.iter_messages(entity, limit=limit):
            exact, normalized, fuzzy = [], [], []
            for row_index, row in enumerate(getattr(message, "buttons", None) or []):
                for col_index, button in enumerate(row):
                    button_text = str(getattr(button, "text", "") or "").strip()
                    button_norm = self.__normalize_button_text(button_text)
                    item = (message, row_index, col_index, button_text)
                    if button_text.casefold() == wanted_raw.casefold():
                        exact.append(item)
                    elif button_norm == wanted_norm:
                        normalized.append(item)
                    elif wanted_norm in button_norm:
                        fuzzy.append(item)
            # Prefer the newest message containing a match, avoiding duplicate old menus.
            matches = exact or normalized or fuzzy
            if matches:
                if len(matches) > 1:
                    labels = "、".join(dict.fromkeys(x[3] for x in matches[:5]))
                    raise RuntimeError(f"当前菜单按钮匹配不唯一（{labels}），请填写更完整的按钮文字")
                return matches[0]
        raise RuntimeError(f"最近 {limit} 条消息中未找到按钮：{wanted_raw}")

    async def __click_button(self, client, entity, wanted, wait_seconds):
        message, row_index, col_index, actual_text = await self.__find_button(client, entity, wanted)
        old_id = getattr(message, "id", 0)
        result = await message.click(row_index, col_index)
        # Callback bots may edit the same menu message instead of sending a new one.
        await asyncio.sleep(min(1.5, wait_seconds))
        edited = await client.get_messages(entity, ids=old_id)
        edited_text = self.__message_text(edited) if edited else ""
        old_text = self.__message_text(message)
        replies = await self.__collect_replies(client, entity, old_id, "", wait_seconds)
        if edited_text and edited_text != old_text:
            replies.insert(0, edited_text)
        if not replies and result is not None:
            result_text = str(getattr(result, "message", "") or result).strip()
            if result_text:
                replies.append(result_text)
        return actual_text, replies

    @staticmethod
    def __target_to_peer(target: str):
        value = str(target or "").strip()
        # Telethon only treats numeric chat/channel ids as ids when they are ints.
        # Keep @username/t.me links untouched, but convert -100... group ids.
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return value

    async def __execute(self, rows):
        client = self.__client()
        wait = self.__seconds(self._reply_wait_seconds, 8, 1, 60)
        interval = self.__seconds(self._command_interval_seconds, 2, 0, 120)
        channel_results = []
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram 未登录，请重新完成验证码登录")
            for line_no, target, commands in rows:
                channel_started = time.perf_counter()
                useful_replies, errors = [], []
                try:
                    entity = await client.get_entity(self.__target_to_peer(target))
                except Exception as e:
                    channel_results.append({"target": target, "success": False, "result": "", "error": f"无法访问目标：{e}", "duration": time.perf_counter() - channel_started})
                    continue
                for pos, action in enumerate(commands):
                    try:
                        prefix, sep, value = action.partition(":")
                        action_type = prefix.strip().casefold() if sep else ""
                        value = value.strip()
                        if action_type in {"等待", "wait"}:
                            await asyncio.sleep(self.__seconds(value, 1, 0, 300))
                        elif action_type in {"点击", "click"}:
                            _, replies = await self.__click_button(client, entity, value, wait)
                            # A click is normally the final business action; always discard earlier menu welcomes.
                            useful_replies = replies or []
                        else:
                            text = value if action_type in {"发送", "send"} else action
                            if not text.strip():
                                raise RuntimeError("发送内容为空")
                            msg = await client.send_message(entity, text)
                            replies = await self.__collect_replies(client, entity, getattr(msg, "id", 0), text, wait)
                            if replies:
                                useful_replies.extend(replies)
                    except FloodWaitError as e:
                        errors.append(f"Telegram 限流，需等待 {e.seconds} 秒")
                    except Exception as e:
                        errors.append(f"{action}：{e}")
                    if interval and pos < len(commands) - 1:
                        await asyncio.sleep(interval)
                result = self.__clean_output("\n".join(useful_replies)) if useful_replies else "未获取到有效回复"
                channel_results.append({"target": target, "success": not errors, "result": result,
                                        "error": "\n".join(errors), "duration": time.perf_counter() - channel_started})
        finally:
            await client.disconnect()
        return channel_results

    def __notify_channels(self, channel_results):
        for item in channel_results:
            success = bool(item.get("success"))
            if (success and not self._notify) or (not success and not self._notify_on_failure):
                continue
            target = str(item.get("target") or "未知目标")
            text = self.__clean_output(item.get("result") if success else item.get("error") or item.get("result"))
            try:
                self.post_message(mtype=NotificationType.Plugin,
                                  title=f"TG万能签到 · {target} · {'成功' if success else '失败'}",
                                  text=text[:4000])
            except Exception as e:
                logger.warning(f"【TG万能签到】发送 {target} 通知失败: {e}")

    def run_tasks(self):
        try:
            channel_results = self.__run_async(self.__execute(self.__task_rows()))
        except Exception as e:
            channel_results = [{"target": "配置", "success": False, "result": "", "error": f"执行失败：{e}", "duration": 0}]
        self._last_result = self.__append_channel_history(channel_results, "定时/立即")
        self.__update_config()
        self.__notify_channels(channel_results)
        for item in channel_results:
            logger.info(f"【TG万能签到】【{item.get('target')}】{'成功' if item.get('success') else '失败'}：{item.get('result') or item.get('error')}")
        success = all(item.get("success") for item in channel_results)
        return {"code": 0 if success else 1, "msg": self._last_result}

    async def __status(self):
        client = self.__client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return "未登录"
            me = await client.get_me()
            return f"已登录：{me.id} (@{me.username or '-'})"
        finally:
            await client.disconnect()

    async def __send_code(self):
        if not self._phone:
            raise RuntimeError("请先配置手机号")
        client = self.__client()
        await client.connect()
        try:
            sent = await client.send_code_request(self._phone)
            self._phone_code_hash = sent.phone_code_hash
            self.__update_config()
            return "验证码已发送"
        finally:
            await client.disconnect()

    async def __login(self, code):
        if not self._phone_code_hash:
            raise RuntimeError("请先发送验证码")
        client = self.__client()
        await client.connect()
        try:
            try:
                await client.sign_in(phone=self._phone, code=str(code).strip().replace(" ", ""), phone_code_hash=self._phone_code_hash)
            except SessionPasswordNeededError:
                return "需要二步验证密码"
            me = await client.get_me()
            self._phone_code_hash = ""
            return f"登录成功：{me.id} (@{me.username or '-'})"
        finally:
            await client.disconnect()

    async def __login_2fa(self, password):
        client = self.__client()
        await client.connect()
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            self._phone_code_hash = ""
            return f"登录成功：{me.id} (@{me.username or '-'})"
        finally:
            await client.disconnect()

    def api_status(self):
        try:
            return {"code": 0, "msg": self.__run_async(self.__status())}
        except Exception as e:
            return {"code": 1, "msg": str(e)}

    def api_send_code(self):
        try:
            return {"code": 0, "msg": self.__run_async(self.__send_code())}
        except Exception as e:
            return {"code": 1, "msg": str(e)}

    def api_login(self, code: str = Body("", embed=True)):
        try:
            return {"code": 0, "msg": self.__run_async(self.__login(code))} if code else {"code": 1, "msg": "缺少 code 参数"}
        except Exception as e:
            return {"code": 1, "msg": str(e)}

    def api_login_2fa(self, password: str = Body("", embed=True)):
        try:
            return {"code": 0, "msg": self.__run_async(self.__login_2fa(password))} if password else {"code": 1, "msg": "缺少 password 参数"}
        except Exception as e:
            return {"code": 1, "msg": str(e)}

    def api_run(self):
        return self.run_tasks()


    def get_render_mode(self):
        """
        使用宿主 Vue 插件面板，配置与历史页面共用一致的界面。

        :return: 渲染类型与静态文件目录。
        """
        return "vue", "frontend/dist/assets"

    def api_ui_overview(self, page: int = 1, search: str = "", state: str = "all") -> dict:
        """
        仅读取本地历史分页及概况，打开界面不会连接 Telegram 或执行任务。

        :param page: 从一开始的历史页码。
        :param search: 目标和回复中的搜索文字。
        :param state: all、success 或 failed。
        :return: 不含账号密钥和会话内容的概况与历史。
        """
        records = self.__load_history()
        needle = str(search or "").strip().casefold()
        selected = [item for item in records
                    if (state not in {"success", "failed"} or bool(item.get("success")) == (state == "success"))
                    and (not needle or needle in " ".join(str(item.get(k) or "") for k in
                                                         ("target", "trigger", "summary", "detail")).casefold())]
        pages = max(1, (len(selected) + 19) // 20)
        page = min(max(int(page or 1), 1), pages)
        rows = []
        for item in selected[(page - 1) * 20:page * 20]:
            row = {key: item.get(key) for key in ("time", "created_at", "target", "trigger", "summary", "detail")}
            row["success"] = bool(item.get("success"))
            try:
                row["duration"] = max(0, float(item.get("duration") or 0))
            except (TypeError, ValueError):
                row["duration"] = 0
            rows.append(row)
        latest = records[0] if records else {}
        return {"records": rows, "total": len(selected), "page": page, "pages": pages,
                "stats": {"total": len(records), "success": sum(bool(r.get("success")) for r in records),
                          "failed": sum(not bool(r.get("success")) for r in records)},
                "latest": {"time": latest.get("time") or latest.get("created_at") or "", "success": latest.get("success")},
                "enabled": self._enabled, "cron": self._cron,
                "account_configured": bool(self._api_id and self._api_hash and self._phone),
                "session_saved": self._session_path.with_suffix(".session").is_file()}
