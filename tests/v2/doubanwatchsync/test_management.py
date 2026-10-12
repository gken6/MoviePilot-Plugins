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
    for suffix in ('doubanclient', 'mediaserver', 'management', 'auth', 'qrnotify', 'compat'):
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


def fake_client(monkeypatch, management, *, remove=True, write=True, detail=True):
    client = management.DoubanClient(cookie='dbcl2=local; ck=local')
    client.login_state = Mock(return_value=True)
    client.get_subject_info = Mock(return_value={'title': 'correct title', 'image': 'https://img.example.test/cover.jpg'} if detail else None)
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
    key = 'pending'
    plugin.save_data('pending', {key: {'title': 'source', 'type': media_type, 'status': 'do'}})
    client = fake_client(monkeypatch, management)
    result = plugin.api_match({'key': key, 'subject_id': '200', 'status': status})
    assert result['success'] is valid
    if valid:
        plugin._run_correction(key)
        client.set_status.assert_called_once_with('200', status, True)
        assert plugin.data['archive'][key]['status'] == status
        client.remove_status.assert_not_called()
    else:
        client.set_status.assert_not_called()


def test_manual_same_subject_can_change_in_progress_to_watched(runtime, monkeypatch):
    plugin, management, _, _ = runtime
    key = archive_record(plugin)
    archive = plugin.get_data('archive'); archive[key].update(type='电视剧', status='do')
    plugin.save_data('archive', archive)
    client = fake_client(monkeypatch, management)
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
    monkeypatch.setattr(plugin, '_search_subject', lambda *args: (None, None))
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
    client.get_subject_info = Mock(return_value={'title': 'correct title', 'image': ''})
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
