"""隔离测试：所有宿主存储、媒体服务器和豆瓣请求均在边界模拟。"""
import copy
import os
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(os.environ.get("WATCHSYNC_TEST_ROOT", Path(__file__).resolve().parents[3] / 'plugins.v3' / 'doubanwatchsync'))


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
    # Imports are restored by monkeypatch along with SDK stubs after every test.
    name = '_test_watchsync'
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    plugin_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, plugin_module)
    for suffix in ('doubanclient', 'mediaserver', 'management'):
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
    client = Mock()
    client.has_login.return_value = True
    client.get_subject_info.return_value = {'title': 'correct title', 'image': 'https://img.example.test/cover.jpg'} if detail else None
    client.set_status.return_value = write
    client.remove_status.return_value = remove
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
