"""隔离测试：所有宿主存储、媒体服务器和豆瓣请求均在边界模拟。"""
import copy
import os
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = (Path(os.environ["WATCHSYNC_TEST_ROOT"]) if os.environ.get("WATCHSYNC_TEST_ROOT")
        else Path(__file__).resolve().parents[3] / 'plugins.v2' / 'doubanwatchsync')


@pytest.fixture
def runtime(monkeypatch):
    names = ('app', 'app.schemas', 'app.schemas.types', 'app.sdk', 'app.sdk.events',
             'app.sdk.logging', 'app.sdk.plugin', 'app.sdk.services', 'app.sdk.scheduler',
             'app.sdk.network', 'app.sdk.security', 'app.plugins', 'app.plugins.doubanwatchsync', 'fastapi',
             'apscheduler', 'apscheduler.triggers', 'apscheduler.triggers.cron')
    modules = {name: types.ModuleType(name) for name in names}
    class Base:
        def __init__(self):
            self.data = {}
            self.config = {}
        def get_data(self, key):
            return copy.deepcopy(self.data.get(key))
        def save_data(self, key, value):
            self.data[key] = copy.deepcopy(value)
        def get_config(self):
            return copy.deepcopy(self.config)
        def update_config(self, value):
            self.config = copy.deepcopy(value)
    class Helper:
        def iterate_module_instances(self):
            return []
    class EventManager:
        def register(self, *args):
            return lambda function: function
    modules['app.schemas'].WebhookEventInfo = type('WebhookEventInfo', (), {})
    modules['app.schemas.types'].MessageType = types.SimpleNamespace(Plugin='plugin')
    modules['app.schemas.types'].EventType = types.SimpleNamespace(WebhookMessage=1, PluginAction=2)
    modules['app.sdk.events'].Event = type('Event', (), {})
    modules['app.sdk.events'].eventmanager = EventManager()
    modules['app.sdk.logging'].logger = Mock()
    modules['app.sdk.plugin']._PluginBase = Base
    modules['app.sdk.services'].MediaServerHelper = Helper
    modules['app.sdk.network'].RequestUtils = Mock(side_effect=AssertionError('unexpected network'))
    modules['app.plugins.doubanwatchsync'].__path__ = [str(ROOT)]
    modules['app.sdk.security'].verify_token = lambda: None
    modules['fastapi'].Body = lambda *args: None
    modules['fastapi'].Depends = lambda function: function
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            super().__init__(detail)
    modules['fastapi'].HTTPException = HTTPException
    modules['apscheduler.triggers.cron'].CronTrigger = types.SimpleNamespace(from_crontab=lambda value: value)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    # V2 的导入入口独立隔离，不依赖宿主 V3 SDK。
    legacy = {'app.log': modules['app.sdk.logging'], 'app.core.event': modules['app.sdk.events'],
              'app.core.security': modules['app.sdk.security'], 'app.helper.mediaserver': modules['app.sdk.services'],
              'app.helper.notification': modules['app.sdk.services'], 'app.utils.http': modules['app.sdk.network']}
    for namespace in ['app.core', 'app.helper', 'app.utils']:
        monkeypatch.setitem(sys.modules, namespace, types.ModuleType(namespace))
    for name, module in legacy.items():
        monkeypatch.setitem(sys.modules, name, module)
    modules['app.plugins']._PluginBase = Base
    modules['app.schemas.types'].NotificationType = modules['app.schemas.types'].MessageType
    modules['app.schemas'].Notification = lambda **kwargs: types.SimpleNamespace(userid=None, targets=None, username=None, **kwargs)
    # Imports are restored by monkeypatch along with SDK stubs after every test.
    name = '_test_watchsync'
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    plugin_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, plugin_module)
    for suffix in ('doubanclient', 'mediaserver', 'management', 'auth', 'qrnotify', 'embycookie', 'compat'):
        monkeypatch.setitem(sys.modules, name + '.' + suffix, None)
        sys.modules.pop(name + '.' + suffix)
    spec.loader.exec_module(plugin_module)
    plugin = plugin_module.DoubanWatchSync()
    plugin._record_locks = {}
    queued = []
    plugin._schedule_once = lambda *args: queued.append(args)
    plugin._load_cookie = Mock(return_value='simulated-cookie')
    plugin.init_plugin({'enabled': True, 'users': 'mp-user', 'server': ['mp-server']})
    return plugin, sys.modules[name + '.management'], plugin_module, queued


def server_body(**kwargs):
    return dict(action='save', name='direct-a', url='https://emby.example.test', username='direct-user', password='secret', **kwargs)


def add_server(runtime):
    plugin, management, module, queued = runtime
    assert plugin.api_server(server_body())['success']
    identifier = next(iter(plugin.data[management.SERVERS]))
    return identifier


def archive_record(plugin, key='movie@direct-a', subject='100'):
    plugin.save_data('archive', {key: {'title': 'wrong title', 'source_title': 'source title',
                                    'subject_id': subject, 'subject_name': 'wrong title', 'type': '电影',
                                    'status': 'collect', 'server_name': 'direct-a', 'user_name': 'direct-user',
                                    'season': 0, 'episode': 0, 'timestamp': '2026-10-11 12:00:00'}})
    return key


def fake_client(monkeypatch, management, *, remove=True, write=True, detail=True, kind="MOV"):
    client = management.DoubanClient(cookie='dbcl2=local; ck=local')
    client.login_state = Mock(return_value=True)
    client.get_subject_info = Mock(return_value={'title': 'correct title', 'image': 'https://img.example.test/cover.jpg', 'media_type': kind} if detail else None)
    client.set_status = Mock(return_value=write)
    client.remove_status = Mock(return_value=remove)
    calls = Mock()
    calls.attach_mock(client.set_status, 'set_status')
    calls.attach_mock(client.remove_status, 'remove_status')
    client.method_calls = calls.method_calls
    client.last_error = 'simulated removal failure' if not remove else ''
    monkeypatch.setattr(management, 'DoubanClient', lambda **kwargs: client)
    return client


def test_add_server_is_async_and_redacts_credentials(runtime):
    identifier = add_server(runtime)
    plugin, management, _, queued = runtime
    result = plugin.api_management()['servers'][0]
    assert result['status'] == '正在获取token'
    assert not {'password', 'token', 'user_id'} & set(result)
    assert queued[-1][0] == 'direct_check_' + identifier
    assert plugin.config == {}  # Never writes MP or even global plugin settings.


@pytest.mark.parametrize('update', [dict(url='ftp://bad'), dict(url='https://u:p@host'), dict(name='a@b'), dict(username='')])
def test_invalid_server_is_rejected(runtime, update):
    body = server_body(); body.update(update)
    assert runtime[0].api_server(body)['success'] is False
    assert runtime[0].api_management()['servers'] == []


def test_duplicate_names_and_mp_names_are_rejected(runtime):
    add_server(runtime)
    assert not runtime[0].api_server(server_body())['success']
    runtime[0]._server_items = lambda: [{'value': 'mp-server'}]
    body = server_body(); body['name'] = 'mp-server'
    assert not runtime[0].api_server(body)['success']


def test_auth_success_and_counts(runtime):
    identifier = add_server(runtime)
    plugin, management, _, _ = runtime
    plugin._authenticate_direct = Mock(return_value={'token': 'test-token', 'user_id': 'test-userid', 'username': 'direct-user'})
    plugin._direct_counts = Mock(return_value={'movies': 42, 'series': 9})
    plugin._check_server(identifier)
    row = plugin.api_management()['servers'][0]
    assert (row['status'], row['movies'], row['series']) == ('正常', 42, 9)
    assert plugin._direct_server_configs()[0]['token'] == 'test-token'


def test_auth_failure_is_visible(runtime):
    identifier = add_server(runtime)
    plugin = runtime[0]
    plugin._authenticate_direct = Mock(return_value=None)
    plugin._check_server(identifier)
    assert plugin.api_management()['servers'][0]['status'] == '获取失败'
    assert plugin._direct_server_configs() == []


def test_disabled_server_never_authenticates_or_polls(runtime):
    identifier = add_server(runtime)
    plugin = runtime[0]
    plugin.api_server({'action': 'toggle', 'id': identifier, 'enabled': False})
    plugin._authenticate_direct = Mock(side_effect=AssertionError('disabled account accessed'))
    plugin._check_server(identifier)
    assert plugin.api_management()['servers'][0]['status'] == '未启用'
    assert plugin._direct_server_configs() == []
    assert plugin._direct_emby_sessions({'enabled': False}) == []


def test_late_auth_result_cannot_reenable_disabled_server(runtime):
    identifier = add_server(runtime)
    plugin = runtime[0]
    def authenticate(_):
        plugin.api_server({'action': 'toggle', 'id': identifier, 'enabled': False})
        return {'token': 'late', 'user_id': 'late', 'username': 'direct-user'}
    plugin._authenticate_direct = authenticate
    plugin._direct_counts = lambda _: {'movies': 1, 'series': 1}
    plugin._check_server(identifier)
    assert plugin.api_management()['servers'][0]['status'] == '未启用'
    assert plugin._direct_server_configs() == []


def test_edit_preserves_password_and_renames_history(runtime):
    identifier = add_server(runtime)
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    body = server_body(); body.update(id=identifier, password='', name='direct-b')
    assert plugin.api_server(body)['success']
    assert plugin.data[management.SERVERS][identifier]['password'] == 'secret'
    assert 'movie@direct-b' in plugin.data['archive']
    assert key not in plugin.data['archive']


def test_delete_preserves_history_and_prevents_queued_direct_sync(runtime):
    identifier = add_server(runtime)
    plugin = runtime[0]
    archive_record(plugin)
    assert plugin.api_server({'action': 'delete', 'id': identifier})['success']
    assert plugin.data['archive']
    assert not plugin._direct_record_allowed({'direct_id': identifier})


def test_legacy_accounts_migrate_once_and_new_changes_survive_config_save(runtime):
    plugin, management, _, _ = runtime
    plugin.data.pop(management.SERVERS)
    config = {'enabled': True, 'users': 'mp-user', 'server': ['mp-server'],
              'direct_accounts': 'legacy|https://example.test|old-user|old-secret',
              'direct_servers': 'legacy|https://example.test|old-token|old-id|old-user'}
    plugin.init_plugin(config)
    row = next(iter(plugin.data[management.SERVERS].values()))
    assert row['password'] == 'old-secret' and row['token'] == 'old-token'
    plugin.api_server({'action': 'delete', 'id': row['id']})
    plugin.init_plugin(config)
    assert plugin.api_management()['servers'] == []
    assert plugin._users == 'mp-user' and plugin._server == ['mp-server']


def test_direct_poll_uses_its_account_without_mp_allowlist(runtime):
    plugin = runtime[0]
    payload = {'title': 'film', 'media_type': 'MOV', 'season': 0, 'episode': 0, 'user_name': 'direct-user'}
    assert plugin._direct_payload_allowed(payload, {'name': 'direct-a', 'username': 'direct-user'})
    assert not plugin._is_target_user('direct-user')  # Original MP filter stays in place.
    payload['user_name'] = 'other-user'
    assert not plugin._direct_payload_allowed(payload, {'name': 'direct-a', 'username': 'direct-user'})


def test_mp_event_routing_and_playback_rule_are_preserved(runtime):
    plugin, _, module, _ = runtime
    info = module.WebhookEventInfo(); info.server_name = 'mp-server'
    assert plugin._event_server_name(info) == 'mp-server'
    info.server_name = 'different'; assert plugin._event_server_name(info) is None
    assert plugin._is_finished_event({'event': 'PlaybackPause', 'percentage': 5}) is True
    assert plugin._is_target_user('mp-user') is True


def test_correct_id_writes_new_then_removes_old_and_updates_archive(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    client = fake_client(monkeypatch, management)
    assert plugin.api_match({'key': key, 'subject_id': '200'})['success']
    plugin._run_correction(key)
    assert plugin.data['archive'][key]['subject_id'] == '200'
    assert plugin.data['archive'][key]['subject_name'] == 'correct title'
    assert plugin.data[management.CORRECTIONS] == {}
    assert plugin._manual_subject(key) == '200'
    client.set_status.assert_called_once_with('200', 'collect', True)
    client.remove_status.assert_called_once_with('100')
    assert [call[0] for call in client.method_calls].index('set_status') < [call[0] for call in client.method_calls].index('remove_status')


def test_invalid_manual_id_and_unknown_subject_do_not_write(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    assert not plugin.api_match({'key': key, 'subject_id': 'https://bad'})['success']
    client = fake_client(monkeypatch, management, detail=False)
    assert plugin.api_match({'key': key, 'subject_id': '200'})['success']
    plugin._run_correction(key)
    client.set_status.assert_not_called(); client.remove_status.assert_not_called()
    assert plugin.data['archive'][key]['subject_id'] == '100'
    assert plugin.api_management()['archive'][0]['operation']


def test_new_write_failure_preserves_old_marker(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    client = fake_client(monkeypatch, management, write=False)
    plugin.api_match({'key': key, 'subject_id': '200'}); plugin._run_correction(key)
    client.remove_status.assert_not_called()
    assert plugin.data['archive'][key]['subject_id'] == '100'
    assert plugin.data[management.CORRECTIONS][key]['state'] == 'failed'


def test_partial_cleanup_failure_is_visible_and_retry_only_removes_old(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    client = fake_client(monkeypatch, management, remove=False)
    plugin.api_match({'key': key, 'subject_id': '200'}); plugin._run_correction(key)
    operation = plugin.data[management.CORRECTIONS][key]
    assert operation['new_synced'] and operation['cleanup_ids'] == ['100']
    assert '旧标记待撤销' in operation['message']
    client.remove_status.return_value = True
    plugin.api_match({'action': 'retry', 'key': key}); plugin._run_correction(key)
    assert plugin.data[management.CORRECTIONS] == {}
    client.set_status.assert_called_once()


def test_same_subject_resync_does_not_delete_marker(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    client = fake_client(monkeypatch, management)
    plugin.api_match({'key': key, 'subject_id': '100'}); plugin._run_correction(key)
    client.set_status.assert_called_once(); client.remove_status.assert_not_called()


@pytest.mark.parametrize('media_type,status,valid', [
    ('MOV', 'do', False), ('电影', 'do', False), ('MOV', 'wish', False),
    ('电影', 'collect', True), ('TV', 'do', True), ('电视剧', 'collect', True),
    ('TV', 'wish', False), ('TV', 'unknown', False),
])
def test_manual_status_is_validated_by_type(runtime, monkeypatch, media_type, status, valid):
    plugin, management, _, _ = runtime
    plugin._tv_completion = Mock(return_value={"complete":True,"known":True,"episodes":[1,2],"played":[1,2],"expected":2})
    key = 'pending'
    plugin.save_data('pending', {key: {'title': 'source', 'type': media_type, 'status': 'do'}})
    client = fake_client(monkeypatch, management, kind='TV' if media_type in ('TV','电视剧') else 'MOV')
    result = plugin.api_match({'key': key, 'subject_id': '200', 'status': status})
    assert result['success'] is valid
    if valid:
        plugin._run_correction(key)
        expected = 'collect' if media_type in ('TV', '电视剧') else status
        client.set_status.assert_called_once_with('200', expected, True)
        assert plugin.data['archive'][key]['status'] == expected
        client.remove_status.assert_not_called()
    else:
        client.set_status.assert_not_called()


def test_manual_same_subject_can_change_in_progress_to_watched(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    plugin._tv_completion = Mock(return_value={"complete":True,"known":True,"episodes":[1,2],"played":[1,2],"expected":2})
    key = archive_record(plugin)
    archive = plugin.get_data('archive'); archive[key].update(type='电视剧', status='do')
    plugin.save_data('archive', archive)
    client = fake_client(monkeypatch, management, kind='TV')
    assert plugin.api_match({'key': key, 'subject_id': '100', 'status': 'collect'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('100', 'collect', True)
    client.remove_status.assert_not_called()
    assert plugin.data['archive'][key]['status'] == 'collect'


@pytest.mark.parametrize('response,success', [
    ({'r': 0}, True), ({'r': False}, False), ({'r': True}, False),
    ({'r': '0'}, False), ({'r': 1}, False), ({}, False), ([], False), (None, False),
])
def test_douban_write_response_requires_integer_zero(runtime, monkeypatch, response, success):
    module = sys.modules['_test_watchsync.doubanclient']
    http = Mock(status_code=200); http.json.return_value = response
    monkeypatch.setattr(module, 'RequestUtils', Mock(return_value=Mock(post_res=Mock(return_value=http))))
    client = module.DoubanClient(cookie='ck=simulated')
    assert client.set_status('200', 'collect') is success
    assert bool(client.last_error) is (not success)


def test_missing_match_preserves_resolved_mp_watch_status(runtime, monkeypatch):
    plugin, _, _, _ = runtime
    monkeypatch.setattr(plugin, '_reader', lambda *args, **kwargs: Mock(get_douban_id=Mock(return_value='')))
    monkeypatch.setattr(plugin, '_user_id_for', lambda *args: None)
    monkeypatch.setattr(plugin, '_resolve_status', lambda *args: 'collect')
    monkeypatch.setattr(plugin, '_match_subject', lambda *args: (None, None))
    plugin._sync_item_original({'title': 'source', 'media_type': 'MOV', 'server_name': 'mp-server'})
    assert next(iter(plugin.data['pending'].values()))['status'] == 'collect'


def test_client_rejects_wish_without_any_request(runtime, monkeypatch):
    module = sys.modules['_test_watchsync.doubanclient']
    network = Mock(side_effect=AssertionError('unexpected network'))
    monkeypatch.setattr(module, 'RequestUtils', network)
    client = module.DoubanClient(cookie='ck=simulated')
    assert client.set_status('200', 'wish') is False
    assert client.last_error
    network.assert_not_called()


def test_shared_old_subject_is_not_deleted_for_other_valid_records(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    archive = plugin.get_data('archive'); archive['other@direct-b'] = dict(archive[key]); plugin.save_data('archive', archive)
    client = fake_client(monkeypatch, management)
    plugin.api_match({'key': key, 'subject_id': '200'}); plugin._run_correction(key)
    client.remove_status.assert_not_called()
    assert plugin.data['archive']['other@direct-b']['subject_id'] == '100'


def test_ignore_pending_stops_matching_without_cookie_access(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = 'missing@direct-a'
    plugin.save_data('pending', {key: {'title': 'missing', 'type': 'MOV', 'status': 'collect', 'server_name': 'direct-a'}})
    plugin._load_cookie = Mock(side_effect=AssertionError('no-entry should not access cookies'))
    client = fake_client(monkeypatch, management)
    plugin.api_match({'key': key, 'action': 'ignore'}); plugin._run_correction(key)
    assert plugin.data['pending'] == {}
    assert key in plugin.data[management.IGNORED] and plugin._manual_guard(key)
    client.set_status.assert_not_called(); client.remove_status.assert_not_called()
    plugin._sync_item_original = Mock(side_effect=AssertionError('ignored item matched'))
    plugin._sync_item({'title': 'missing', 'media_type': 'MOV', 'season': 0, 'server_name': 'direct-a'})
    plugin._load_cookie.assert_not_called()


def test_ignore_synced_removes_old_marker_and_dashboard_entry(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    client = fake_client(monkeypatch, management)
    plugin.api_match({'key': key, 'action': 'ignore'}); plugin._run_correction(key)
    assert plugin.data['archive'] == {}
    assert key in plugin.data[management.IGNORED]
    client.remove_status.assert_called_once_with('100')


def test_restore_no_entry_reopens_matching_and_clears_poll_markers(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = 'missing@direct-a'
    plugin.save_data(management.IGNORED, {key: {'title': 'missing', 'type': 'MOV', 'server_name': 'direct-a', 'subject_id': '100'}})
    plugin.save_data(management.OVERRIDES, {key: {'ignore': True}})
    plugin.save_data('poll', {'direct|direct-a|user|item|do': 'now', 'mp|marker': 'now'})
    assert plugin.api_match({'key': key, 'action': 'restore'})['success']
    assert not plugin._manual_guard(key)
    assert plugin.data['pending'][key]['subject_id'] == ''
    assert plugin.data['poll'] == {'mp|marker': 'now'}


def test_matching_from_no_entry_moves_back_to_archive(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = 'missing@direct-a'
    plugin.save_data(management.IGNORED, {key: {'title': 'missing', 'type': 'MOV', 'status': 'collect', 'server_name': 'direct-a'}})
    fake_client(monkeypatch, management)
    plugin.api_match({'key': key, 'subject_id': '200'}); plugin._run_correction(key)
    assert key not in plugin.data[management.IGNORED]
    assert plugin.data['archive'][key]['subject_id'] == '200'


def test_corrected_entry_is_used_for_future_automatic_sync(runtime):
    plugin, management, _, _ = runtime
    key = 'film@direct-a'; plugin.save_data(management.OVERRIDES, {key: {'subject_id': '200'}})
    assert plugin._manual_subject(key) == '200'
    assert not plugin._manual_guard(key)


def test_dashboard_excludes_no_entry_and_reads_corrected_id(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin, subject='200')
    plugin.data['archive'][key]['image'] = ''
    plugin.save_data(management.IGNORED, {'missing': {'title': 'missing'}})
    _, attrs, elements = plugin.get_dashboard('archive')
    assert attrs['subtitle'] == '共 1 部'
    import json
    assert 'subject/200/' in json.dumps(elements)
    assert 'missing' not in json.dumps(elements)


def test_inline_image_cache_does_not_permanently_hide_later_posters(runtime, monkeypatch):
    plugin, _, module, _ = runtime
    plugin._image_map = {str(index): 'cached' for index in range(20)}
    plugin._image_budget = 8; plugin._image_failed = set(); plugin._inline_limit = 20
    client = Mock(); client.fetch_image_data_uri.return_value = 'data:image/jpeg;base64,abc'
    monkeypatch.setattr(module, 'DoubanClient', lambda: client)
    assert plugin._resolve_image({'image': 'https://img.doubanio.com/21.jpg'}).startswith('data:')


def test_removal_fails_closed_when_official_endpoint_cannot_be_verified(runtime, monkeypatch):
    _, _, module, _ = runtime
    client_module = sys.modules['_test_watchsync.doubanclient']
    response = types.SimpleNamespace(status_code=200, text='<html></html>', content=b'<html></html>')
    transport = Mock(); transport.get_res.return_value = response
    monkeypatch.setattr(client_module, 'RequestUtils', lambda **kwargs: transport)
    # Minimal parser stub keeps tests dependency-free.
    soup = types.SimpleNamespace(find_all=lambda *args, **kwargs: [])
    monkeypatch.setitem(sys.modules, 'bs4', types.SimpleNamespace(BeautifulSoup=lambda *args: soup))
    client = client_module.DoubanClient('ck=test; dbcl2=fake')
    assert client.remove_status('100') is False
    transport.post_res.assert_not_called()
    assert '未能核实' in client.last_error


def test_cookie_read_exception_leaves_retryable_failure(runtime):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    plugin._load_cookie.side_effect = RuntimeError('simulated failure')
    assert plugin.api_match({'key': key, 'subject_id': '200'})['success']
    plugin._run_correction(key)
    assert plugin.data[management.CORRECTIONS][key]['state'] == 'failed'


@pytest.mark.parametrize('url', ['http://[bad', 'http://emby.test:99999', 'http://emby.test:bad'])
def test_malformed_url_is_a_friendly_validation_error(runtime, url):
    body = server_body(); body['url'] = url
    assert runtime[0].api_server(body)['success'] is False


def test_migrated_history_stops_after_server_deletion(runtime):
    plugin, management, _, _ = runtime
    identifier = add_server(runtime)
    key = archive_record(plugin)
    plugin._management_init({})
    record = plugin.data['archive'][key]
    assert record['direct_id'] == identifier
    plugin.api_server({'action': 'delete', 'id': identifier})
    assert not plugin._direct_record_allowed(record)


def test_rescan_does_not_overwrite_manual_operation(runtime, monkeypatch):
    plugin, management, module, _ = runtime
    key = archive_record(plugin)
    plugin.data['archive'][key].update(type='电视剧', status='do', season=1)
    plugin.data[management.CORRECTIONS] = {key: {'state': 'failed'}}
    client = fake_client(monkeypatch, management)
    monkeypatch.setattr(module, 'DoubanClient', lambda **kw: client)
    result = plugin.rescan_archive()
    assert result['skipped'] == 1
    client.set_status.assert_not_called()


def test_management_api_requires_administrator(runtime):
    plugin, management, _, _ = runtime
    management.require_manager(types.SimpleNamespace(super_user=True))
    with pytest.raises(Exception) as error:
        management.require_manager(types.SimpleNamespace(super_user=False))
    assert error.value.status_code == 403
    assert all(item['dependencies'] for item in plugin.management_api())


def enable_auto(runtime):
    plugin, _, module, _ = runtime
    plugin.init_plugin({'enabled': True, 'auto_login': True, 'douban_username': 'account@example.test',
                        'douban_password': 'simulated-password'})
    return plugin, sys.modules[module.__name__ + '.auth'], sys.modules[module.__name__ + '.doubanclient']


def login_client(client_module, cookie='dbcl2=expired; ck=old'):
    client = client_module.DoubanClient(cookie=cookie)
    client.login_state = Mock(return_value=False)
    client.set_status = Mock(return_value=True)
    client.remove_status = Mock(return_value=True)
    return client


def good_login():
    return {'status': '登录正常', 'cookie': 'dbcl2=fresh; ck=new', 'user_agent': 'browser-test-agent'}


def test_auto_login_has_no_timer_or_initial_login(runtime):
    plugin, auth, _ = enable_auto(runtime)
    assert not any('login' in job[0] for job in runtime[3])
    assert [item['id'] for item in plugin.get_service()] == ['DoubanWatchSync.Retry', 'DoubanWatchSync.PollPlaying']
    assert plugin._auth_public()['status'] == '等待同步触发'
    assert auth.AUTH_DATA not in plugin.data


def test_valid_initial_cookie_is_reused_without_login(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    browser = Mock(side_effect=AssertionError('should not log in'))
    monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm); client.login_state.return_value = True
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    browser.assert_not_called()
    assert plugin.data[auth.AUTH_DATA]['cookie'] == client.cookie_header()
    assert 'last_login' not in plugin.data[auth.AUTH_DATA]


def test_expired_cookie_renews_only_on_write_and_saves_new_cookie(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    browser = Mock(return_value=good_login()); monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    browser.assert_called_once()
    assert client.cookies == {'dbcl2': 'fresh', 'ck': 'new'}
    assert client._build_base_headers()['User-Agent'] == 'browser-test-agent'
    client.set_status.assert_called_once_with('200', 'collect', True)
    report = plugin.api_management()['login']
    assert report['status'] == '登录正常'
    assert not {'cookie', 'credential_key', 'user_agent', 'password'} & report.keys()
    assert 'simulated-password' not in str(report)
    assert 'fresh' not in str(report)
    plugin._cookie = 'dbcl2=expired; ck=old'
    plugin._load_cookie = types.MethodType(runtime[2].DoubanWatchSync._load_cookie, plugin)
    assert plugin._load_cookie() == plugin._cookie  # Explicit hand-filled cookie now takes priority.


def test_write_detects_sudden_expiry_and_retries_once(runtime, monkeypatch):
    import time
    plugin, auth, cm = enable_auto(runtime)
    client = login_client(cm)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'cookie': client.cookie_header(),
                                    'status': '登录正常', 'checked_at': time.time()})
    browser = Mock(return_value=good_login()); monkeypatch.setattr(auth, 'browser_login', browser)
    client.set_status.side_effect = [False, True]
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.set_status.call_count == 2
    browser.assert_called_once()


def test_write_failure_after_successful_login_stays_failure(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    monkeypatch.setattr(auth, 'browser_login', Mock(return_value=good_login()))
    client = login_client(cm)
    client.login_state.side_effect = lambda: client.cookies.get('dbcl2') == 'fresh'
    client.set_status.return_value = False
    assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
    client.set_status.assert_called_once()
    assert plugin.get_data('archive') is None


def test_network_problem_is_not_cookie_expiry(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    browser = Mock(side_effect=AssertionError('should not log in')); monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm); client.login_state.return_value = None; client.set_status.return_value = False
    assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
    browser.assert_not_called()


def test_challenge_cools_down_and_keeps_history(runtime, monkeypatch):
    import time
    plugin, auth, cm = enable_auto(runtime)
    key = archive_record(plugin)
    before = copy.deepcopy(plugin.data['archive'])
    browser = Mock(return_value={'status': '需要验证', 'message': '豆瓣要求安全验证'})
    monkeypatch.setattr(auth, 'browser_login', browser)
    for _ in range(3):
        client = login_client(cm)
        assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
        client.set_status.assert_not_called()
    browser.assert_called_once()
    assert plugin.data['archive'] == before
    assert plugin._auth_public()['next_attempt'] > time.time() + 21000
    assert key in plugin.data['archive']


def test_disabled_auto_login_never_uses_browser(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._auto_login = False
    browser = Mock(side_effect=AssertionError('disabled')); monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    client.login_state.return_value = True
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    client.login_state.assert_called_once(); browser.assert_not_called()


def test_late_login_cannot_save_cookie_after_account_change(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    def login(*_, **kwargs):
        plugin._auth_init({'auto_login': True, 'douban_username': 'new@example.test', 'douban_password': 'new-secret'})
        return good_login()
    monkeypatch.setattr(auth, 'browser_login', login)
    client = login_client(cm)
    assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert not plugin._auth_record().get('cookie')
    client.set_status.assert_not_called()


def test_concurrent_sync_uses_one_login(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    plugin, auth, cm = enable_auto(runtime)
    browser = Mock(return_value=good_login()); monkeypatch.setattr(auth, 'browser_login', browser)
    clients = [login_client(cm) for _ in range(4)]
    for client in clients:
        client.login_state.side_effect = lambda c=client: c.cookies.get('dbcl2') == 'fresh'
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda c: plugin._write_douban(c, 'set_status', '200', 'collect', True), clients))
    browser.assert_called_once()


@pytest.mark.parametrize('http,html,expected', [
    (200, '<div id="db-global-nav"><div class="nav-user-account">账号</div></div>', True),
    (200, '<div id="db-global-nav"><a href="https://accounts.douban.com/passport/login">登录</a></div>', False),
    (200, '<h1>暂时无法访问</h1>', None), (403, '', None), (418, '', None), (401, '', False)])
def test_login_check_distinguishes_expiry_from_network(runtime, monkeypatch, http, html, expected):
    cm = sys.modules[runtime[2].__name__ + '.doubanclient']
    client = cm.DoubanClient(cookie='dbcl2=simulated; ck=simulated')
    monkeypatch.setattr(cm, 'RequestUtils', Mock(return_value=Mock(get_res=Mock(return_value=types.SimpleNamespace(
        status_code=http, text=html, url='https://movie.douban.com/')))))
    assert client.login_state() is expected


def browser_stub(monkeypatch, *, success=False, text='账号密码错误', changed_url=None, server_message=None):
    page = Mock()
    page.url = 'https://accounts.douban.com/passport/login'
    page.inner_text.return_value = text
    page.evaluate.return_value = 'browser-agent'
    page.query_selector.return_value = object() if success else None
    def goto(url, **kwargs):
        page.url = changed_url or url
    page.goto.side_effect = goto
    class Pending:
        value = types.SimpleNamespace(url='https://accounts.douban.com/j/accounts/login/basic', json=lambda:
            {'status': 'success' if success else 'failed', 'message': server_message or text})
        def __enter__(self): return self
        def __exit__(self, *args): return False
    page.expect_response.return_value = Pending()
    context = Mock(new_page=Mock(return_value=page))
    context.cookies.return_value = ([{'name': 'dbcl2', 'value': 'fresh', 'domain': '.douban.com', 'path': '/'},
                                     {'name': 'ck', 'value': 'fresh-ck', 'domain': '.douban.com', 'path': '/'},
                                     {'name': 'alien', 'value': 'exclude', 'domain': '.example.test', 'path': '/'}] if success else [])
    browser_module = types.ModuleType('app.sdk.browser')
    browser_module.launch_browser_context = Mock(return_value=context)
    monkeypatch.setitem(sys.modules, 'app.sdk.browser', browser_module)
    browser_module.BrowserSessionHelper = types.SimpleNamespace(_launch_context=browser_module.launch_browser_context)
    monkeypatch.setitem(sys.modules, 'app.helper.browser', browser_module)
    return context, page


def test_browser_login_success_closes_context_and_filters_domains(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch, success=True)
    result = auth.browser_login('test-user', 'test-password')
    assert result['status'] == '登录正常'
    assert 'alien' not in result['cookie'] and 'exclude' not in result['cookie']
    page.fill.assert_any_call('input[name="password"]', 'test-password')
    context.close.assert_called_once()


def test_browser_login_challenge_is_not_solved(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch, text='请完成安全验证')
    result = auth.browser_login('test-user', 'test-password')
    assert result['status'] == '需要验证' and 'cookie' not in result
    assert page.click.call_args_list == [((".account-tab-account",),), ((".account-form-field-submit .btn-account",),)]
    context.close.assert_called_once()


def test_server_captcha_is_detected_without_visible_page_message(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch, text='密码登录', server_message='captcha_required')
    result = auth.browser_login('test-user', 'test-password')
    assert result['status'] == '需要验证' and 'cookie' not in result
    page.inner_text.assert_not_called()
    context.close.assert_called_once()


def test_browser_never_submits_password_to_redirected_site(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch, changed_url='https://example.test/')
    assert auth.browser_login('test-user', 'test-password')['status'] == '登录失败'
    page.fill.assert_not_called(); context.close.assert_called_once()


def test_browser_exception_does_not_leak_password(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch)
    page.fill.side_effect = RuntimeError('simulated-password')
    result = auth.browser_login('test-user', 'simulated-password')
    assert result['status'] == '登录失败' and 'simulated-password' not in str(result)
    context.close.assert_called_once()


def test_account_change_keeps_explicit_manual_cookie_priority(runtime):
    plugin, auth, _ = enable_auto(runtime)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, **good_login()})
    plugin._auth_init({'auto_login': True, 'douban_username': 'other@example.test', 'douban_password': 'other-secret'})
    plugin._cookie = 'dbcl2=old-user; ck=old'
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('old CookieCloud should not be used'))
    plugin._load_cookie = types.MethodType(runtime[2].DoubanWatchSync._load_cookie, plugin)
    assert plugin._load_cookie() == plugin._cookie
    plugin._download_cookiecloud_cookies.assert_not_called()



def test_qr_notification_uses_mp_and_exposes_only_active_image(runtime, monkeypatch):
    import time, importlib
    plugin, auth, _ = enable_auto(runtime)
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    sender = Mock(return_value={'sent': 1, 'failed': 0}); monkeypatch.setattr(qn, 'send_qr_notifications', sender)
    image = 'https://img9.doubanio.com/view/qr/test.jpg'
    assert plugin._notify_login_qr(image, time.time() + 360, plugin._credential_key)
    sender.assert_called_once()
    assert sender.call_args.args[1] == image
    assert plugin._auth_public()['status'] == '等待扫码'
    assert plugin._auth_public()['qr_image'] == image
    assert 'qr_image' not in plugin.get_data(auth.AUTH_DATA)
    assert 'simulated-password' not in str(sender.call_args)
    plugin._enabled = False
    assert 'qr_image' not in plugin._auth_public()


def test_qr_notification_failure_keeps_local_qr(runtime, monkeypatch):
    import time, importlib
    plugin, auth, _ = enable_auto(runtime)
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    monkeypatch.setattr(qn, 'send_qr_notifications', Mock(return_value={'sent':0,'failed':1}))
    plugin._notify_login_qr('https://img9.doubanio.com/qr.jpg', time.time()+60, plugin._credential_key)
    public = plugin._auth_public()
    assert public['qr_image'] and '图片发送失败' in public['message']


def test_qr_account_change_cannot_notify_or_save_cookie(runtime):
    import time
    plugin, auth, _ = enable_auto(runtime)
    old = plugin._credential_key
    plugin.post_message = Mock()
    plugin._auth_init({'auto_login': True, 'douban_username': 'new', 'douban_password': 'new'})
    assert not plugin._notify_login_qr('https://img9.doubanio.com/qr.jpg', time.time()+60, old)
    plugin.post_message.assert_not_called()
    assert not plugin._save_auth_result(good_login(), old, {})


def test_qr_restart_does_not_show_dead_session(runtime):
    plugin, auth, _ = enable_auto(runtime)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'status': '等待扫码'})
    assert plugin._auth_public()['status'] == '扫码已结束'
    assert 'qr_image' not in plugin._auth_public()


@pytest.mark.parametrize('image', ['http://img9.doubanio.com/qr', 'https://evil.test/qr',
    'https://doubanio.com.evil.test/qr', 'https://user:password@img9.doubanio.com/qr',
    'https://img9.doubanio.com:bad/qr', 'data:image/png;base64,abc', None])
def test_qr_notification_rejects_untrusted_images(runtime, image):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    assert not auth._qr_image_url(image)


def test_password_failure_falls_back_without_solving_captcha(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch, server_message='captcha_required')
    callback = Mock(); keep = Mock(return_value=True)
    qr = Mock(return_value=good_login()); monkeypatch.setattr(auth, '_qr_login', qr)
    assert auth.browser_login('account', 'password', on_qr=callback, should_continue=keep)['cookie']
    qr.assert_called_once_with(context, page, callback, keep)
    context.close.assert_called_once()


def qr_browser(runtime, monkeypatch):
    auth = sys.modules[runtime[2].__name__ + '.auth']
    context, page = browser_stub(monkeypatch)
    page.expect_response.return_value.value = types.SimpleNamespace(json=lambda: {'status': 'success',
        'payload': {'code': 'opaque-test-session', 'img': 'https://img9.doubanio.com/qr.jpg'}})
    page.query_selector.return_value = None
    page.locator.return_value.get_attribute.return_value = 'data:image/png;base64,iVBORw0KGgo='
    return auth, context, page


def test_qr_waits_for_actual_cookie_and_closes_on_success(runtime, monkeypatch):
    auth, context, page = qr_browser(runtime, monkeypatch)
    verified = Mock(side_effect=[None, good_login()]); monkeypatch.setattr(auth, '_verified_cookie', verified)
    notify = Mock()
    assert auth._qr_login(context, page, notify, lambda: True)['status'] == '登录正常'
    notify.assert_called_once()
    page.wait_for_timeout.assert_called_once_with(1500)
    assert 'opaque-test-session' not in str(notify.call_args)


def test_qr_expired_never_saves_login(runtime, monkeypatch):
    auth, context, page = qr_browser(runtime, monkeypatch)
    page.query_selector.return_value = Mock(is_visible=Mock(return_value=True))
    notify = Mock()
    result = auth._qr_login(context, page, notify, lambda: True)
    assert result['status'] == '二维码已过期' and 'cookie' not in result
    notify.assert_called_once()


def test_qr_cancel_on_config_change(runtime, monkeypatch):
    auth, context, page = qr_browser(runtime, monkeypatch)
    checks = iter([True, False])
    result = auth._qr_login(context, page, Mock(), lambda: next(checks))
    assert result['status'] == '登录取消' and 'cookie' not in result


def test_login_api_is_async_deduplicated_and_never_marks_watched(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    browser = Mock(return_value=good_login()); monkeypatch.setattr(auth, 'browser_login', browser)
    queued = runtime[3]
    plugin._load_cookie = Mock(return_value='')
    assert plugin.api_login()['success']
    assert plugin.api_login()['success']
    jobs = [j for j in queued if j[0] == 'login_only']
    assert len(jobs) == 1
    browser.assert_not_called()
    jobs[0][1]()
    assert plugin._auth_record()['cookie']
    assert not plugin.get_data('archive') and not plugin.get_data('pending')


def test_qr_success_resumes_original_write_once(runtime, monkeypatch):
    import time, importlib
    qn = importlib.import_module(runtime[2].__name__ + ".qrnotify")
    monkeypatch.setattr(qn, "send_qr_notifications", Mock(return_value={"sent":1,"failed":0}))
    plugin, auth, cm = enable_auto(runtime)
    plugin.post_message = Mock()
    def login(username, password, *, on_qr, should_continue):
        assert on_qr('https://img9.doubanio.com/qr.jpg', time.time()+360)
        assert should_continue()
        assert not client.set_status.called
        return good_login()
    monkeypatch.setattr(auth, 'browser_login', login)
    client = login_client(cm)
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    client.set_status.assert_called_once()
    assert plugin._auth_record()['cookie'] and not plugin._login_qr


def test_qr_wait_is_bounded_without_scan(runtime, monkeypatch):
    auth, context, page = qr_browser(runtime, monkeypatch)
    clock = iter([100, 101, 461])
    monkeypatch.setattr(auth.time, 'time', lambda: next(clock))
    notify = Mock()
    result = auth._qr_login(context, page, notify, lambda: True)
    assert result['status'] == '二维码已过期' and 'cookie' not in result
    notify.assert_called_once_with('https://img9.doubanio.com/qr.jpg', 460, b'\x89PNG\r\n\x1a\n')
    page.wait_for_timeout.assert_called_once()


def test_qr_payload_without_trusted_image_never_notifies(runtime, monkeypatch):
    auth, context, page = qr_browser(runtime, monkeypatch)
    page.expect_response.return_value.value.json = lambda: {'status': 'success', 'payload': {'code': 'opaque', 'img': 'https://evil.test/img'}}
    notify = Mock()
    result = auth._qr_login(context, page, notify, lambda: True)
    assert result['status'] == '登录失败' and 'cookie' not in result
    notify.assert_not_called()


def test_old_login_status_does_not_block_new_login_after_restart(runtime):
    plugin, auth, _ = enable_auto(runtime)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'status': '正在登录'})
    assert plugin._auth_public()['status'] == '等待同步触发'
    assert plugin.api_login()['success']
    assert any(job[0] == 'login_only' for job in runtime[3])


def qr_notify_env(runtime, monkeypatch):
    import importlib
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    message_module = types.ModuleType('app.schemas.message')
    message_module.Message = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, 'app.schemas.message', message_module)
    client = Mock()
    client._appid = 'configured-app'
    client._send_msg_url = 'configured-send-endpoint'
    client._upload_temp_media.return_value = 'private-media-id'
    client._WeChat__post_request.return_value = True
    module = Mock()
    module.get_instance.return_value = client
    module.check_message.return_value = True
    module.get_notification_userid.return_value = 'configured-admin'
    service = types.SimpleNamespace(name='configured-wechat', type='wechat', module=module,
                                    config=types.SimpleNamespace(enabled=True), instance=client)
    monkeypatch.setattr(sys.modules['app.sdk.services'], 'NotificationHelper',
                        lambda: types.SimpleNamespace(iterate_module_instances=lambda: iter([service])), raising=False)
    plugin = runtime[0]; plugin.chain = Mock(); plugin.post_message = Mock()
    monkeypatch.setattr(qn, '_routed_messages', lambda chain, message: [message])
    paths = []
    def download(image, directory):
        p = directory / 'qr.png'; p.write_bytes(b'test-QR'); paths.append(p); return p
    monkeypatch.setattr(qn, '_download_qr', download)
    return qn, plugin, module, client, service, paths


def test_wechat_qr_is_image_message_without_link_and_temp_file_is_deleted(runtime, monkeypatch):
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    result = qn.send_qr_notifications(plugin, 'https://img9.doubanio.com/qr.png', '请扫码')
    assert result == {'sent':1,'failed':0,'skipped':0,'failures':[]}
    client._upload_temp_media.assert_called_once_with(paths[0], media_type='image')
    payload = client._WeChat__post_request.call_args.args[1]
    assert payload == {'touser':'configured-admin', 'msgtype':'image', 'agentid':'configured-app',
                       'image':{'media_id':'private-media-id'}}
    assert not any(p.exists() for p in paths)
    plugin.post_message.assert_not_called()
    module.check_message.assert_called_once()
    module.get_notification_userid.assert_called_once()


def test_disabled_wechat_channel_is_not_sent(runtime, monkeypatch):
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    service.config.enabled = False
    assert not qn.send_qr_notifications(plugin, 'image', 'text')['sent']
    module.check_message.assert_not_called(); client._upload_temp_media.assert_not_called()
    assert not paths


def test_filtered_plugin_message_never_downloads_or_uploads_qr(runtime, monkeypatch):
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    module.check_message.return_value = False
    assert qn.send_qr_notifications(plugin, 'image', 'text')['skipped'] == 1
    client._upload_temp_media.assert_not_called()
    assert not paths


@pytest.mark.parametrize('stage', ['upload', 'send', 'exception', 'unsupported'])
def test_wechat_picture_failure_does_not_fallback_to_link(runtime, monkeypatch, stage):
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    if stage == 'upload': client._upload_temp_media.return_value = None
    if stage == 'send': client._WeChat__post_request.return_value = False
    if stage == 'exception': client._WeChat__post_request.side_effect = RuntimeError('secret-token')
    if stage == 'unsupported': client._WeChat__post_request = None
    result = qn.send_qr_notifications(plugin, 'image', 'text')
    assert result['failed'] == 1 and result['sent'] == 0
    plugin.post_message.assert_not_called()
    assert not any(p.exists() for p in paths)
    assert 'secret-token' not in str(sys.modules['app.sdk.logging'].logger.warn.call_args_list)


def test_other_notification_channels_keep_mp_image_delivery(runtime, monkeypatch):
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    service.type = 'telegram'
    assert qn.send_qr_notifications(plugin, 'official-image', 'text')['sent'] == 1
    assert plugin.post_message.call_args.kwargs['source'] == 'configured-wechat'
    assert plugin.post_message.call_args.kwargs['image'] == 'official-image'
    client._upload_temp_media.assert_not_called()


def test_wechat_qr_uses_host_routing_and_admin_bindings(runtime, monkeypatch):
    import importlib
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    config = types.ModuleType('app.core.config'); config.settings = types.SimpleNamespace(SUPERUSER='host-admin')
    user_module = types.ModuleType('app.db.user_oper'); users = Mock()
    users.get_settings.return_value = {'wechat_userid': 'bound-admin'}
    user_module.UserOper = Mock(return_value=users)
    service = types.ModuleType('app.helper.service')
    service.ServiceConfigHelper = types.SimpleNamespace(get_notification_switch=Mock(return_value='admin'))
    for name, value in [('app.core.config',config),('app.db.user_oper',user_module),('app.helper.service',service)]:
        monkeypatch.setitem(sys.modules,name,value)
    chain = Mock(); chain._normalize_notification_for_dispatch.side_effect = lambda message: message
    message = types.SimpleNamespace(userid=None, username=None, mtype='plugin', targets=None)
    messages = list(qn._routed_messages(chain, message))
    assert messages[0].targets == {'wechat_userid':'bound-admin'}
    users.get_settings.assert_called_once_with('host-admin')


@pytest.mark.parametrize('bad', ['untrusted', 'redirect', 'oversize', 'not_image'])
def test_qr_image_download_rejects_invalid_content(runtime, monkeypatch, tmp_path, bad):
    import importlib
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    response = types.SimpleNamespace(status_code=200, url='https://img9.doubanio.com/qr.png', content=b'not-image')
    if bad == 'redirect': response.url = 'https://evil.test/qr.png'
    if bad == 'oversize': response.content = b'x' * (2*1024*1024+1)
    mock = Mock(return_value=Mock(get_res=Mock(return_value=response)))
    monkeypatch.setattr(sys.modules['app.sdk.network'], 'RequestUtils', mock)
    image = 'https://evil.test/qr' if bad == 'untrusted' else 'https://img9.doubanio.com/qr.png'
    with pytest.raises(Exception): qn._download_qr(image, tmp_path)
    assert not list(tmp_path.iterdir())
    if bad == 'untrusted': mock.assert_not_called()


def test_browser_qr_png_is_used_directly_without_image_download(runtime, monkeypatch):
    import io
    from PIL import Image
    qn, plugin, module, client, service, paths = qr_notify_env(runtime, monkeypatch)
    download = Mock(side_effect=AssertionError('must not download URL'))
    monkeypatch.setattr(qn, '_download_qr', download)
    raw = io.BytesIO(); Image.new('RGB',(128,128),'white').save(raw,format='PNG')
    result = qn.send_qr_notifications(plugin, 'https://img9.doubanio.com/qr.png', 'text', qr_png=raw.getvalue())
    assert result['sent'] == 1 and result['failed'] == 0
    download.assert_not_called()
    image_path = client._upload_temp_media.call_args.args[0]
    assert image_path.suffix == '.png' and not image_path.exists()


@pytest.mark.parametrize('src', ['https://evil.test/image', 'data:image/svg+xml;base64,PHN2Zz4=',
                                 'data:image/png;base64,YmFkLWltYWdl'])
def test_login_page_without_valid_qr_png_never_notifies(runtime, monkeypatch, src):
    auth, context, page = qr_browser(runtime, monkeypatch)
    page.locator.return_value.get_attribute.return_value = src
    notify = Mock()
    result = auth._qr_login(context, page, notify, lambda: True)
    assert result['status'] == '登录失败'
    notify.assert_not_called()


def test_late_picture_notification_cannot_overwrite_new_account_state(runtime, monkeypatch):
    import time, importlib
    plugin, auth, _ = enable_auto(runtime)
    qn = importlib.import_module(runtime[2].__name__ + '.qrnotify')
    old = plugin._credential_key
    def notify(*args, **kwargs):
        plugin._auth_init({'auto_login':True,'douban_username':'new','douban_password':'new'})
        plugin.save_data(auth.AUTH_DATA, {'credential_key':plugin._credential_key,'status':'正在登录'})
        raise RuntimeError('notification failed')
    monkeypatch.setattr(qn,'send_qr_notifications',notify)
    assert not plugin._notify_login_qr('https://img9.doubanio.com/qr.png',time.time()+60,old)
    assert plugin.get_data(auth.AUTH_DATA)['credential_key'] == plugin._credential_key


@pytest.mark.parametrize('auto_login', [True, False])
def test_saved_cookie_has_priority_and_does_not_read_cloud(runtime, monkeypatch, auto_login):
    plugin, auth, cm = enable_auto(runtime)
    plugin._auto_login = auto_login
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, **good_login()})
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('local cookie is valid'))
    browser = Mock(side_effect=AssertionError('local cookie is valid'))
    monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    client.login_state.side_effect = lambda: client.cookies.get('dbcl2') == 'fresh'
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.cookies['dbcl2'] == 'fresh'
    plugin._download_cookiecloud_cookies.assert_not_called()
    browser.assert_not_called()


def test_expired_local_uses_cloud_before_browser_and_saves_cloud(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'cookie': 'dbcl2=local; ck=old', 'user_agent': 'old-agent'})
    events = []
    def cloud():
        events.append('cloud')
        return {'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''
    plugin._download_cookiecloud_cookies = Mock(side_effect=cloud)
    browser = Mock(side_effect=AssertionError('cloud cookie works'))
    monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    def check():
        events.append('check:' + client.cookies.get('dbcl2', ''))
        return client.cookies.get('dbcl2') == 'cloud'
    client.login_state.side_effect = check
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert events == ['check:local', 'cloud', 'check:cloud']
    assert client._parse_cookie(plugin._auth_record()['cookie']) == {'dbcl2': 'cloud', 'ck': 'cloud-ck'}
    assert client.user_agent == ''
    browser.assert_not_called()
    again = login_client(cm)
    again.login_state = Mock(side_effect=AssertionError('recent saved cookie'))
    assert plugin._write_douban(again, 'set_status', '201', 'collect', True)
    plugin._download_cookiecloud_cookies.assert_called_once()


def test_both_cookie_sources_expired_then_browser_login(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    events = []
    def cloud():
        events.append('cloud')
        return {'douban.com': 'dbcl2=cloud-expired; ck=old'}, ''
    def browser(*args, **kwargs):
        events.append('browser')
        return good_login()
    plugin._download_cookiecloud_cookies = Mock(side_effect=cloud)
    monkeypatch.setattr(auth, 'browser_login', Mock(side_effect=browser))
    client = login_client(cm, 'dbcl2=local-expired; ck=old')
    def check():
        events.append('check:' + client.cookies['dbcl2'])
        return False
    client.login_state.side_effect = check
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert events == ['check:local-expired', 'cloud', 'check:cloud-expired', 'browser']
    client.set_status.assert_called_once()
    assert plugin._auth_record()['cookie'] == good_login()['cookie']


def test_cloud_switch_off_skips_download_then_logs_in(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._use_cookiecloud = False
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('disabled cloud'))
    browser = Mock(return_value=good_login())
    monkeypatch.setattr(auth, 'browser_login', browser)
    assert plugin._write_douban(login_client(cm), 'set_status', '200', 'collect', True)
    plugin._download_cookiecloud_cookies.assert_not_called()
    browser.assert_called_once()


@pytest.mark.parametrize('cookie', ['', 'dbcl2=expired; ck=old'])
def test_cloud_works_without_enabled_account_login(runtime, monkeypatch, cookie):
    plugin, auth, cm = enable_auto(runtime)
    plugin._auto_login = False
    plugin._douban_username = plugin._douban_password = ''
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''))
    browser = Mock(side_effect=AssertionError('account login disabled'))
    monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm, cookie)
    client.login_state.side_effect = lambda: client.cookies.get('dbcl2') == 'cloud'
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client._parse_cookie(plugin._auth_record()['cookie']) == {'dbcl2': 'cloud', 'ck': 'cloud-ck'}
    browser.assert_not_called()


def test_valid_cookie_works_without_configured_account(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._douban_username = plugin._douban_password = ''
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('valid cookie'))
    monkeypatch.setattr(auth, 'browser_login', Mock(side_effect=AssertionError('no account')))
    client = login_client(cm); client.login_state.return_value = True
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)


def test_identical_expired_cloud_cookie_is_not_rechecked(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'ck=old; dbcl2=expired'}, ''))
    browser = Mock(return_value=good_login()); monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    client.login_state.assert_called_once()
    browser.assert_called_once()


def test_sudden_expiry_switches_to_cloud_and_retries_write_once(runtime, monkeypatch):
    import time
    plugin, auth, cm = enable_auto(runtime)
    client = login_client(cm)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'cookie': client.cookie_header(),
                                    'status': '登录正常', 'checked_at': time.time()})
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''))
    client.login_state.side_effect = lambda: client.cookies.get('dbcl2') == 'cloud'
    client.set_status.side_effect = [False, True]
    browser = Mock(side_effect=AssertionError('cloud handles expiry')); monkeypatch.setattr(auth, 'browser_login', browser)
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.set_status.call_count == 2
    assert client.login_state.call_count == 2  # failed local, then cloud; rejected local is not checked twice.
    browser.assert_not_called()


def test_cloud_check_network_error_does_not_trigger_account_login(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''))
    browser = Mock(side_effect=AssertionError('network is not expiry')); monkeypatch.setattr(auth, 'browser_login', browser)
    client = login_client(cm)
    client.login_state.side_effect = lambda: None if client.cookies.get('dbcl2') == 'cloud' else False
    client.set_status.return_value = False
    assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert plugin._auth_record()['status'] == '检查失败'
    browser.assert_not_called()


def test_account_change_allows_explicit_cloud_fallback(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, **good_login()})
    plugin._auth_init({'auto_login': True, 'douban_username': 'new@example.test', 'douban_password': 'new-secret'})
    plugin._cookie = 'dbcl2=old-manual; ck=old'
    plugin._load_cookie = types.MethodType(runtime[2].DoubanWatchSync._load_cookie, plugin)
    assert plugin._load_cookie() == plugin._cookie
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''))
    monkeypatch.setattr(auth, 'browser_login', Mock(side_effect=AssertionError('cloud works')))
    client = login_client(cm, '')
    client.login_state.side_effect = lambda: client.cookies.get('dbcl2') == 'cloud'
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.cookies['dbcl2'] == 'cloud'


def test_config_change_during_cookie_check_does_not_save_late_result(runtime):
    plugin, auth, cm = enable_auto(runtime)
    client = login_client(cm)
    def check():
        plugin._auth_init({'auto_login': True, 'douban_username': 'new@example.test', 'douban_password': 'new-secret'})
        plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'status': '等待同步触发'})
        return False
    client.login_state.side_effect = check
    assert not plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert plugin._auth_record() == {'credential_key': plugin._credential_key, 'status': '等待同步触发'}
    client.set_status.assert_not_called()


def test_manual_correction_with_no_local_cookie_can_use_cloud(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    plugin._load_cookie = Mock(return_value='')
    plugin._download_cookiecloud_cookies = Mock(return_value=({'douban.com': 'dbcl2=cloud; ck=cloud-ck'}, ''))
    cm = sys.modules[runtime[2].__name__ + '.doubanclient']
    client = cm.DoubanClient('')
    client.login_state = Mock(side_effect=lambda: client.cookies.get('dbcl2') == 'cloud')
    client.get_subject_info = Mock(return_value={'title': 'correct title', 'image': '', 'media_type': 'MOV'})
    client.set_status = Mock(return_value=True)
    client.remove_status = Mock(return_value=True)
    monkeypatch.setattr(management, 'DoubanClient', lambda **kwargs: client)
    assert plugin.api_match({'key': key, 'subject_id': '200'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('200', 'collect', True)
    client.remove_status.assert_called_once_with('100')
    assert plugin.get_data('archive')[key]['subject_id'] == '200'
    plugin._download_cookiecloud_cookies.assert_called_once()


@pytest.mark.parametrize('auto_login', [True, False])
def test_manual_cookie_overrides_saved_without_overwriting_it(runtime, monkeypatch, auto_login):
    import time
    plugin, auth, cm = enable_auto(runtime)
    plugin._auto_login = auto_login
    plugin._cookie = 'dbcl2=manual; ck=manual-ck'
    saved = {'credential_key': plugin._credential_key, **good_login(), 'checked_at': time.time()}
    plugin.save_data(auth.AUTH_DATA, saved)
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('manual is valid'))
    monkeypatch.setattr(auth, 'browser_login', Mock(side_effect=AssertionError('manual is valid')))
    plugin._load_cookie = types.MethodType(runtime[2].DoubanWatchSync._load_cookie, plugin)
    assert plugin._load_cookie() == plugin._cookie
    client = login_client(cm)
    client.login_state.side_effect = lambda: client.cookies['dbcl2'] == 'manual'
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.cookies['dbcl2'] == 'manual'
    client.login_state.assert_called_once()
    assert plugin._auth_record()['cookie'] == saved['cookie']
    assert plugin._auth_record()['user_agent'] == saved['user_agent']
    plugin._download_cookiecloud_cookies.assert_not_called()
    plugin._cookie = ''
    assert plugin._load_cookie() == saved['cookie']


def test_expired_manual_cookie_falls_back_to_saved_before_cloud(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._cookie = 'dbcl2=manual-expired; ck=old'
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, **good_login()})
    plugin._download_cookiecloud_cookies = Mock(side_effect=AssertionError('saved is valid'))
    client = login_client(cm)
    checked = []
    def check():
        checked.append(client.cookies['dbcl2'])
        return client.cookies['dbcl2'] == 'fresh'
    client.login_state.side_effect = check
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert checked == ['manual-expired', 'fresh']
    assert client.user_agent == 'browser-test-agent'
    plugin._download_cookiecloud_cookies.assert_not_called()


def test_changed_manual_cookie_does_not_reuse_previous_validation(runtime):
    plugin, auth, cm = enable_auto(runtime)
    plugin._cookie = 'dbcl2=manual-one; ck=one'
    client = login_client(cm); client.login_state.return_value = True
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    plugin._cookie = 'dbcl2=manual-two; ck=two'
    again = login_client(cm); again.login_state.return_value = True
    assert plugin._write_douban(again, 'set_status', '201', 'collect', True)
    again.login_state.assert_called_once()
    assert again.cookies['dbcl2'] == 'manual-two'


def test_failed_manual_write_falls_back_to_saved_and_retries_once(runtime):
    plugin, auth, cm = enable_auto(runtime)
    plugin._cookie = 'dbcl2=manual; ck=manual-ck'
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, **good_login()})
    client = login_client(cm)
    client.login_state.side_effect = [True, False, True]
    client.set_status.side_effect = [False, True]
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert client.cookies['dbcl2'] == 'fresh'
    assert client.set_status.call_count == 2
    assert client.login_state.call_count == 3
    assert plugin._auth_record()['manual_checked_at'] == 0
    again = login_client(cm)
    again.login_state.side_effect = lambda: again.cookies['dbcl2'] == 'fresh'
    assert plugin._write_douban(again, 'set_status', '201', 'collect', True)
    again.set_status.assert_called_once()
    assert again.cookies['dbcl2'] == 'fresh'


def test_manual_saved_cloud_then_account_are_checked_in_order(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._cookie = 'dbcl2=manual; ck=manual'
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'cookie': 'dbcl2=saved; ck=saved'})
    order = []
    def cloud():
        order.append('read-cloud')
        return {'douban.com': 'dbcl2=cloud; ck=cloud'}, ''
    def browser(*args, **kwargs):
        order.append('account')
        return good_login()
    plugin._download_cookiecloud_cookies = Mock(side_effect=cloud)
    monkeypatch.setattr(auth, 'browser_login', Mock(side_effect=browser))
    client = login_client(cm)
    def check():
        order.append('check:' + client.cookies['dbcl2'])
        return False
    client.login_state.side_effect = check
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    assert order == ['check:manual', 'check:saved', 'read-cloud', 'check:cloud', 'account']


def test_manual_and_saved_identical_invalid_cookie_is_checked_once(runtime, monkeypatch):
    plugin, auth, cm = enable_auto(runtime)
    plugin._cookie = 'dbcl2=expired; ck=old'
    plugin.save_data(auth.AUTH_DATA, {'credential_key': plugin._credential_key, 'cookie': 'ck=old; dbcl2=expired'})
    plugin._use_cookiecloud = False
    monkeypatch.setattr(auth, 'browser_login', Mock(return_value=good_login()))
    client = login_client(cm)
    assert plugin._write_douban(client, 'set_status', '200', 'collect', True)
    client.login_state.assert_called_once()


def test_v2_adapter_uses_host_browser_without_sdk(runtime, monkeypatch):
    compat = sys.modules[runtime[2].__name__ + '.compat']
    browser = types.ModuleType('app.helper.browser')
    context = Mock()
    browser.BrowserSessionHelper = types.SimpleNamespace(_launch_context=Mock(return_value=context))
    monkeypatch.setitem(sys.modules, 'app.helper.browser', browser)
    assert compat.launch_browser_context(headless=True) is context
    browser.BrowserSessionHelper._launch_context.assert_called_once_with(headless=True)
    assert all('app.sdk' not in p.read_text() for p in ROOT.glob('*.py'))


def test_v2_deferred_task_replacement_and_cancel_do_not_touch_other_plugin(runtime, monkeypatch):
    compat = sys.modules[runtime[2].__name__ + '.compat']
    timers = []
    class Timer:
        def __init__(self, delay, func):
            self.func = func; self.cancelled = False; timers.append(self)
        def start(self): pass
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(compat.threading, 'Timer', Timer)
    tasks = compat.OnceTasks()
    old, new, unrelated = Mock(), Mock(), Mock()
    tasks.add_plugin_once_job('watchsync','retry',old,'retry',delay_seconds=1)
    tasks.add_plugin_once_job('watchsync','retry',new,'retry',delay_seconds=1)
    tasks.add_plugin_once_job('other','retry',unrelated,'retry',delay_seconds=1)
    assert timers[0].cancelled
    timers[0].func(); old.assert_not_called()
    tasks.remove_plugin_once_job('watchsync')
    timers[1].func(); new.assert_not_called()
    assert not timers[2].cancelled
    timers[2].func(); unrelated.assert_called_once()


def test_v2_missing_recipient_binding_is_not_broadcast(runtime):
    compat = sys.modules[runtime[2].__name__ + '.compat']
    module = types.SimpleNamespace()
    with pytest.raises(ValueError):
        compat.notification_userid(module, types.SimpleNamespace(userid=None, targets={}), 'wechat_userid')
    assert compat.notification_userid(module, types.SimpleNamespace(userid=None, targets={'wechat_userid':'admin'}), 'wechat_userid') == 'admin'
    assert compat.notification_userid(module, types.SimpleNamespace(userid=None, targets=None), 'wechat_userid') is None


def test_v2_stop_cancels_all_owned_delayed_tasks(runtime):
    compat = sys.modules[runtime[2].__name__ + '.compat']
    compat.scheduler_sdk.remove_plugin_once_job = Mock()
    plugin = runtime[0]
    plugin.stop_service()
    assert not plugin._enabled
    compat.scheduler_sdk.remove_plugin_once_job.assert_called_once_with('DoubanWatchSync')


def test_management_reuses_posters_without_network_or_raw_urls(runtime):
    plugin, management, module, queued = runtime
    url = 'https://img3.doubanio.com/view/photo/s_ratio_poster/public/example.jpg'
    plugin.save_data('archive', {'a': {'title': '影片', 'image': url}})
    plugin.save_data('images', {url: 'data:image/jpeg;base64,cached'})
    queued.clear()
    assert plugin.api_management()['archive'][0]['image'] == 'data:image/jpeg;base64,cached'
    assert not queued
    assert plugin.get_data('archive')['a']['image'] == url


def test_management_warms_posters_asynchronously_and_retries(runtime, monkeypatch):
    plugin, management, module, queued = runtime
    urls = ['https://image.tmdb.org/t/p/original/example.jpg', 'https://img9.doubanio.com/poster.jpg']
    plugin.save_data('archive', {str(i): {'image': url} for i, url in enumerate(urls)})
    queued.clear()
    assert all(not row['image'] for row in plugin.api_management()['archive'])
    assert len(queued) == 1
    plugin.api_management()
    assert len(queued) == 1
    client = Mock()
    client.fetch_image_data_uri.side_effect = ['data:image/jpeg;base64,new', None]
    original = management.DoubanClient
    monkeypatch.setattr(management, 'DoubanClient', Mock(return_value=client))
    queued[0][1]()
    monkeypatch.setattr(management, 'DoubanClient', original)
    assert sum(bool(row['image']) for row in plugin.api_management()['archive']) == 1
    assert len(queued) == 1  # Failed images are not retried every three-second refresh.
    monkeypatch.setattr(management.time, 'monotonic', lambda: 10**12)
    plugin.api_management()
    assert len(queued) == 2


def test_management_never_exposes_private_poster_urls(runtime):
    plugin, management, module, queued = runtime
    plugin.save_data('pending', {'a': {'image': 'http://emby.local/Items/1/Images/Primary?api_key=secret'}})
    queued.clear()
    assert plugin.api_management()['pending'][0]['image'] == ''
    assert not queued


@pytest.mark.parametrize('url', ['https://image.tmdb.org/t/p/w185/a.jpg', 'https://img.doubanio.com/a.jpg', 'https://img3.doubanio.com/a.jpg'])
def test_public_poster_hosts(runtime, url):
    assert runtime[2].DoubanClient.is_public_image_url(url)


@pytest.mark.parametrize('url', ['http://img3.doubanio.com/a.jpg', 'https://img3.doubanio.com.attacker.test/a.jpg', 'https://evilimg3.doubanio.com/a.jpg', 'https://img3.doubanio.com/a.jpg?api_key=secret', 'https://u:p@image.tmdb.org/a.jpg', 'https://127.0.0.1/a.jpg', 'data:image/svg+xml,test'])
def test_private_or_untrusted_poster_hosts_are_rejected(runtime, url):
    assert not runtime[2].DoubanClient.is_public_image_url(url)


def test_poster_download_is_anonymous_and_checks_image_bytes(runtime, monkeypatch):
    import io
    from PIL import Image
    client_module = sys.modules[runtime[2].__name__ + '.doubanclient']
    buf = io.BytesIO()
    Image.new('RGB', (2, 3)).save(buf, format='JPEG')
    response = Mock(status_code=200, content=buf.getvalue())
    request = Mock()
    request.get_res.return_value = response
    factory = Mock(return_value=request)
    monkeypatch.setattr(client_module, 'RequestUtils', factory)
    client = runtime[2].DoubanClient(cookie='dbcl2=private; ck=private')
    assert client.fetch_image_data_uri('https://image.tmdb.org/t/p/original/a.jpg').startswith('data:image/jpeg;base64,')
    headers = factory.call_args.kwargs['headers']
    assert not any(key.lower() == 'cookie' for key in headers)
    request.get_res.assert_called_once_with('https://image.tmdb.org/t/p/w185/a.jpg', allow_redirects=False)
    response.content = b'<html>blocked</html>'
    assert client.fetch_image_data_uri('https://img3.doubanio.com/a.jpg') is None


def test_dashboard_cache_flush_preserves_management_posters(runtime):
    plugin = runtime[0]
    plugin._image_map = {'old': 'cached-old'}
    plugin._image_dirty = True
    plugin.save_data('images', {'background': 'cached-new'})
    plugin._flush_images()
    assert plugin.get_data('images') == {'old': 'cached-old', 'background': 'cached-new'}

UP = '进击的巨人 OAD #4-1 无悔的选择（上集）'
DOWN = '进击的巨人 OAD #4-2 无悔的选择（下集）'


def episode_record(title, episode, subject='25949760'):
    return {'title': title, 'source_title': title, 'subject_name': title, 'subject_id': subject,
            'type': '电视剧', 'season': 0, 'episode': episode, 'server_name': 'example-server',
            'item_id': 'example-series', 'status': 'collect', 'timestamp': f'2025-01-01 23:{episode}:00'}


def test_multipart_key_reuses_work_but_isolates_other_oad_and_servers(runtime):
    plugin = runtime[0]
    key = plugin._archive_key(UP, 'TV', 0, 'example-server')
    assert key == plugin._archive_key(DOWN, 'TV', 0, 'example-server')
    assert key != plugin._archive_key('进击的巨人 OAD #3 困难', 'TV', 0, 'example-server')
    assert key != plugin._archive_key(DOWN, 'TV', 0, 'other-server')
    assert key != plugin._archive_key(DOWN, 'TV', 1, 'example-server')
    assert plugin._archive_key(UP, 'MOV', 0, 'example-server') != plugin._archive_key(DOWN, 'MOV', 0, 'example-server')


def test_migration_merges_both_manual_ids_without_writes_and_is_idempotent(runtime):
    plugin, management, module, queued = runtime
    old = {UP+'@example-server': episode_record(UP,15), DOWN+'@example-server': episode_record(DOWN,16)}
    plugin.save_data('archive', old)
    plugin.save_data(management.OVERRIDES, {key:{'subject_id':'25949760','ignore':False} for key in old})
    plugin._migrate_work_records()
    key = plugin._archive_key(DOWN, 'TV', 0, 'example-server')
    assert list(plugin.get_data('archive')) == [key]
    assert plugin._manual_subject(plugin._archive_key(UP, 'TV', 0, 'example-server')) == '25949760'
    assert plugin._manual_subject(key) == '25949760'
    assert plugin.get_data('archive')[key]['episode'] == 16
    assert plugin.get_data('episode_work_backup')['archive'] == old
    saved = copy.deepcopy(plugin.data)
    plugin._migrate_work_records()
    assert plugin.data == saved


def test_migration_keeps_latest_unsynced_episode_for_retry(runtime):
    plugin, management, _, _ = runtime
    plugin.save_data('archive', {UP+'@example-server':episode_record(UP,15)})
    plugin.save_data('pending', {DOWN+'@example-server':episode_record(DOWN,16,subject='')})
    plugin.save_data(management.OVERRIDES, {UP+'@example-server':{'subject_id':'25949760','ignore':False}})
    plugin._migrate_work_records()
    key=plugin._archive_key(UP,'TV',0,'example-server')
    assert key in plugin.get_data('pending')
    assert plugin._manual_subject(key)=='25949760'


def test_migration_does_not_guess_when_manual_numbers_conflict(runtime):
    plugin, management, _, _=runtime
    archive={UP+'@example-server':episode_record(UP,15),DOWN+'@example-server':episode_record(DOWN,16,subject='other')}
    plugin.save_data('archive',archive)
    plugin.save_data(management.OVERRIDES,{key:{'subject_id':row['subject_id']} for key,row in archive.items()})
    plugin._migrate_work_records()
    assert plugin.get_data('archive')==archive
    assert plugin._manual_guard(plugin._archive_key(UP,'TV',0,'example-server'))


def test_special_work_scope_includes_only_its_two_episodes(runtime):
    reader_module = sys.modules[runtime[2].__name__+'.mediaserver']
    reader = reader_module.MediaServerReader()
    reader.get_season_episode_items=Mock(return_value=[
        {'Name':'OAD #3 困难','IndexNumber':14,'ParentIndexNumber':0},
        {'Name':'OAD #4-1 无悔的选择（上集）','IndexNumber':15,'ParentIndexNumber':0},
        {'Name':'OAD #4-2 无悔的选择（下集）','IndexNumber':16,'ParentIndexNumber':0},
        {'Name':'OAD #5-1 迷失的女孩（上集）','IndexNumber':17,'ParentIndexNumber':0},
        {'Name':'正片','IndexNumber':1,'ParentIndexNumber':1}])
    assert reader.get_work_episodes('series',0,DOWN,16)=={'episodes':[15,16],'minimum':2}
    assert reader.get_work_episodes('series',0,'困难',14)=={'episodes':[14],'minimum':1}


@pytest.mark.parametrize('played,expected', [({15:True,16:False},'do'),({15:False,16:True},'do'),
    ({15:True,16:True,14:False,17:False},'collect'),({15:True},'do'),({},'do')])
def test_special_completion_requires_both_episodes(runtime,played,expected):
    plugin=runtime[0]
    reader=Mock();reader.get_series_id.return_value='series';reader.get_work_episodes.return_value={'episodes':[15,16],'minimum':2}
    plugin._play_state_of_any_user=Mock(return_value=played)
    plugin._subject_episode_count=Mock(return_value=2)
    payload={'title':DOWN,'item_id':'series','server_name':'example-server','season':0,'episode':16}
    assert plugin._resolve_status(payload,reader,'TV',0,16,'25949760')==expected


@pytest.mark.parametrize('episodes,count,state,expected', [
    ([1,2,3],3,{1:True,2:True,3:False},'do'),
    ([1,2,3],3,{1:False,2:False,3:True},'do'),
    ([1,2,3],3,{1:True,2:True,3:True},'collect'),
    ([1,2],3,{1:True,2:True},'do'),
    ([1,3],2,{1:True,3:True},'do'),
    ([1,2],0,{1:True,2:True},'do'),
    ([1,2,3],3,{1:True,3:True},'do')])
def test_normal_season_requires_complete_episode_set_and_play_state(runtime,episodes,count,state,expected):
    plugin=runtime[0]
    reader=Mock();reader.get_series_id.return_value='series';reader.get_work_episodes.return_value={'episodes':episodes,'minimum':0}
    plugin._play_state_of_any_user=Mock(return_value=state)
    plugin._subject_episode_count=Mock(return_value=count)
    payload={'title':'剧集','item_id':'series','season':1,'episode':3}
    assert plugin._resolve_status(payload,reader,'TV',1,3,'200')==expected


def test_second_episode_automatically_reuses_manual_id_without_search(runtime,monkeypatch):
    plugin, management, module, _ = runtime
    plugin.save_data('archive',{UP+'@example-server':episode_record(UP,15)})
    plugin.save_data(management.OVERRIDES,{UP+'@example-server':{'subject_id':'25949760','ignore':False}})
    plugin._migrate_work_records()
    reader=Mock();reader.get_series_id.return_value='series';reader.get_work_episodes.return_value={'episodes':[15,16],'minimum':2}
    reader.get_douban_id=Mock(side_effect=AssertionError('must reuse manual subject'))
    plugin._reader=Mock(return_value=reader)
    plugin._subject_episode_count=Mock(return_value=2)
    plugin._play_state_of_any_user=Mock(return_value={15:True,16:True})
    plugin._match_subject=Mock(side_effect=AssertionError('must not search again'))
    client=Mock();client.get_subject_image.return_value='';client.get_subject_info.return_value={'title':'无悔的选择','media_type':'TV','episodes_count':2}
    monkeypatch.setattr(module,'DoubanClient',lambda **kwargs:client)
    plugin._write_douban=Mock(return_value=True)
    plugin._sync_item({'title':DOWN,'media_type':'TV','season':0,'episode':16,'server_name':'example-server','item_id':'example-series'})
    assert len(plugin.get_data('archive'))==1
    row=next(iter(plugin.get_data('archive').values()))
    assert row['subject_id']=='25949760'
    assert row['watched_episodes']==[15,16]
    assert plugin._write_douban.call_args.kwargs['subject_id']=='25949760'
    assert plugin._write_douban.call_args.kwargs['status']=='collect'


def test_manual_watched_falls_back_to_in_progress_until_whole_work_is_complete(runtime,monkeypatch):
    plugin, management, _, _ = runtime
    key=archive_record(plugin)
    plugin.data['archive'][key].update(type='电视剧',status='do')
    client=fake_client(monkeypatch,management,kind='TV')
    plugin._tv_completion=Mock(return_value={'complete':False,'known':True,'episodes':[1,2],'played':[1],'expected':2})
    assert plugin.api_match({'key':key,'subject_id':'100','status':'collect'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('100','do',True)
    assert key not in plugin.get_data(management.CORRECTIONS)
    assert plugin.get_data('archive')[key]['status']=='do'


def test_season_state_includes_missing_and_never_mixes_season_zero(runtime):
    reader_module=sys.modules[runtime[2].__name__+'.mediaserver']
    reader=reader_module.MediaServerReader()
    reader.get_season_episode_items=Mock(return_value=[
        {'IndexNumber':15,'UserData':{'Played':True}},
        {'IndexNumber':16,'IsMissing':True,'UserData':{'Played':True}}])
    assert reader.get_season_play_state('series',0)=={15:True,16:False}


def test_new_episode_reader_strictly_filters_specials_and_keeps_missing(runtime):
    reader_module=sys.modules[runtime[2].__name__+'.mediaserver']
    reader=reader_module.MediaServerReader();instance=Mock()
    reader._locate=Mock(return_value=(instance,'emby'))
    reader._user_scope_url=Mock(side_effect=lambda instance,url:url)
    reader._get=Mock(return_value={'Items':[{'IndexNumber':15,'ParentIndexNumber':0},
        {'IndexNumber':16,'ParentIndexNumber':0,'IsMissing':True},{'IndexNumber':1,'ParentIndexNumber':1}]})
    rows=reader.get_season_episode_items('series',0)
    assert [row['IndexNumber'] for row in rows]==[15,16]
    assert 'IsMissing=false' not in reader._get.call_args.args[1]


@pytest.mark.parametrize('original,complete,known,expected_write', [
    ('collect',False,True,'do'),('collect',False,False,None),('do',True,True,'collect'),('collect',True,True,None)])
def test_rescan_uses_work_scope_and_only_corrects_confirmed_state(runtime,monkeypatch,original,complete,known,expected_write):
    plugin,management,module,_=runtime
    key=archive_record(plugin)
    plugin.save_data('subject_details',{'100':{'title':'剧集','media_type':'TV','expires':9999999999}})
    plugin.data['archive'][key].update(type='电视剧',status=original,season=1,item_id='series')
    reader=Mock();reader.get_series_id.return_value='series';plugin._reader=Mock(return_value=reader)
    plugin._tv_completion=Mock(return_value={'complete':complete,'known':known,'episodes':[1,2],
        'played':[1,2] if complete else [1],'expected':2})
    plugin._write_douban=Mock(return_value=True)
    plugin._match_subject=Mock(side_effect=lambda record,reader: (record.get('title'),record.get('subject_id')))
    summary=plugin.rescan_archive()
    if expected_write:
        assert plugin._write_douban.call_args.kwargs['status']==expected_write
        assert plugin.get_data('archive')[key]['status']==expected_write
    else:
        plugin._write_douban.assert_not_called()
        assert plugin.get_data('archive')[key]['status']==original


def test_retry_rechecks_stale_per_episode_watched_status(runtime):
    plugin=runtime[0]
    plugin.save_data('subject_details',{'200':{'title':'剧集','media_type':'TV','expires':9999999999}})
    plugin.save_data('pending',{'show@mp-server':{'title':'剧集','source_title':'剧集','subject_id':'200','type':'TV',
        'status':'collect','season':1,'episode':2,'server_name':'mp-server','item_id':'series'}})
    plugin._fill_missing_images=Mock();plugin.retry_management=Mock()
    reader=Mock();reader.get_series_id.return_value='series';reader.get_work_episodes.return_value={'episodes':[1,2],'minimum':0}
    plugin._reader=Mock(return_value=reader)
    plugin._subject_episode_count=Mock(return_value=2)
    plugin._play_state_of_any_user=Mock(return_value={1:False,2:True})
    plugin._write_douban=Mock(return_value=True)
    reader.get_match_metadata.return_value={};reader.get_douban_id.return_value='200'
    plugin.retry_pending()
    assert plugin._write_douban.call_args.kwargs['status']=='do'
    assert plugin.get_data('archive')['show@mp-server']['status']=='do'


def test_subject_episode_count_reuses_public_metadata_cache(runtime,monkeypatch):
    plugin,_,module,_=runtime
    client=Mock();client.get_subject_info.return_value={'title':'剧集','episodes_count':12}
    factory=Mock(return_value=client);monkeypatch.setattr(module,'DoubanClient',factory)
    assert plugin._subject_episode_count('200')==12
    assert plugin._subject_episode_count('200')==12
    factory.assert_called_once_with()
    client.get_subject_info.assert_called_once_with('200','TV')

def test_progress_refresh_only_updates_local_metadata(runtime):
    plugin=runtime[0]
    key=archive_record(plugin)
    plugin.data['archive'][key].update(type='电视剧')
    plugin.save_data('work_progress_pending',[key])
    plugin._reader=Mock()
    plugin._tv_completion=Mock(return_value={'complete':True,'known':True,'episodes':[15,16],'played':[15,16],'expected':2})
    plugin._write_douban=Mock(side_effect=AssertionError('progress must not write to Douban'))
    plugin._refresh_work_progress([key])
    assert plugin.get_data('archive')[key]['watched_episodes']==[15,16]
    assert plugin.get_data('archive')[key]['expected_episodes']==2
    assert plugin.get_data('work_progress_pending')==[]


def test_late_progress_result_does_not_overwrite_new_manual_match(runtime):
    plugin=runtime[0]
    key=archive_record(plugin)
    plugin._reader=Mock()
    def completion(*args):
        plugin.data['archive'][key]['subject_id']='new'
        return {'complete':True,'known':True,'episodes':[1,2],'played':[1,2],'expected':2}
    plugin._tv_completion=Mock(side_effect=completion)
    plugin._refresh_work_progress([key])
    assert plugin.get_data('archive')[key]['subject_id']=='new'
    assert 'watched_episodes' not in plugin.get_data('archive')[key]


def lost_items():
    return [{'Name':'OAD #5-1 迷失的女孩：再见，希娜之墙（上集）','IndexNumber':17,'ParentIndexNumber':0},
            {'Name':'OAD #5-2 迷失的女孩：再见，希娜之墙（下集）','IndexNumber':18,'ParentIndexNumber':0},
            {'Name':'OAD #8 迷失的女孩：迷失在残酷的世界里','IndexNumber':19,'ParentIndexNumber':0},
            {'Name':'OAD #4-1 无悔的选择（上集）','IndexNumber':15,'ParentIndexNumber':0}]


def test_lost_girls_groups_three_episodes_and_reuses_id_across_oad_numbers(runtime):
    plugin=runtime[0]
    reader_module=sys.modules[runtime[2].__name__+'.mediaserver']
    reader=reader_module.MediaServerReader();reader.get_season_episode_items=Mock(return_value=lost_items())
    first='进击的巨人 '+lost_items()[0]['Name']
    last='进击的巨人 '+lost_items()[2]['Name']
    assert plugin._archive_key(first,'TV',0,'sample')==plugin._archive_key(last,'TV',0,'sample')
    assert reader.get_work_episodes('series',0,last,19)['episodes']==[17,18,19]
    assert plugin._archive_key(first,'TV',0,'sample')!=plugin._archive_key('进击的巨人 '+lost_items()[3]['Name'],'TV',0,'sample')


@pytest.mark.parametrize('count,state,result',[(3,{17:True,18:True,19:False},'do'),(3,{17:True,18:True,19:True},'collect'),(0,{17:True,18:True,19:True},'do')])
def test_official_total_is_required_and_all_three_episodes_must_be_watched(runtime,count,state,result):
    plugin=runtime[0];reader=Mock();reader.get_series_id.return_value='series'
    reader.get_work_episodes.return_value={'episodes':[17,18,19],'minimum':2}
    plugin._subject_episode_count=Mock(return_value=count)
    plugin._play_state_of_any_user=Mock(return_value=state)
    payload={'title':'Lost Girls','season':0,'episode':19,'item_id':'series'}
    assert plugin._resolve_status(payload,reader,'TV',0,19,'27144865')==result
    assert payload['expected_episodes']==count


def test_failed_refresh_keeps_previous_douban_total(runtime,monkeypatch):
    plugin,_,module,_=runtime
    plugin.save_data('episode_counts',{'27144865':{'count':3,'expires':0}})
    client=Mock();client.get_subject_info.return_value=None
    monkeypatch.setattr(module,'DoubanClient',Mock(return_value=client))
    assert plugin._subject_episode_count('27144865')==3
    assert plugin.get_data('episode_counts')['27144865']['count']==3


def test_manual_details_cache_douban_total_before_completion(runtime):
    plugin=runtime[0]
    plugin._remember_subject_info('27144865',{'title':'Lost Girls','episodes_count':'3'})
    assert plugin._subject_episode_count('27144865')==3
    plugin._remember_subject_info('27144865',{'episodes_count':0})
    assert plugin._subject_episode_count('27144865')==3


def test_missing_api_episode_count_falls_back_to_official_html(runtime,monkeypatch):
    client_module=sys.modules[runtime[2].__name__+'.doubanclient']
    api_module=types.ModuleType('app.modules.douban.apiv2')
    api_module.DoubanApi=Mock(return_value=types.SimpleNamespace(tv_detail=Mock(return_value={'title':'Lost Girls'})))
    monkeypatch.setitem(sys.modules,'app.modules.douban.apiv2',api_module)
    response=Mock(status_code=200,text='<h1><span property="v:itemreviewed">Lost Girls</span></h1><div id="info"><span>集数 </span>: 3</div>')
    monkeypatch.setattr(client_module,'RequestUtils',Mock(return_value=types.SimpleNamespace(get_res=Mock(return_value=response))))
    assert client_module.DoubanClient().get_subject_info('27144865','TV')['episodes_count']==3


def test_upgrade_clears_inferred_total_but_retains_confirmed_douban_total(runtime):
    plugin=runtime[0]
    plugin.data.pop('official_episode_totals_v1',None)
    plugin.save_data('archive',{'a':{'type':'电视剧','subject_id':'a','expected_episodes':2},'b':{'type':'电视剧','subject_id':'b','expected_episodes':2}})
    plugin.save_data('episode_counts',{'b':{'count':3}})
    plugin._clear_inferred_totals()
    assert plugin.get_data('archive')['a']['expected_episodes']==0
    assert plugin.get_data('archive')['b']['expected_episodes']==3
    assert plugin.get_data('inferred_episode_totals_backup')['archive']['a']['expected_episodes']==2


def cross_setup(runtime, *, season=1, expected=4, a_items=None, b_items=None, a_state=None, b_state=None):
    plugin = runtime[0]
    plugin._server = ['a', 'b']
    plugin._skip_first = False
    plugin.save_data('subject_details',{'200':{'title':'剧集','media_type':'TV','expires':9999999999}})
    def record(server):
        return {'title': '剧集', 'source_title': '剧集', 'type': '电视剧', 'subject_id': '200',
                'status': 'do', 'season': season, 'episode': 1, 'item_id': server + '-series',
                'server_name': server, 'timestamp': '2026-10-19 12:00:00'}
    records = {server: record(server) for server in ('a', 'b')}
    plugin.save_data('archive', {server: record for server, record in records.items()})
    readers = {}
    for server, items in [('a', a_items), ('b', b_items)]:
        if items is None:
            items = [{'IndexNumber': index, 'ParentIndexNumber': season, 'Name': f'Episode {index}'} for index in range(1, expected + 1)]
        reader = Mock()
        reader.get_series_id.return_value = server + '-series'
        reader.get_work_episodes.return_value = {'episodes': [row['IndexNumber'] for row in items]}
        reader.get_season_episode_items.return_value = items
        reader.get_douban_id.return_value = '200'
        readers[server] = reader
    plugin._reader = Mock(side_effect=lambda name, **kwargs: readers[name])
    states = {'a': a_state if a_state is not None else {1: True, 2: True, 3: False, 4: False},
              'b': b_state if b_state is not None else {1: False, 2: False, 3: True, 4: True}}
    plugin._play_state_of_any_user = Mock(side_effect=lambda series, season, server: states[server])
    plugin._subject_episode_count = Mock(return_value=expected)
    plugin._write_douban = Mock(return_value=True)
    return plugin, records, readers, states


def test_cross_server_split_watch_is_complete_and_deduplicated(runtime):
    plugin, records, readers, _ = cross_setup(runtime)
    for server in ('a', 'b'):
        completion = plugin._tv_completion(records[server], readers[server], '200')
        assert completion['complete'] and completion['played'] == [1, 2, 3, 4]
        assert completion['servers'] == ['a', 'b']


def test_cross_server_repeats_do_not_add_up(runtime):
    plugin, records, readers, _ = cross_setup(runtime, a_state={1: True, 2: True, 3: False, 4: False},
                                             b_state={1: True, 2: True, 3: False, 4: False})
    completion = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not completion['complete'] and completion['played'] == [1, 2]


def test_cross_server_partial_libraries_can_supply_complete_normal_season(runtime):
    plugin, records, readers, _ = cross_setup(runtime, expected=2,
        a_items=[{'IndexNumber': 1}], b_items=[{'IndexNumber': 2}], a_state={1: True}, b_state={2: True})
    completion = plugin._tv_completion(records['a'], readers['a'], '200')
    assert completion['complete'] and completion['episodes'] == [1, 2]


@pytest.mark.parametrize('change', ['subject', 'season', 'ignore', 'correction', 'disabled', 'deleted', 'unselected', 'missing_subject'])
def test_cross_server_excludes_unrelated_or_disabled_records(runtime, change):
    plugin, records, readers, _ = cross_setup(runtime)
    if change == 'subject': plugin.data['archive']['b']['subject_id'] = 'different'
    if change == 'season': plugin.data['archive']['b']['season'] = 2
    if change == 'ignore': plugin.save_data('manual_matches', {'b': {'ignore': True}})
    if change == 'correction': plugin.save_data('corrections', {'b': {'state': 'running'}})
    if change in ('disabled', 'deleted'):
        plugin.data['archive']['b']['direct_id'] = 'b-direct'
        plugin.save_data('direct_servers_v2', {'b-direct': {'id': 'b-direct', 'name': 'b', 'enabled': False}} if change == 'disabled' else {})
    if change == 'unselected': plugin._server = ['a']
    if change == 'missing_subject': plugin.data['archive']['b']['subject_id'] = ''
    completion = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not completion['complete'] and completion['played'] == [1, 2]
    plugin._reader.assert_not_called()


def test_cross_server_uses_live_progress_not_old_aggregate_archive(runtime):
    plugin, records, readers, _ = cross_setup(runtime, a_state={1: False, 2: False, 3: False, 4: False},
                                             b_state={1: False, 2: False, 3: False, 4: False})
    for row in plugin.data['archive'].values(): row.update(watched_episodes=[1, 2, 3, 4], status='collect')
    completion = plugin._tv_completion(records['a'], readers['a'], '200')
    assert completion['known'] and not completion['complete'] and completion['played'] == []


def test_cross_server_failed_peer_is_unknown_not_a_confirmed_downgrade(runtime):
    plugin, records, readers, _ = cross_setup(runtime)
    readers['b'].get_series_id.side_effect = RuntimeError('offline')
    completion = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not completion['known'] and not completion['complete']


def test_cross_server_one_fully_watched_server_suffices_if_peer_offline(runtime):
    plugin, records, readers, _ = cross_setup(runtime, a_state={1: True, 2: True, 3: True, 4: True})
    readers['b'].get_series_id.side_effect = RuntimeError('offline')
    assert plugin._tv_completion(records['a'], readers['a'], '200')['complete']


def test_cross_server_unknown_total_never_marks_complete(runtime):
    plugin, records, readers, _ = cross_setup(runtime)
    plugin._subject_episode_count.return_value = 0
    assert not plugin._tv_completion(records['a'], readers['a'], '200')['complete']


def special_cross(runtime, provider=True):
    names = ['无悔的选择（上集）', '无悔的选择（下集）']
    a = [{'Name': f'OAD #4-{index} {name}', 'IndexNumber': index + 14,
          'ProviderIds': {'Tvdb': str(100 + index)} if provider else {}} for index, name in enumerate(names, 1)]
    b = [{'Name': f'OVA #1-{index} {name}', 'IndexNumber': index,
          'ProviderIds': {'Tvdb': str(100 + index)} if provider else {}} for index, name in enumerate(names, 1)]
    return cross_setup(runtime, season=0, expected=2, a_items=a, b_items=b,
                       a_state={15: True, 16: False}, b_state={1: False, 2: True})


@pytest.mark.parametrize('provider', [True, False])
def test_cross_server_specials_align_by_episode_identity_not_local_numbers(runtime, provider):
    plugin, records, readers, _ = special_cross(runtime, provider)
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert result['complete'] and len(result['played']) == 2


def test_cross_server_specials_repeated_episode_is_not_counted_twice(runtime):
    plugin, records, readers, states = special_cross(runtime)
    states['b'] = {1: True, 2: False}
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not result['complete'] and len(result['played']) == 1


def test_cross_server_specials_conflicting_provider_ids_do_not_merge_by_title(runtime):
    plugin, records, readers, _ = special_cross(runtime)
    for row in readers['b'].get_season_episode_items.return_value:
        row['ProviderIds']['Tvdb'] += '-wrong'
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not result['complete'] and not result['known']


def test_cross_server_specials_generic_titles_are_not_guessed(runtime):
    plugin, records, readers, _ = special_cross(runtime, False)
    for reader in readers.values():
        for row in reader.get_season_episode_items.return_value: row['Name'] = '第1集'
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not result['complete'] and not result['known']


def test_cross_server_auto_sync_updates_both_record_statuses_and_progress(runtime, monkeypatch):
    plugin, records, _, _ = cross_setup(runtime)
    client = Mock(); client.get_subject_image.return_value = ''
    monkeypatch.setattr(runtime[2], 'DoubanClient', Mock(return_value=client))
    plugin._sync_item({**records['a'], 'media_type': 'TV'})
    assert plugin._write_douban.call_args.kwargs['status'] == 'collect'
    archive = plugin.get_data('archive')
    assert all(row['status'] == 'collect' and row['watched_episodes'] == [1,2,3,4] for row in archive.values())
    assert {row['server_name'] for row in archive.values()} == {'a', 'b'}
    assert all(row['progress_servers'] == ['a','b'] for row in archive.values())


def test_cross_server_unknown_read_cannot_overwrite_completed_subject_from_other_server(runtime, monkeypatch):
    plugin, records, readers, _ = cross_setup(runtime)
    plugin.data['archive']['b']['status'] = 'collect'
    readers['b'].get_series_id.side_effect = RuntimeError('offline')
    plugin._sync_item({**records['a'], 'media_type': 'TV'})
    plugin._write_douban.assert_not_called()


def test_cross_server_retry_rechecks_combined_progress(runtime):
    plugin, records, _, _ = cross_setup(runtime)
    plugin.save_data('pending', {'a': {**records['a'], 'type': 'TV'}})
    plugin.data['archive'].pop('a')
    plugin.retry_management = Mock(); plugin._fill_missing_images = Mock()
    plugin.retry_pending()
    assert plugin._write_douban.call_args.kwargs['status'] == 'collect'
    assert plugin.get_data('archive')['b']['status'] == 'collect'
    assert not plugin.get_data('pending')


def test_cross_server_rescan_does_not_downgrade_after_other_server_completed(runtime):
    plugin, _, _, _ = cross_setup(runtime)
    plugin.rescan_archive()
    assert plugin._write_douban.call_count == 1
    assert plugin._write_douban.call_args.kwargs['status'] == 'collect'
    assert all(row['status'] == 'collect' for row in plugin.get_data('archive').values())


def test_cross_server_manual_match_uses_combined_progress(runtime, monkeypatch):
    plugin, _, _, _ = cross_setup(runtime)
    client = fake_client(monkeypatch, runtime[1])
    client.get_subject_info.return_value = {'title': '剧集', 'episodes_count': 4}
    assert plugin.api_match({'key': 'a', 'subject_id': '200', 'status': 'do'})['success']
    plugin._run_correction('a')
    assert plugin._write_douban.call_args.args[3] == 'collect'
    assert all(row['status'] == 'collect' for row in plugin.get_data('archive').values())


def test_cross_server_upgrade_only_refreshes_metadata_and_keeps_backup(runtime):
    plugin, _, _, _ = cross_setup(runtime)
    plugin.data.pop('cross_server_progress_v1')
    plugin._queue_combined_progress()
    assert plugin.get_data('work_progress_pending') == ['a', 'b']
    plugin._refresh_work_progress(['a', 'b'])
    plugin._write_douban.assert_not_called()
    assert plugin.get_data('cross_server_progress_backup')['a']['status'] == 'do'
    assert all(row['status'] == 'do' and row['watched_episodes'] == [1,2,3,4] for row in plugin.get_data('archive').values())


def test_cross_server_failed_submission_keeps_both_previous_states(runtime, monkeypatch):
    plugin, records, _, _ = cross_setup(runtime)
    plugin._write_douban.return_value = False
    plugin._sync_item({**records['a'], 'media_type': 'TV'})
    assert all(row['status'] == 'do' for row in plugin.get_data('archive').values())
    assert plugin.get_data('pending')


def test_cross_server_concurrent_events_cannot_end_with_stale_in_progress_write(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    plugin, records, _, states = cross_setup(runtime, expected=2,
        a_state={1: True, 2: False}, b_state={1: False, 2: False})
    first_write = Event(); release_first = Event(); second_started = Event(); second_write = Event()
    writes = []
    def write(client, method, *args, **kwargs):
        status = kwargs['status']
        if status == 'do':
            first_write.set()
            assert release_first.wait(3)
        else:
            second_write.set()
        writes.append(status)
        return True
    plugin._write_douban = Mock(side_effect=write)
    client = Mock();client.get_subject_image.return_value = ''
    monkeypatch.setattr(runtime[2], 'DoubanClient', Mock(return_value=client))
    def second():
        second_started.set()
        plugin._sync_item({**records['b'], 'media_type': 'TV'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(plugin._sync_item, {**records['a'], 'media_type': 'TV'})
        assert first_write.wait(3)
        states['b'][2] = True
        later = pool.submit(second)
        assert second_started.wait(3)
        # The later complete result must not overtake the older pending write.
        assert not second_write.wait(.05)
        release_first.set()
        first.result(timeout=3); later.result(timeout=3)
    assert writes == ['do', 'collect']
    assert all(row['status'] == 'collect' for row in plugin.get_data('archive').values())


def test_cross_server_stale_library_id_is_relocated_with_verified_douban_id(runtime):
    plugin, records, readers, _ = cross_setup(runtime)
    readers['a'].get_series_id.return_value = 'old-a'
    readers['a'].get_work_episodes.side_effect = [{'episodes': []}, {'episodes': [1,2,3,4]}]
    readers['a'].search_series.return_value = 'new-a'
    readers['a'].get_douban_id.return_value = '200'
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert result['complete'] and result['servers'] == ['a','b']
    readers['a'].get_douban_id.assert_called_once_with('new-a')


def test_cross_server_stale_id_does_not_relocate_to_same_title_with_other_subject(runtime):
    plugin, records, readers, _ = cross_setup(runtime)
    readers['a'].get_work_episodes.return_value = {'episodes': []}
    readers['a'].search_series.return_value = 'unrelated'
    readers['a'].get_douban_id.return_value = 'different'
    result = plugin._tv_completion(records['a'], readers['a'], '200')
    assert not result['complete'] and result['played'] == [3,4]
    assert result['servers'] == ['b']


@pytest.mark.parametrize('metadata,expected',[
    ({'type':'movie'},'MOV'),({'type':'tv'},'TV'),({'is_tv':False},'MOV'),({'is_tv':True},'TV'),
    ({'episodes_count':3},'TV'),({'episodes_count':0},''),({'title':'电影名'},''),({'type':'unknown'},'')])
def test_subject_type_requires_explicit_douban_evidence(runtime,metadata,expected):
    cm=sys.modules[runtime[2].__name__+'.doubanclient']
    assert cm.DoubanClient._detail_type(metadata)==expected


def test_s00_movie_type_is_read_from_official_movie_page_not_tmdb_hint(runtime,monkeypatch):
    cm=sys.modules[runtime[2].__name__+'.doubanclient']
    page=Mock(status_code=200,text='<h1><span property="v:itemreviewed">完结篇 前篇</span></h1><div id="info">上映日期: 2023-03-04 片长: 61分钟</div>')
    requests=Mock(return_value=Mock(get_res=Mock(return_value=page)))
    monkeypatch.setattr(cm,'RequestUtils',requests)
    result=cm.DoubanClient('ck=simulated').get_subject_info('35853587','TV')
    assert result['media_type']=='MOV' and result['episodes_count']==0


def test_missing_tv_count_does_not_make_a_tv_subject_a_movie(runtime,monkeypatch):
    cm=sys.modules[runtime[2].__name__+'.doubanclient']
    page=Mock(status_code=200,text='<h1><span property="v:itemreviewed">剧集</span></h1><div id="info">首播: 2026-10-19 单集片长: 45分钟</div><script type="application/ld+json">{"@type":"Movie"}</script>')
    monkeypatch.setattr(cm,'RequestUtils',Mock(return_value=Mock(get_res=Mock(return_value=page))))
    result=cm.DoubanClient().get_subject_info('200','MOV')
    assert result['media_type']=='TV' and result['episodes_count']==0


@pytest.mark.parametrize('values,expected', [(['wish','collect'],'MOV'),(['wish','do','collect'],'TV'),(['collect'],''),([], '')])
def test_official_read_only_form_can_identify_subject_type(runtime,monkeypatch,values,expected):
    cm=sys.modules[runtime[2].__name__+'.doubanclient']
    response=Mock(status_code=200)
    response.json.return_value={'html':'<form>'+''.join(f'<input name="interest" value="{value}">' for value in values)+'</form>'}
    get=Mock(return_value=response);post=Mock(side_effect=AssertionError('read-only classification must not POST'))
    monkeypatch.setattr(cm,'RequestUtils',Mock(return_value=Mock(get_res=get,post_res=post)))
    assert cm.DoubanClient('ck=simulated')._form_subject_type('35853587')==expected
    assert get.call_args.args[0]=='https://movie.douban.com/j/subject/35853587/interest'
    post.assert_not_called()


def movie_special(runtime,monkeypatch):
    plugin,management,module,_=runtime
    record={'title':'剧名 完结篇（上集）','source_title':'剧名 完结篇','type':'TV',
        'season':0,'episode':36,'item_id':'series','server_name':'mp-server','user_name':'mp-user',
        'status':'collect','expected_episodes':0,'watched_episodes':[36,37]}
    key=plugin._archive_key(record['source_title'],'TV',0,'mp-server')
    plugin.save_data('pending',{key:record})
    client=fake_client(monkeypatch,management,kind='MOV')
    client.get_subject_info.return_value.update(title='完结篇 前篇',episodes_count=0)
    plugin._tv_completion=Mock(side_effect=AssertionError('a Douban movie has no TV completion check'))
    return plugin,key,record,client


def test_manual_s00_movie_writes_collect_and_isolates_following_episode(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    assert plugin.api_match({'key':key,'subject_id':'35853587','status':'collect'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('35853587','collect',True)
    exact=plugin._movie_episode_key(record)
    result=plugin.get_data('archive')[exact]
    assert key not in plugin.get_data('pending') and key not in plugin.get_data('manual_matches')
    assert result['type']=='电影' and result['source_media_type']=='TV' and result['douban_type']=='MOV'
    assert result['expected_episodes']==0 and result['watched_episodes']==[]
    assert plugin._manual_subject(plugin._routing_key({**record,'media_type':'TV'}))=='35853587'
    assert plugin._manual_subject(plugin._routing_key({**record,'episode':37,'media_type':'TV'}))==''
    assert plugin.get_data('subject_type_backup_2.3.9')['pending'][key]['episode']==36


def test_manual_in_progress_is_corrected_to_collect_for_douban_movie(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    assert plugin.api_match({'key':key,'subject_id':'35853587','status':'do'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('35853587','collect',True)


def test_s00_movie_failed_submission_keeps_precise_retry_binding(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    client.set_status.return_value=False
    plugin.api_match({'key':key,'subject_id':'35853587'});plugin._run_correction(key)
    exact=plugin._movie_episode_key(record)
    assert plugin.get_data('corrections')[exact]['state']=='failed'
    assert plugin.get_data('manual_matches')[exact]['episode']==36
    assert not plugin.get_data('archive')
    assert plugin._routing_key({**record,'episode':37})!=exact


@pytest.mark.parametrize('event,writes',[('PlaybackStart',False),('PlaybackStop',True)])
def test_auto_s00_movie_never_sends_do(runtime,monkeypatch,event,writes):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    monkeypatch.setattr(runtime[2],'DoubanClient',Mock(return_value=client))
    reader=Mock();reader.get_douban_id.return_value='35853587';reader.get_match_metadata.return_value={'douban_id':'35853587','scope':'episode'};plugin._reader=Mock(return_value=reader)
    plugin._write_douban=Mock(return_value=True)
    plugin._sync_item({**record,'media_type':'TV','event':event})
    if writes:
        assert plugin._write_douban.call_args.kwargs['status']=='collect'
        assert plugin.get_data('archive')[plugin._movie_episode_key(record)]['type']=='电影'
    else:
        plugin._write_douban.assert_not_called()


def test_retry_s00_movie_checks_only_the_corresponding_episode(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.data['pending'][key]['subject_id']='35853587'
    monkeypatch.setattr(runtime[2],'DoubanClient',Mock(return_value=client))
    reader=Mock();reader.get_series_id.return_value='series';plugin._reader=Mock(return_value=reader)
    plugin._is_played_by_any_user=Mock(return_value=True)
    plugin._write_douban=Mock(return_value=True);plugin.retry_management=Mock();plugin._fill_missing_images=Mock()
    reader.get_match_metadata.return_value={'douban_id':'35853587','scope':'episode'};reader.get_douban_id.return_value='35853587'
    plugin.retry_pending()
    assert plugin._write_douban.call_args.kwargs['status']=='collect'
    assert plugin._is_played_by_any_user.call_args.args[2]==36
    assert plugin.get_data('archive')[plugin._movie_episode_key(record)]['type']=='电影'


def test_unverified_douban_type_is_not_guessed_from_media_server(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    client.get_subject_info.return_value={'title':'未知类型','episodes_count':0}
    plugin._prepare_login=Mock(return_value=False)
    plugin._write_douban=Mock(return_value=True)
    plugin.api_match({'key':key,'subject_id':'35853587'});plugin._run_correction(key)
    plugin._write_douban.assert_not_called()
    assert '类型' in plugin.get_data('corrections')[key]['message']


def test_subject_type_cache_survives_failed_refresh(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.save_data('subject_details',{'35853587':{'title':'前篇','media_type':'MOV','expires':0}})
    client.get_subject_info.return_value=None
    assert plugin._subject_detail(client,'35853587','TV')['media_type']=='MOV'


def test_code_upgrade_keeps_old_failed_manual_sync_for_user_retry(runtime):
    plugin=runtime[0]
    plugin.data.pop('subject_type_failures_v1',None)
    plugin.save_data('corrections',{'old':{'state':'failed','record':{'type':'TV'},'subject_id':'35853587'}})
    plugin._defer_old_type_failures()
    plugin._queue_correction=Mock()
    plugin.retry_management()
    plugin._queue_correction.assert_not_called()
    assert plugin.get_data('corrections')['old']['manual_retry_only']


def test_rescan_old_s00_tv_record_uses_douban_movie_type(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.save_data('pending',{})
    plugin.save_data('archive',{key:{**record,'type':'电视剧','subject_id':'35853587','status':'do'}})
    monkeypatch.setattr(runtime[2],'DoubanClient',Mock(return_value=client))
    reader=Mock();reader.get_series_id.return_value='series';plugin._reader=Mock(return_value=reader)
    plugin._is_played_by_any_user=Mock(return_value=True)
    plugin._write_douban=Mock(return_value=True)
    plugin._match_subject=Mock(side_effect=lambda record,reader: (record.get('title'),record.get('subject_id')))
    result=plugin.rescan_archive()
    assert result['upgraded']==1
    assert plugin._write_douban.call_args.kwargs['status']=='collect'
    assert plugin.get_data('archive')[plugin._movie_episode_key(record)]['type']=='电影'


def test_precise_movie_no_entry_rule_does_not_block_next_special(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.api_match({'key':key,'subject_id':'35853587'});plugin._run_correction(key)
    exact=plugin._movie_episode_key(record)
    plugin.api_match({'key':exact,'action':'ignore'})
    assert plugin._manual_guard(plugin._routing_key(record))
    assert not plugin._manual_guard(plugin._routing_key({**record,'episode':37}))


def test_user_can_explicitly_retry_deferred_type_failure(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.api_match({'key':key,'subject_id':'35853587'})
    op=plugin.data['corrections'][key];op['state']='failed';op['manual_retry_only']=True
    assert plugin.api_match({'key':key,'action':'retry'})['success']
    assert not plugin.get_data('corrections')[key].get('manual_retry_only')
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('35853587','collect',True)


def test_pending_type_error_can_be_retried_without_existing_correction(runtime,monkeypatch):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    plugin.data['pending'][key].update(subject_id='35853587',error='无法确认豆瓣条目类型')
    assert plugin.api_match({'key':key,'action':'retry'})['success']
    plugin._run_correction(key)
    client.set_status.assert_called_once_with('35853587','collect',True)


@pytest.mark.parametrize('kind,writes',[('MOV',True),('TV',False)])
def test_first_episode_filter_uses_douban_type_not_s00_location(runtime,monkeypatch,kind,writes):
    plugin,key,record,client=movie_special(runtime,monkeypatch)
    record.update(episode=1)
    client.get_subject_info.return_value['media_type']=kind
    monkeypatch.setattr(runtime[2],'DoubanClient',Mock(return_value=client))
    reader=Mock();reader.get_douban_id.return_value='35853587';reader.get_match_metadata.return_value={'douban_id':'35853587','scope':'episode'};plugin._reader=Mock(return_value=reader)
    plugin._write_douban=Mock(return_value=True)
    plugin._skip_first=True
    plugin._sync_item({**record,'media_type':'TV','event':'PlaybackStop'})
    assert plugin._write_douban.called is writes
    if writes:assert plugin._write_douban.call_args.kwargs['status']=='collect'


@pytest.mark.parametrize('special', [False, True])
def test_match_metadata_uses_series_ids_for_tv_and_episode_ids_for_special(runtime, monkeypatch, special):
    mm = sys.modules[runtime[2].__name__ + '.mediaserver']
    reader = mm.MediaServerReader()
    episode = {'Id':'ep','Type':'Episode','Name':'第3集','SeriesId':'show',
               'ProviderIds':{'IMDB':'tt9999','Tmdb':'9000'}}
    series = {'Id':'show','Type':'Series','Name':'魔女嘉莉','ProductionYear':2026,
              'ProviderIds':{'Imdb':'tt11540908','Tmdb':'288673'}}
    monkeypatch.setattr(reader, 'get_item', lambda key: {'ep':episode,'show':series}.get(key, {}))
    found = reader.get_match_metadata('ep', 'TV', 0 if special else 1, 3)
    assert found['imdb_id'] == ('tt9999' if special else 'tt11540908')
    assert found['tmdb_id'] == ('9000' if special else '288673')
    assert found['scope'] == ('episode' if special else 'series')


def test_series_event_resolves_exact_special_before_using_identifiers(runtime, monkeypatch):
    mm = sys.modules[runtime[2].__name__ + '.mediaserver']
    reader = mm.MediaServerReader()
    items = {'show':{'Id':'show','Type':'Series','ProviderIds':{'Imdb':'tt1111'}},
             'ep':{'Id':'ep','Type':'Episode','ProviderIds':{'Imdb':'tt2222'}}}
    monkeypatch.setattr(reader, 'get_item', lambda key: items.get(key, {}))
    reader.get_season_episode_items = Mock(return_value=[{'Id':'ep','IndexNumber':36}])
    assert reader.get_match_metadata('show','TV',0,36)['imdb_id'] == 'tt2222'
    assert reader.get_match_metadata('show','TV',0,37) == {}


def test_missing_series_does_not_use_an_ordinary_episode_as_the_tv_work(runtime, monkeypatch):
    mm = sys.modules[runtime[2].__name__ + '.mediaserver']
    reader = mm.MediaServerReader()
    reader.get_item = Mock(return_value={'Type':'Episode','ProviderIds':{'Imdb':'tt9999'}})
    assert reader.get_match_metadata('ep','TV',1,3) == {}


@pytest.mark.parametrize('raw,expected', [('37083974','37083974'),
    ('https://api.douban.com/movie/37083974','37083974'),
    ('https://movie.douban.com/subject/37083974/','37083974'),
    ('https://other.example/movie/37083974',None),('tt11540908',None)])
def test_imdb_lookup_parses_only_documented_douban_identifiers(runtime, monkeypatch, raw, expected):
    cm = sys.modules[runtime[2].__name__ + '.doubanclient']
    module = types.ModuleType('app.modules.douban.apiv2')
    api = Mock(); api.imdbid.return_value = {'id':raw,'title':'Carrie'}
    module.DoubanApi = Mock(return_value=api)
    monkeypatch.setitem(sys.modules, 'app.modules.douban.apiv2', module)
    title, subject = cm.DoubanClient().find_by_imdb('tt11540908')
    assert subject == expected
    api.imdbid.assert_called_once_with('tt11540908')


def test_imdb_lookup_rejects_a_response_with_conflicting_external_id(runtime, monkeypatch):
    cm = sys.modules[runtime[2].__name__ + '.doubanclient']
    module = types.ModuleType('app.modules.douban.apiv2')
    api = Mock(); api.imdbid.return_value = {'id':'1292664','title':'Carrie','attrs':{'imdb':['tt0074285']}}
    module.DoubanApi = Mock(return_value=api)
    monkeypatch.setitem(sys.modules, 'app.modules.douban.apiv2', module)
    assert cm.DoubanClient().find_by_imdb('tt11540908') == (None, None)


def matching_reader(**metadata):
    """
    只模拟影视元数据，禁止测试意外使用服务器的豆瓣编号兜底。
    """
    return Mock(get_match_metadata=Mock(return_value=metadata),
                get_douban_id=Mock(side_effect=AssertionError('must not fall back to another subject')))


def test_carrie_imdb_identity_wins_over_old_movie_id_and_same_title(runtime, monkeypatch):
    plugin, _, module, _ = runtime
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock(); client.find_by_imdb.return_value = ('Carrie','37083974')
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    payload = {'title':'魔女嘉莉','media_type':'TV','season':1,'episode':3,
               'item_id':'ep','subject_id':'1292664'}
    reader = matching_reader(imdb_id='tt11540908',tmdb_id='288673',media_type='TV',scope='series')
    assert plugin._match_subject(payload, reader) == ('Carrie','37083974')
    assert payload['matched_imdb'] == 'tt11540908' and payload['match_method'] == 'imdb'
    client.find_by_imdb.assert_called_once_with('tt11540908')
    assert plugin._match_subject(dict(payload),reader)[1] == '37083974'
    assert client.find_by_imdb.call_count == 1


def test_failed_identifier_lookup_does_not_search_same_title_or_accept_old_subject(runtime, monkeypatch):
    plugin, _, module, _ = runtime
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock(); client.find_by_imdb.return_value = (None, None)
    client.search = Mock(side_effect=AssertionError('no title fallback'))
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    reader = matching_reader(imdb_id='tt11540908',scope='series')
    payload = {'title':'魔女嘉莉','media_type':'TV','season':1,'subject_id':'1292664'}
    assert plugin._match_subject(payload, reader) == (None, None)
    assert '编号' in payload['subject_error']
    client.search.assert_not_called()
    assert not plugin.get_data('identifier_matches')


@pytest.mark.parametrize('media_type,expected', [('TV','tv'),('MOV','movie')])
def test_tmdb_conversion_preserves_type_namespace_and_verifies_returned_id(runtime,monkeypatch,media_type,expected):
    plugin, _, module, _ = runtime
    chain = types.ModuleType('app.sdk.chain' if 'plugins.v3' in str(ROOT) else 'app.chain')
    api = Mock(); api.tmdb_info.return_value = {'id':123,'external_ids':{'imdb_id':'tt456'}}
    chain.ChainBase = Mock(return_value=api)
    monkeypatch.setitem(sys.modules,chain.__name__,chain)
    monkeypatch.setattr(sys.modules['app.schemas.types'],'MediaType',types.SimpleNamespace(TV='tv',MOVIE='movie'),raising=False)
    metadata = {'tmdb_id':'123','media_type':media_type,'scope':'series' if media_type=='TV' else 'movie'}
    assert plugin._tmdb_imdb(metadata) == 'tt456'
    api.tmdb_info.assert_called_once_with(tmdbid=123,mtype=expected)
    api.tmdb_info.return_value['id'] = 789
    assert plugin._tmdb_imdb(metadata) == ''


def test_tmdb_only_metadata_is_converted_before_douban_lookup(runtime,monkeypatch):
    plugin, _, module, _ = runtime
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock(); client.find_by_imdb.return_value = ('Carrie','37083974')
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    plugin._tmdb_imdb = Mock(return_value='tt11540908')
    reader = matching_reader(tmdb_id='288673',media_type='TV',scope='series')
    payload = {'title':'魔女嘉莉','media_type':'TV','season':1}
    assert plugin._match_subject(payload,reader)[1] == '37083974'
    assert payload['match_method'] == 'tmdb_imdb'
    client.find_by_imdb.assert_called_once_with('tt11540908')


@pytest.mark.parametrize('season,title,accepted', [(1,'Show 第二季',False),(2,'Show',False),
    (2,'Show 第一季',False),(2,'Show 第二季',True),(3,'Show Season 3',True)])
def test_series_imdb_cannot_be_silently_reused_for_another_season(runtime,monkeypatch,season,title,accepted):
    plugin, _, module, _ = runtime
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock(); client.find_by_imdb.return_value = (title,'200')
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    result = plugin._match_subject({'title':'Show','media_type':'TV','season':season},
                                   matching_reader(imdb_id='tt123',scope='series'))
    assert bool(result[1]) is accepted


def test_old_pending_movie_match_is_revalidated_and_never_written_on_failure(runtime,monkeypatch):
    plugin, _, module, _ = runtime
    key = '魔女嘉莉@mp-server'
    record = {'title':'魔女嘉莉','source_title':'魔女嘉莉','type':'TV','source_media_type':'TV',
              'season':1,'episode':3,'subject_id':'1292664','server_name':'mp-server','item_id':'ep'}
    plugin.save_data('pending',{key:record})
    reader = matching_reader(imdb_id='tt11540908',scope='series')
    plugin._reader = Mock(return_value=reader)
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock();client.find_by_imdb.return_value=(None,None)
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    plugin._write_douban = Mock();plugin.retry_management = Mock();plugin._fill_missing_images = Mock()
    plugin.retry_pending()
    plugin._write_douban.assert_not_called()
    assert plugin.get_data('pending')[key]['subject_id'] == ''
    assert '编号' in plugin.get_data('pending')[key]['error']


def test_normal_tv_identifier_mapping_to_movie_is_blocked_before_writing(runtime,monkeypatch):
    plugin, _, module, _ = runtime
    client=Mock();client.get_subject_info.return_value={'title':'Same title','media_type':'MOV'}
    monkeypatch.setattr(module,'DoubanClient',Mock(return_value=client))
    def found(payload,reader):
        payload['match_method']='imdb'
        return 'Same title','1292664'
    plugin._match_subject=found;plugin._reader=Mock();plugin._write_douban=Mock()
    plugin._sync_item_original({'title':'Show','media_type':'TV','season':1,'episode':3,'server_name':'mp-server'})
    plugin._write_douban.assert_not_called()
    row=next(iter(plugin.get_data('pending').values()))
    assert '冲突' in row['error'] and not row['subject_id']


def test_unidentified_special_does_not_inherit_parent_series_douban_id(runtime):
    """
    S00 当前单集无标识时，不借用主剧的豆瓣条目。
    """
    plugin = runtime[0]
    reader = matching_reader(scope='episode',imdb_id='',tmdb_id='',douban_id='')
    assert plugin._match_subject({'title':'Special','media_type':'TV','season':0,'episode':7},reader) == (None,None)


def test_failed_imdb_mapping_can_recover_on_next_attempt(runtime,monkeypatch):
    """
    查询失败不能缓存为永久无条目，下一次应重新获取。
    """
    plugin, _, module, _ = runtime
    mm = sys.modules[module.__name__ + '.matching']
    client = Mock();client.find_by_imdb.side_effect = [(None,None),('Carrie','37083974')]
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    reader = matching_reader(imdb_id='tt11540908',scope='series')
    payload = {'title':'魔女嘉莉','media_type':'TV','season':1}
    assert plugin._match_subject(payload,reader) == (None,None)
    assert plugin._match_subject(payload,reader)[1] == '37083974'
    assert client.find_by_imdb.call_count == 2


def test_rescan_does_not_write_an_old_title_match_when_source_ids_disagree(runtime):
    """
    重扫发现源编号与旧档案不一致时保留记录，不修改豆瓣标记。
    """
    plugin = runtime[0]
    key = archive_record(plugin)
    plugin.data['archive'][key].update(type='电视剧',status='do',season=1,item_id='series')
    plugin.save_data('subject_details',{'100':{'title':'Show','media_type':'TV','expires':9999999999}})
    reader = Mock();reader.get_series_id.return_value = 'series'
    plugin._reader = Mock(return_value=reader)
    plugin._tv_completion = Mock(return_value={'complete':True,'known':True,'episodes':[1,2],'played':[1,2],'expected':2})
    plugin._match_subject = Mock(return_value=('Correct version','200'))
    plugin._write_douban = Mock()
    result = plugin.rescan_archive()
    plugin._write_douban.assert_not_called()
    assert result['skipped'] == 1 and plugin.get_data('archive')[key]['subject_id'] == '100'


def test_read_only_diagnosis_skips_guarded_record_without_counter_error(runtime):
    """
    人工处理中或已停用的记录在诊断接口中直接跳过，不访问不存在的重扫计数器。
    """
    plugin = runtime[0]
    archive_record(plugin)
    plugin._manual_guard = Mock(return_value=True)
    plugin._available_libraries = Mock(return_value=[])
    assert plugin.api_diagnose()['items'] == []


def name_year_client(runtime,monkeypatch,candidates):
    """
    模拟公开候选及详情，不访问豆瓣或宿主数据库。
    """
    mm = sys.modules[runtime[2].__name__ + '.matching']
    client=Mock();client.search_candidates.return_value=candidates
    client.get_subject_info.side_effect=lambda subject,kind: next(dict(item) for item in candidates if item['subject_id']==subject)
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    return client


@pytest.mark.parametrize('kind,candidate_kind,accepted',[('MOV','MOV',True),('MOV','TV',False),('TV','MOV',False),('TV','TV',True)])
def test_title_year_fallback_requires_exact_type_for_ordinary_works(runtime,monkeypatch,kind,candidate_kind,accepted):
    """
    名称及年份相同也不能混淆电影和普通电视剧。
    """
    client=name_year_client(runtime,monkeypatch,[{'subject_id':'200','title':'Carrie','year':2026,'media_type':candidate_kind}])
    payload={'title':'魔女嘉莉','media_type':kind,'season':1 if kind=='TV' else 0}
    result=runtime[0]._match_by_title_year(payload,{'title':'魔女嘉莉','original_title':'Carrie','year':2026})
    assert bool(result[1]) is accepted
    if accepted:assert payload['match_method']=='title_year' and payload['match_year']==2026


@pytest.mark.parametrize('year,title',[(1976,'Carrie'),(2026,'Carrie 2')])
def test_title_year_does_not_accept_wrong_year_or_partial_title(runtime,monkeypatch,year,title):
    """
    拒绝旧版同名电影和仅包含主标题的续作。
    """
    name_year_client(runtime,monkeypatch,[{'subject_id':'1292664','title':title,'year':year,'media_type':'MOV'}])
    result=runtime[0]._match_by_title_year({'title':'Carrie','media_type':'MOV'}, {'title':'Carrie','year':2026})
    assert result==(None,None)


def test_ambiguous_exact_title_year_results_require_manual_choice(runtime,monkeypatch):
    """
    两个条目均满足名称、年份和类型时，不选搜索首条。
    """
    name_year_client(runtime,monkeypatch,[{'subject_id':str(n),'title':'Carrie','year':2026,'media_type':'MOV'} for n in [100,200]])
    payload={'title':'Carrie','media_type':'MOV'}
    assert runtime[0]._match_by_title_year(payload,{'title':'Carrie','year':2026})==(None,None)
    assert '唯一' in payload['subject_error']


def test_second_season_uses_tmdb_season_air_year_instead_of_emby_series_year(runtime,monkeypatch):
    """
    Emby 将 S02 年份沿用 S01 时，以 TMDB 第二季首播日期为依据。
    """
    plugin=runtime[0]
    plugin._tmdb_detail=Mock(return_value={'season_number':2,'air_date':'2028-05-12'})
    client=name_year_client(runtime,monkeypatch,[{'subject_id':'200','title':'Example 第二季','year':2028,'media_type':'TV'}])
    payload={'title':'Example','media_type':'TV','season':2}
    metadata={'title':'Example','year':2026,'season_year':2026,'tmdb_id':'123','scope':'series','media_type':'TV'}
    assert plugin._match_by_title_year(payload,metadata)[1]=='200'
    assert payload['match_year']==2028
    assert all(call.args[1]==2028 for call in client.search_candidates.call_args_list)
    plugin._tmdb_detail.assert_called_once_with(metadata,2)


def test_second_season_without_reliable_year_never_uses_first_season_year(runtime,monkeypatch):
    """
    无当前季日期且 Emby 仍是第一季年份时，保留待匹配。
    """
    plugin=runtime[0];plugin._tmdb_detail=Mock(return_value=None)
    client=name_year_client(runtime,monkeypatch,[])
    payload={'title':'Example','media_type':'TV','season':2}
    assert plugin._match_by_title_year(payload,{'title':'Example','year':2026,'season_year':2026,'tmdb_id':'123','scope':'series'})==(None,None)
    assert '当前季' in payload['subject_error']
    client.search_candidates.assert_not_called()


@pytest.mark.parametrize('title,accepted',[('Example',False),('Example 第一季',False),('Example 第二季',True)])
def test_same_year_cannot_replace_explicit_season_verification(runtime,monkeypatch,title,accepted):
    """
    即使年份正确，后续季仍必须有对应季号。
    """
    plugin=runtime[0];plugin._season_year=Mock(return_value=2028)
    name_year_client(runtime,monkeypatch,[{'subject_id':'200','title':title,'year':2028,'media_type':'TV'}])
    assert bool(plugin._match_by_title_year({'title':'Example','media_type':'TV','season':2},{'title':'Example','year':2026})[1]) is accepted


@pytest.mark.parametrize('kind',['MOV','TV'])
def test_special_title_year_can_match_a_douban_movie_or_special_series(runtime,monkeypatch,kind):
    """
    S00 的特别篇使用本篇名称及年份，可对应影片或独立剧集。
    """
    client=name_year_client(runtime,monkeypatch,[{'subject_id':'200','title':'特别篇作品','year':2024,'media_type':kind}])
    payload={'title':'主剧','media_type':'TV','season':0,'episode':36}
    result=runtime[0]._match_by_title_year(payload,{'title':'特别篇作品','series_title':'主剧','year':2024,'scope':'episode'})
    assert result[1]=='200'
    assert all(call.args[0]=='特别篇作品' for call in client.search_candidates.call_args_list)


def test_imdb_and_tmdb_attempts_precede_title_year_fallback(runtime,monkeypatch):
    """
    Emby IMDb 无结果时仍尝试 TMDB 转换的 IMDb，再决定是否按名称兜底。
    """
    plugin,_,module,_=runtime
    mm=sys.modules[module.__name__+'.matching']
    client=Mock();client.find_by_imdb.side_effect=[(None,None),('Carrie','37083974')]
    monkeypatch.setattr(mm,'DoubanClient',Mock(return_value=client))
    plugin._tmdb_imdb=Mock(return_value='tt11540908')
    plugin._match_by_title_year=Mock(side_effect=AssertionError('do not fall back before both identifiers'))
    result=plugin._match_subject({'title':'Carrie','media_type':'TV','season':1},matching_reader(imdb_id='tt9999',tmdb_id='288673',scope='series'))
    assert result[1]=='37083974'
    assert [call.args[0] for call in client.find_by_imdb.call_args_list]==['tt9999','tt11540908']


def test_both_identifier_failures_reach_exact_title_year_fallback(runtime,monkeypatch):
    """
    两种外部编号均无结果后，允许唯一且严格一致的名称年份候选。
    """
    plugin=runtime[0]
    client=name_year_client(runtime,monkeypatch,[{'subject_id':'200','title':'Carrie','year':2026,'media_type':'MOV'}])
    client.find_by_imdb.return_value=(None,None)
    plugin._tmdb_imdb=Mock(return_value='tt456')
    metadata={'title':'Carrie','year':2026,'imdb_id':'tt123','tmdb_id':'123','scope':'movie','media_type':'MOV'}
    payload={'title':'Carrie','media_type':'MOV'}
    assert plugin._match_subject(payload,matching_reader(**metadata))[1]=='200'
    assert [call.args[0] for call in client.find_by_imdb.call_args_list]==['tt123','tt456']
    assert payload['match_method']=='title_year'


def test_emby_current_season_year_is_used_when_tmdb_is_unavailable(runtime):
    """
    当前季年份明确不同于主剧首播年时，可使用 Emby 当前季年份。
    """
    plugin=runtime[0];plugin._tmdb_detail=Mock(return_value=None)
    assert plugin._season_year({'scope':'series','tmdb_id':'123','year':2026,'season_year':2028},2)==2028


def test_tmdb_can_confirm_two_seasons_released_in_same_calendar_year(runtime):
    """
    TMDB 明确第二季首播日期时，即使与第一季同年也可确认。
    """
    plugin=runtime[0];plugin._tmdb_detail=Mock(return_value={'season_number':2,'air_date':'2026-11-01'})
    assert plugin._season_year({'scope':'series','tmdb_id':'123','year':2026,'season_year':2026},2)==2026


def test_current_season_metadata_does_not_use_series_production_year(runtime,monkeypatch):
    """
    普通剧集保留 Series 编号，同时单独读取当前季的年份。
    """
    mm=sys.modules[runtime[2].__name__+'.mediaserver'];reader=mm.MediaServerReader()
    items={'ep':{'Type':'Episode','SeriesId':'show','SeasonId':'s2'},
           'show':{'Type':'Series','Name':'Example','ProductionYear':2026,'ProviderIds':{'Tmdb':'123'}},
           's2':{'Type':'Season','IndexNumber':2,'ProductionYear':2026,'PremiereDate':'2028-05-01'}}
    reader.get_item=Mock(side_effect=lambda key:items.get(key,{}))
    result=reader.get_match_metadata('ep','TV',2,3)
    assert result['year']==2026 and result['season_year']=='2028' and result['tmdb_id']=='123'


def test_special_generic_episode_name_cannot_fall_back_to_parent_show_title(runtime,monkeypatch):
    """
    S00 只有泛化集名时，不能拿观看事件中的主剧名寻找豆瓣主剧条目。
    """
    client=name_year_client(runtime,monkeypatch,[{'subject_id':'100','title':'Parent Show','year':2026,'media_type':'TV'}])
    payload={'title':'Parent Show','media_type':'TV','season':0,'episode':3}
    result=runtime[0]._match_by_title_year(payload,{'title':'Episode 3','year':2026,'scope':'episode'})
    assert result==(None,None)
    client.search_candidates.assert_not_called()

# 神医助手 Cookie 同步只在隔离边界模拟网络与凭据。
def cookie_module(runtime):
    return sys.modules[runtime[2].__name__ + '.embycookie']


def configure_cookie_target(runtime, **overrides):
    body = dict(action='save', url='http://emby.test:8096', username='admin', password='secret', enabled=True)
    body.update(overrides)
    assert runtime[0].api_cookie_target(body)['success']
    return cookie_module(runtime)


def test_cookie_target_is_private_and_independent(runtime):
    module = configure_cookie_target(runtime)
    plugin = runtime[0]
    public = plugin.api_management()['cookie_target']
    assert public['password_saved'] and public['enabled']
    assert not {'password', 'token', 'cookie'} & public.keys()
    assert plugin.config == {}
    assert not any(job[0] == 'emby_cookie_forward' for job in runtime[3])
    assert plugin.get_data(module.TARGET)['password'] == 'secret'
    route = next(r for r in plugin.management_api() if r['path'] == '/cookie-target')
    assert route['dependencies'] and route['auth'] == 'bear'


def test_cookie_password_blank_retains_only_for_same_target(runtime):
    module = configure_cookie_target(runtime)
    configure_cookie_target(runtime, password='')
    assert runtime[0].get_data(module.TARGET)['password'] == 'secret'
    configure_cookie_target(runtime, password='', url='http://different.test')
    assert not runtime[0].get_data(module.TARGET)['password']


@pytest.mark.parametrize('url', ['ftp://emby.test', 'https://u:p@emby.test', 'http://emby.test?a=b', 'http://emby.test/#part', 'http://emby.test:bad', 'http://em\nby.test'])
def test_cookie_target_invalid_url(runtime, url):
    assert not runtime[0].api_cookie_target(dict(url=url, username='admin'))['success']


def install_cookie_client(runtime, monkeypatch, failure=None, hook=None):
    module = cookie_module(runtime)
    calls = []
    class Client:
        def __init__(self, config, session, guard): self.config, self.guard = config, guard
        def authenticate(self): return dict(token='own-session', user_id='adminid', target_key=module.target_key(self.config))
        def sync(self, cookie):
            if hook: hook()
            if self.guard(): calls.append(cookie)
            if failure: raise RuntimeError(failure)
    monkeypatch.setattr(module, 'EmbyAssistantClient', Client)
    return calls


def test_cookie_auto_queue_deduplicates_and_new_cookie_triggers(runtime, monkeypatch):
    module = configure_cookie_target(runtime)
    calls = install_cookie_client(runtime, monkeypatch)
    plugin = runtime[0]
    plugin._on_verified_cookie('valid-cookie-a')
    count = len(runtime[3])
    plugin._on_verified_cookie('valid-cookie-a')
    assert len(runtime[3]) == count
    runtime[3][-1][1]()
    assert calls == ['valid-cookie-a']
    assert plugin.get_data(module.RESULT)['status'] == '同步成功'
    plugin._on_verified_cookie('valid-cookie-a')
    assert len(runtime[3]) == count
    plugin._on_verified_cookie('valid-cookie-b')
    runtime[3][-1][1]()
    assert calls == ['valid-cookie-a', 'valid-cookie-b']


def test_cookie_auto_off_manual_still_queues_with_current_form(runtime, monkeypatch):
    module = configure_cookie_target(runtime, enabled=False)
    plugin = runtime[0]
    calls = install_cookie_client(runtime, monkeypatch)
    plugin._on_verified_cookie('not-sent')
    assert not any(j[0] == 'emby_cookie_forward' for j in runtime[3])
    plugin._manual_forward_cookie = Mock(return_value='manually-verified-cookie')
    configure_cookie_target(runtime, action='sync', enabled=False, url='http://new-target.test')
    runtime[3][-1][1]()
    assert calls == ['manually-verified-cookie']
    assert plugin.get_data(module.TARGET)['url'] == 'http://new-target.test'


@pytest.mark.parametrize('invalidate', ['target', 'stop', 'new-cookie'])
def test_old_cookie_task_cannot_write_after_change(runtime, monkeypatch, invalidate):
    configure_cookie_target(runtime)
    calls = install_cookie_client(runtime, monkeypatch)
    plugin = runtime[0]
    plugin._on_verified_cookie('old-cookie')
    old = runtime[3][-1][1]
    if invalidate == 'target': configure_cookie_target(runtime, url='http://another.test')
    elif invalidate == 'stop': plugin._stop_cookie_forward()
    else: plugin._on_verified_cookie('new-cookie')
    old()
    assert calls == []


def test_cookie_failure_does_not_break_douban_login_and_has_cooldown(runtime, monkeypatch):
    module = configure_cookie_target(runtime)
    calls = install_cookie_client(runtime, monkeypatch, failure='Emby 连接失败')
    plugin = runtime[0]
    plugin._on_verified_cookie('valid-cookie')
    runtime[3][-1][1]()
    assert calls == ['valid-cookie']
    assert plugin.get_data(module.RESULT)['status'] == '同步失败'
    count = len(runtime[3]); plugin._on_verified_cookie('valid-cookie')
    assert len(runtime[3]) == count
    monkeypatch.setattr(plugin, '_queue_cookie_forward', Mock(side_effect=RuntimeError('queue unavailable')))
    plugin._on_verified_cookie('valid-cookie')  # Cannot raise into the successful Douban operation.


@pytest.mark.parametrize('valid,expected', [(True, True), (False, False), (None, False)])
def test_only_verified_cookie_is_forwarded(runtime, monkeypatch, valid, expected):
    configure_cookie_target(runtime)
    plugin = runtime[0]
    client = runtime[1].DoubanClient(cookie='dbcl2=simulated; ck=simulated')
    plugin._remember_cookie(client, plugin._credential_key, valid)
    assert any(j[0] == 'emby_cookie_forward' for j in runtime[3]) is expected


def test_manual_cookie_priority_and_uncertain_state_never_written(runtime, monkeypatch):
    configure_cookie_target(runtime)
    module, plugin = cookie_module(runtime), runtime[0]
    plugin._cookie = 'manual'; plugin._auth_record = Mock(return_value={'cookie': 'saved'})
    seen = []
    def factory(**kwargs):
        seen.append(kwargs['cookie'])
        return types.SimpleNamespace(login_state=lambda: kwargs['cookie'] == 'saved', cookie_header=lambda: kwargs['cookie'])
    monkeypatch.setattr(module, 'DoubanClient', factory)
    assert plugin._manual_forward_cookie() == 'saved' and seen == ['manual', 'saved']
    monkeypatch.setattr(module, 'DoubanClient', lambda **kwargs: types.SimpleNamespace(login_state=lambda: None))
    with pytest.raises(RuntimeError, match='无法确认'): plugin._manual_forward_cookie()


def assistant_view(cookie='old'):
    return {'PageId': '63c322:MetadataEnhancePageView', 'EditObjectContainer': {'Object': {
        'EnableDoubanAssistScraping': True, 'Other': [1, 2], 'MetadataRefreshOptions': {
            'DoubanCookie': cookie, 'DoubanAccessToken': 'preserved', 'RatingUpdateLookbackDays': 3}}},
        'Commands': [{'CommandId': 'PageSave', 'IsEnabled': True}]}


def test_assistant_client_only_changes_cookie_and_checks_readback(runtime):
    module = cookie_module(runtime)
    config = dict(url='http://emby.test', username='admin', password='secret')
    client = module.EmbyAssistantClient(config, {})
    client.session = {'user_id': 'adminid'}
    view = assistant_view(); calls = []
    def request(path, data=None):
        calls.append((path, copy.deepcopy(data)))
        if path.startswith('web/configurationpages'):
            return [{'PluginId': module.PLUGIN_ID, 'Name': 'MetadataEnhancePageView', 'Href': 'genericui?PageId=63c322%3AMetadataEnhancePageView'}]
        if path == 'UI/Command':
            assert data['CommandId'] == 'PageSave'
            updated = __import__('json').loads(data['Data'])
            expected = copy.deepcopy(view['EditObjectContainer']['Object'])
            expected['MetadataRefreshOptions']['DoubanCookie'] = 'new'
            assert updated == expected
            view['EditObjectContainer']['Object'] = updated
            return view
        return copy.deepcopy(view)
    client._request = request
    client.sync('new')
    assert len([p for p, _ in calls if p.startswith('UI/View')]) == 2
    assert not any('cookie=' in p.lower() for p, _ in calls)


@pytest.mark.parametrize('mode', ['missing-page', 'missing-field', 'disabled-save', 'readback-failed'])
def test_assistant_incompatible_protocol_does_not_claim_success(runtime, mode):
    module = cookie_module(runtime)
    client = module.EmbyAssistantClient(dict(url='http://emby.test', username='admin'), {})
    client.session = {'user_id': 'adminid'}
    view = assistant_view(); writes = []
    if mode == 'missing-field': del view['EditObjectContainer']['Object']['MetadataRefreshOptions']['DoubanCookie']
    if mode == 'disabled-save': view['Commands'][0]['IsEnabled'] = False
    def request(path, data=None):
        if path.startswith('web/configurationpages'):
            return [] if mode == 'missing-page' else [{'PluginId': module.PLUGIN_ID, 'Name': 'MetadataEnhancePageView', 'Href': 'genericui?PageId=63c322%3AMetadataEnhancePageView'}]
        if data: writes.append(data)
        return copy.deepcopy(view)
    client._request = request
    with pytest.raises(RuntimeError): client.sync('new')
    assert len(writes) == (1 if mode == 'readback-failed' else 0)


def test_assistant_auth_is_bound_to_admin_and_target(runtime):
    module = cookie_module(runtime)
    config = dict(url='http://emby.test', username='admin', password='secret')
    client = module.EmbyAssistantClient(config, {'token': 'foreign', 'target_key': 'wrong', 'user_id': 'other'})
    calls = []
    def request(path, data=None):
        calls.append(path)
        assert not client.token
        return {'AccessToken': 'own', 'User': {'Id': 'id', 'Name': 'admin', 'Policy': {'IsAdministrator': True}}}
    client._request = request
    session = client.authenticate()
    assert calls == ['Users/AuthenticateByName'] and session['token'] == 'own'
    assert session['target_key'] == module.target_key(config)
    client._request = Mock(return_value={'AccessToken': 'ordinary', 'User': {'Name': 'admin', 'Policy': {'IsAdministrator': False}}})
    client.session = {}
    with pytest.raises(PermissionError): client.authenticate()


def test_cookie_scheduler_failure_can_be_retried_manually(runtime, monkeypatch):
    module = configure_cookie_target(runtime)
    plugin = runtime[0]
    schedule = plugin._schedule_once
    monkeypatch.setattr(plugin, '_schedule_once', Mock(side_effect=RuntimeError('unavailable')))
    plugin._on_verified_cookie('valid')
    assert plugin._cookie_forward_pending is None
    assert plugin.get_data(module.RESULT)['status'] == '同步失败'
    monkeypatch.setattr(plugin, '_schedule_once', schedule)
    plugin._queue_cookie_forward('', manual=True)
    assert runtime[3][-1][0] == 'emby_cookie_forward'


def test_cookie_browser_login_result_triggers_only_on_success(runtime, monkeypatch):
    configure_cookie_target(runtime)
    plugin = runtime[0]
    plugin._auth_ready = lambda: True
    plugin._save_auth_result({'status': '需要验证', 'cookie': 'unverified'}, plugin._credential_key, {})
    assert not any(j[0] == 'emby_cookie_forward' for j in runtime[3])
    plugin._save_auth_result({'status': '登录正常', 'cookie': 'verified'}, plugin._credential_key, {})
    assert runtime[3][-1][0] == 'emby_cookie_forward'


def test_assistant_http_boundary_redacts_errors_and_never_redirects(runtime):
    module = cookie_module(runtime)
    client = module.EmbyAssistantClient(dict(url='http://emby.test', username='admin'), {})
    client.token = 'sensitive-token'
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            raise module.urllib.error.HTTPError(request.full_url, 401, 'secret-response', {}, None)
    client.opener = Opener()
    with pytest.raises(PermissionError) as error: client._request('UI/Command', {'Data': 'sensitive-cookie'})
    assert 'sensitive' not in str(error.value) and 'secret' not in str(error.value)
    assert seen[0].headers['X-emby-token'] == 'sensitive-token'
    assert 'sensitive' not in seen[0].full_url
    assert module._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://different.test') is None
    client.guard = lambda: False
    with pytest.raises(RuntimeError, match='取消'): client._request('UI/Command', {})
    assert len(seen) == 1
