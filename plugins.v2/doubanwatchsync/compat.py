"""MoviePilot V2.15.6 的浏览器、延后任务和消息收件人兼容层。"""
from __future__ import annotations
import copy
import threading
from app.log import logger


def launch_browser_context(headless=True):
    from app.helper.browser import BrowserSessionHelper
    return BrowserSessionHelper._launch_context(headless=headless)


class OnceTasks:
    def __init__(self):
        self._lock = threading.RLock()
        self._timers = {}

    def add_plugin_once_job(self, plugin_id, job_id, func, name, delay_seconds=1):
        key = (plugin_id, job_id)
        with self._lock:
            self.remove_plugin_once_job(plugin_id, job_id)
            def run():
                with self._lock:
                    if self._timers.get(key) is not timer:
                        return
                    self._timers.pop(key, None)
                try:
                    func()
                except Exception as error:
                    logger.warn(f"豆瓣观看同步后台任务失败：{type(error).__name__}")
            timer = threading.Timer(max(0, delay_seconds), run)
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def remove_plugin_once_job(self, plugin_id, job_id=None):
        with self._lock:
            keys = [key for key in self._timers if key[0] == plugin_id and (job_id is None or key[1] == job_id)]
            for key in keys:
                self._timers.pop(key).cancel()


scheduler_sdk = OnceTasks()


def routed_messages(chain, message):
    """与 V2 ChainBase.post_message 相同的通知开关及管理员/用户隔离。"""
    from app.core.config import settings
    from app.db.user_oper import UserOper
    from app.helper.service import ServiceConfigHelper
    normalize = getattr(chain, '_normalize_notification_for_dispatch', None)
    dispatch = normalize(message) if callable(normalize) else copy.deepcopy(message)
    action = ServiceConfigHelper.get_notification_switch(dispatch.mtype) if not dispatch.userid and dispatch.mtype else None
    if not action:
        yield dispatch
        return
    admin_sent = False
    users = UserOper()
    for item in action.split(','):
        target = copy.deepcopy(dispatch)
        if item == 'admin' and not admin_sent:
            target.targets = users.get_settings(settings.SUPERUSER)
            admin_sent = True
        elif item == 'user' and target.username:
            target.targets = users.get_settings(target.username)
            if target.targets is None:
                if admin_sent:
                    continue
                target.targets = users.get_settings(settings.SUPERUSER)
                admin_sent = True
            elif target.username == settings.SUPERUSER:
                admin_sent = True
        else:
            if not admin_sent:
                yield dispatch
            return
        yield target


def notification_userid(module, message, key):
    """保持 V2 模块的绑定目标规则，缺失个人绑定时不扩大为群发。"""
    getter = getattr(module, 'get_notification_userid', None)
    if callable(getter):
        return getter(message, key)
    userid = message.userid
    if not userid and message.targets is not None:
        userid = message.targets.get(key)
        if not userid:
            raise ValueError('通知收件人未绑定')
    return userid
