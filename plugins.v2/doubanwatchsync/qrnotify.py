"""二维码通知兼容层：企业微信自建应用使用图片消息，沿用宿主路由。"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

from app.log import logger


from .compat import routed_messages as _routed_messages, notification_userid


def _download_qr(image: str, directory: Path) -> Path:
    """仅下载豆瓣官方二维码，临时文件在本次通知结束后删除。"""
    from app.utils.http import RequestUtils
    from .auth import _qr_image_url
    if not _qr_image_url(image):
        raise ValueError("unsupported QR image")
    response = RequestUtils(timeout=20).get_res(image)
    if response is None or response.status_code != 200 or not _qr_image_url(str(response.url)):
        raise ValueError("QR download failed")
    return _save_qr_png(response.content, directory)


def _save_qr_png(data: bytes, directory: Path) -> Path:
    """校验浏览器生成的二维码 PNG，不使用截图或整页内容。"""
    from PIL import Image
    if not data or len(data) > 2 * 1024 * 1024:
        raise ValueError("invalid QR image size")
    with Image.open(io.BytesIO(data)) as picture:
        suffix = {"PNG": ".png", "JPEG": ".jpg"}.get(picture.format)
        if not suffix or not (64 <= picture.width <= 1024 and 64 <= picture.height <= 1024):
            raise ValueError("unsupported QR image format")
        picture.verify()
    target = directory / ("douban-login" + suffix)
    target.write_bytes(data)
    target.chmod(0o600)
    return target


def _send_wechat_image(client, path: Path, userid) -> bool:
    """复用 MP 企业微信凭据、代理和 Token；不修改宿主或全局通知对象。"""
    upload = getattr(client, "_upload_temp_media", None)
    send = getattr(client, "_WeChat__post_request", None)
    endpoint = getattr(client, "_send_msg_url", None)
    app_id = getattr(client, "_appid", None)
    if not callable(upload) or not callable(send) or not endpoint or not app_id:
        return False  # 当前宿主没有纯图片兼容接口，不退回图文链接。
    media_id = upload(path, media_type="image")
    if not media_id:
        return False
    return send(endpoint, {"touser": userid or "@all", "msgtype": "image", "agentid": app_id,
                           "image": {"media_id": media_id}}) is True


def send_qr_notifications(plugin, image: str, text: str, *, qr_png: bytes | None = None) -> dict:
    """只向启用且允许插件消息的渠道发送；微信消息正文不包含链接。"""
    from app.helper.notification import NotificationHelper
    from app.schemas import Notification as Message
    from app.schemas.types import NotificationType as MessageType
    sent = failed = skipped = 0
    failures = []
    seen = set()
    with tempfile.TemporaryDirectory(prefix="douban-login-qr-") as directory:
        qr_path = None
        for service in NotificationHelper().iterate_module_instances():
            conf = service.config
            if not conf or not conf.enabled or service.name in seen:
                continue
            seen.add(service.name)
            stage = "route"
            try:
                if service.type != "wechat":
                    plugin.post_message(source=service.name, mtype=MessageType.Plugin,
                                        title="豆瓣观看同步：请扫码登录", text=text, image=image, link=image)
                    sent += 1  # 通用接口只确认提交，具体送达仍由 MP 负责。
                    continue
                module = service.module
                message = Message(source=service.name, mtype=MessageType.Plugin,
                                  title="豆瓣观看同步：请扫码登录", text=text)
                delivered = False
                for routed in _routed_messages(plugin.chain, message):
                    if not module.check_message(routed, service.name):
                        skipped += 1
                        continue
                    client = module.get_instance(service.name)
                    if not client:
                        failed += 1
                        continue
                    if qr_path is None:
                        stage = "download"
                        qr_path = _save_qr_png(qr_png, Path(directory)) if qr_png is not None else _download_qr(image, Path(directory))
                    stage = "recipient"
                    userid = notification_userid(module, routed, "wechat_userid")
                    stage = "image_delivery"
                    if _send_wechat_image(client, qr_path, userid):
                        sent += 1
                        delivered = True
                    else:
                        failed += 1
                        failures.append({"stage": stage, "error_type": "NotConfirmed"})
                if delivered:
                    logger.info("豆瓣登录二维码已通过 MP 微信渠道发送为图片消息")
            except Exception as error:
                failed += 1
                failures.append({"stage": stage, "error_type": type(error).__name__})
                logger.warn("豆瓣二维码图片通知未成功，可在插件设置中扫码")
    return {"sent": sent, "failed": failed, "skipped": skipped, "failures": failures}
