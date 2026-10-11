"""发布验证：模拟 MP 和 Telegram 边界，不登录、不发送真实消息。"""
import asyncio
import copy
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = (Path(os.environ['TG_SIGN_TEST_ROOT']) if os.environ.get('TG_SIGN_TEST_ROOT')
        else Path(__file__).resolve().parents[3] / 'plugins.v3' / 'tguniversalsign')


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    names = ('app', 'app.sdk', 'app.sdk.logging', 'app.sdk.plugin', 'app.sdk.security',
             'app.schemas', 'app.schemas.types', 'apscheduler', 'apscheduler.triggers',
             'apscheduler.triggers.cron', 'fastapi', 'socks', 'telethon', 'telethon.errors')
    modules = {name: types.ModuleType(name) for name in names}
    class Base:
        def get_data_path(self):
            return tmp_path / 'plugin-data'
        def update_config(self, config):
            self.saved = copy.deepcopy(config)
        post_message = Mock(side_effect=AssertionError('unexpected live notification'))
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            super().__init__(detail)
    class FloodWaitError(Exception):
        seconds = 10
    modules['app.sdk.plugin']._PluginBase = Base
    modules['app.sdk.logging'].logger = Mock()
    modules['app.sdk.security'].verify_token = lambda: None
    modules['app.schemas.types'].NotificationType = types.SimpleNamespace(Plugin='plugin')
    modules['apscheduler.triggers.cron'].CronTrigger = types.SimpleNamespace(from_crontab=lambda text: text)
    modules['fastapi'].Depends = lambda function: function
    modules['fastapi'].Body = lambda default, **kwargs: default
    modules['fastapi'].HTTPException = HTTPException
    modules['socks'].SOCKS5 = 2; modules['socks'].HTTP = 3
    modules['telethon'].TelegramClient = Mock(side_effect=AssertionError('unexpected Telegram client'))
    modules['telethon.errors'].FloodWaitError = FloodWaitError
    modules['telethon.errors'].SessionPasswordNeededError = type('SessionPasswordNeededError', (Exception,), {})
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location('_tg_sign_publication', ROOT / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    # Legacy-path inspection stays in the temporary test directory.
    monkeypatch.setattr(module, 'Path', lambda value: tmp_path / 'legacy-data' if str(value) ==
                        '/config/plugins/tguniversalsign/data' else Path(value))
    return module.TgUniversalSign(), module, tmp_path


def test_fresh_install_has_no_private_defaults_or_auto_run(runtime):
    plugin, module, _ = runtime
    plugin.init_plugin({})
    _, defaults = plugin.get_form()
    assert not any(defaults[key] for key in ('api_id', 'api_hash', 'phone', 'targets', 'commands',
                                             'proxy_host', 'proxy_port', 'login_code', 'login_password'))
    assert defaults['proxy_type'] == 'none'
    assert not defaults['enabled'] and not defaults['onlyonce']
    module.TelegramClient.assert_not_called()


def test_session_and_history_are_under_host_data_path(runtime):
    plugin, _, temp = runtime
    assert plugin._session_path == temp / 'plugin-data/data/personal_account'
    assert plugin._history_path == temp / 'plugin-data/data/history.json'
    assert 'plugins.v3' not in plugin._session_path.parts


def test_existing_legacy_directory_keeps_session_location(runtime):
    plugin, _, temp = runtime
    (temp / 'legacy-data').mkdir()
    assert plugin._session_path == temp / 'legacy-data/personal_account'


def test_task_rows_preserve_blank_command_alignment(runtime):
    plugin, _, _ = runtime
    plugin._targets = '@bot_a\r\n@skip\r\n@bot_c'
    plugin._commands = '/first;等待:2\r\n\r\n/third'
    assert plugin._TgUniversalSign__task_rows() == [(1, '@bot_a', ['/first', '等待:2']), (3, '@bot_c', ['/third'])]


def test_orphan_command_is_rejected_before_connecting(runtime):
    plugin, module, _ = runtime
    plugin._targets = '@bot_a\n'; plugin._commands = '/first\n/second'
    with pytest.raises(RuntimeError, match='第 2 行'):
        plugin._TgUniversalSign__task_rows()
    module.TelegramClient.assert_not_called()


@pytest.mark.parametrize('target,expected', [('-1001234567890', -1001234567890), ('123', 123),
                                           ('@bot_a', '@bot_a'), ('https://t.me/example', 'https://t.me/example')])
def test_numeric_chat_id_is_integer(runtime, target, expected):
    assert runtime[0]._TgUniversalSign__target_to_peer(target) == expected


def test_button_matching_accepts_decorative_emoji_and_rejects_ambiguity(runtime):
    plugin, _, _ = runtime
    message = types.SimpleNamespace(buttons=[[types.SimpleNamespace(text='✅ 签到')]])
    async def messages(*args, **kwargs):
        yield message
    client = types.SimpleNamespace(iter_messages=messages)
    result = asyncio.run(plugin._TgUniversalSign__find_button(client, 'entity', '签到'))
    assert result[1:3] == (0, 0)
    message.buttons = [[types.SimpleNamespace(text='每日签到'), types.SimpleNamespace(text='补签签到')]]
    with pytest.raises(RuntimeError, match='匹配不唯一'):
        asyncio.run(plugin._TgUniversalSign__find_button(client, 'entity', '签到'))


def test_offline_action_sequence_sends_configured_text_and_final_button_result(runtime, monkeypatch):
    plugin, _, _ = runtime
    client = types.SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
                                   is_user_authorized=AsyncMock(return_value=True),
                                   get_entity=AsyncMock(return_value='entity'),
                                   send_message=AsyncMock(return_value=types.SimpleNamespace(id=1)))
    monkeypatch.setattr(plugin, '_TgUniversalSign__client', lambda: client)
    collect = AsyncMock(return_value=['欢迎'])
    click = AsyncMock(return_value=('签到', ['签到完成']))
    monkeypatch.setattr(plugin, '_TgUniversalSign__collect_replies', collect)
    monkeypatch.setattr(plugin, '_TgUniversalSign__click_button', click)
    plugin._command_interval_seconds = '0'
    result = asyncio.run(plugin._TgUniversalSign__execute([(1, '-1001234567890', ['发送:/start', '点击:签到'])]))
    client.get_entity.assert_awaited_once_with(-1001234567890)
    client.send_message.assert_awaited_once_with('entity', '/start')
    assert result[0]['success'] and result[0]['result'] == '签到完成'
    client.disconnect.assert_awaited_once()


def test_unauthorized_account_never_sends(runtime, monkeypatch):
    plugin, _, _ = runtime
    client = types.SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
                                   is_user_authorized=AsyncMock(return_value=False), send_message=AsyncMock())
    monkeypatch.setattr(plugin, '_TgUniversalSign__client', lambda: client)
    with pytest.raises(RuntimeError, match='未登录'):
        asyncio.run(plugin._TgUniversalSign__execute([(1, '@bot_a', ['/start'])]))
    client.send_message.assert_not_called(); client.disconnect.assert_awaited_once()


def test_history_retention_and_output_cleanup(runtime):
    plugin, _, _ = runtime
    records = [{'created_at': (datetime.now() - timedelta(days=8)).isoformat(), 'summary': 'old'},
               {'created_at': datetime.now().isoformat(), 'summary': 'new'}]
    plugin._TgUniversalSign__save_history(records)
    assert [row['summary'] for row in json.loads(plugin._history_path.read_text())] == ['new']
    assert plugin._TgUniversalSign__clean_output('✅ 完成\n完成\n[空消息]') == '完成'


def test_account_management_endpoints_require_admin(runtime):
    plugin, module, _ = runtime
    routes = plugin.get_api()
    assert len(routes) == 5
    assert all(route['dependencies'] == [module.require_manager] for route in routes)
    assert all(route['methods'] == (['GET'] if route['path'] == '/status' else ['POST']) for route in routes)
    with pytest.raises(module.HTTPException) as error:
        module.require_manager(types.SimpleNamespace(super_user=False))
    assert error.value.status_code == 403
    module.require_manager(types.SimpleNamespace(super_user=True))
