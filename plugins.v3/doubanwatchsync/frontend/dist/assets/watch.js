import { share } from './remoteEntry.js';
const { h, ref, reactive, computed, onMounted, onUnmounted, watch, resolveComponent, defineComponent } = await share('vue');

const iconPaths = {
  edit: 'M16.5 3.5l4 4L8 20H4v-4L16.5 3.5zm-1.4 4.2L6 16.8V18h1.2l9.1-9.1-1.2-1.2z',
  delete: 'M6 7h12l-1 14H7L6 7zm2-4h8l1 2h4v2H3V5h4l1-2zm1 6v10h2V9H9zm4 0v10h2V9h-2z',
  add: 'M11 4h2v7h7v2h-7v7h-2v-7H4v-2h7V4z',
  check: 'M9 16.2l-4.2-4.2-1.4 1.4L9 19l12-12-1.4-1.4L9 16.2z',
  close: 'M6.4 5L12 10.6 17.6 5 19 6.4 13.4 12 19 17.6 17.6 19 12 13.4 6.4 19 5 17.6 10.6 12 5 6.4z',
  refresh: 'M17.6 6.4A8 8 0 103 12h2a6 6 0 116 5.7V21l6-4-6-4v2.6a4 4 0 10-4-3.6H3a8 8 0 0114.6-5.6z',
};
const icon = (name) => h('svg', { viewBox: '0 0 24 24', width: 19, height: 19, 'aria-hidden': 'true' }, [h('path', { d: iconPaths[name], fill: 'currentColor' })]);
function button(text, action, options = {}) {
  return h('button', { type: 'button', class: ['dw-button', options.primary && 'dw-primary', options.danger && 'dw-danger', options.icon && 'dw-icon'],
    onClick: action, disabled: options.disabled, title: options.title || text, 'aria-label': options.title || text }, options.icon ? icon(options.icon) : text);
}
function field(label, value, change, options = {}) {
  return h('label', { class: 'dw-field' }, [h('span', label), h('input', { value, type: options.type || 'text', placeholder: options.placeholder || label,
    autocomplete: options.autocomplete || 'off', 'aria-label': label, onInput: event => change(event.target.value),
    onKeydown: options.enter ? event => { if (event.key === 'Enter') { event.preventDefault(); options.enter(); } } : undefined })]);
}
function status(value) {
  const kind = value === '正常' ? 'good' : value === '获取失败' ? 'bad' : value === '正在获取token' ? 'busy' : 'muted';
  return h('span', { class: ['dw-status', `dw-${kind}`] }, [h('i', { class: kind === 'busy' ? 'dw-spinner' : 'dw-dot' }), value || '未启用']);
}
function apiValue(result) { return result && Object.prototype.hasOwnProperty.call(result, 'data') && typeof result.success === 'boolean' ? result.data : result; }
function apiPath(props, path) { return `plugin/${encodeURIComponent(props.pluginId || 'DoubanWatchSync')}/${path}`; }
async function request(props, method, path, data) {
  const result = await props.api[method](apiPath(props, path), ...(data === undefined ? [] : [data]));
  if (result?.success === false) throw new Error(result.message || '操作未完成');
  const value = apiValue(result);
  if (value?.success === false) throw new Error(value.message || '操作未完成');
  return value;
}
function changed() { window.dispatchEvent(new Event('douban-watch-updated')); }
function useManagement(props) {
  const data = ref({ servers: [], archive: [], pending: [], unmatched: [] });
  const message = ref('');
  const failed = ref(false);
  const active = ref(false);
  let timer, stopped = false;
  async function load() {
    if (active.value || stopped) return;
    active.value = true;
    try { data.value = await request(props, 'get', 'manage'); }
    catch (error) { message.value = error.message || '读取数据失败'; failed.value = true; }
    finally { active.value = false; }
  }
  async function mutate(path, body) {
    message.value = ''; failed.value = false;
    try {
      const result = await request(props, 'post', path, body);
      message.value = result.message || '已保存';
      await load(); changed(); return true;
    } catch (error) { message.value = error.message || '操作失败'; failed.value = true; return false; }
  }
  onMounted(() => { load(); timer = setInterval(load, 3000); });
  onUnmounted(() => { stopped = true; clearInterval(timer); });
  return { data, message, failed, load, mutate };
}
const notification = (state) => state.message.value ? h('div', { role: 'status', 'aria-live': 'polite', class: ['dw-notice', state.failed.value && 'dw-error'] }, state.message.value) : null;

const ServerManager = defineComponent({
  props: ['api', 'pluginId'],
  setup(props) {
    const state = useManagement(props);
    const form = reactive({ id: '', name: '', url: '', username: '', password: '', enabled: true });
    const saving = ref(false), deleting = ref('');
    function reset() { Object.assign(form, { id: '', name: '', url: '', username: '', password: '', enabled: true }); }
    function edit(server) { Object.assign(form, { id: server.id, name: server.name, url: server.url, username: server.username, password: '', enabled: server.enabled }); deleting.value = ''; }
    async function save() {
      if (saving.value) return;
      saving.value = true;
      if (await state.mutate('servers', { action: 'save', ...form })) reset();
      saving.value = false;
    }
    return () => h('section', { class: 'dw-servers' }, [
      h('div', { class: 'dw-section-head' }, [h('div', [h('h3', '直连 Emby 服务器'), h('p', '独立连接 · 添加、编辑和启停即时生效')]),
        button('刷新服务器列表', state.load, { icon: 'refresh' })]),
      h('div', { class: 'dw-server-table' }, [
        h('div', { class: 'dw-server-head' }, ['服务器', '状态', '影片数量', '剧集数量', '操作', '启用'].map(text => h('span', text))),
        ...(state.data.value.servers.length ? state.data.value.servers.map(server => h('div', { class: 'dw-server-row', key: server.id }, [
          h('div', { class: 'dw-server-name' }, [h('strong', server.name), h('span', { title: server.url }, server.url)]),
          h('div', { class: 'dw-server-state' }, [status(server.status), server.error ? h('small', server.error) : null,
            server.status === '获取失败' ? button('重试', () => state.mutate('servers', { action: 'refresh', id: server.id })) : null]),
          h('span', { class: 'dw-count', 'data-label': '影片' }, server.movies == null ? '—' : Number(server.movies).toLocaleString()),
          h('span', { class: 'dw-count', 'data-label': '剧集' }, server.series == null ? '—' : Number(server.series).toLocaleString()),
          deleting.value === server.id ? h('div', { class: 'dw-row-actions dw-delete-confirm' }, [
            button('删除', async () => { if (await state.mutate('servers', { action: 'delete', id: server.id })) { if (form.id === server.id) reset(); deleting.value = ''; } }, { danger: true }),
            button('取消', () => deleting.value = '')]) : h('div', { class: 'dw-row-actions' }, [
              button('编辑 ' + server.name, () => edit(server), { icon: 'edit' }),
              button('删除 ' + server.name, () => deleting.value = server.id, { icon: 'delete' })]),
          h('label', { class: 'dw-toggle', title: server.enabled ? '停用服务器' : '启用服务器' }, [h('input', { type: 'checkbox', checked: !!server.enabled,
            role: 'switch', 'aria-label': '启用 ' + server.name, onChange: event => state.mutate('servers', { action: 'toggle', id: server.id, enabled: event.target.checked }) }), h('span')]),
        ])) : [h('div', { class: 'dw-empty' }, '暂无直连服务器，在下方填写后点击 ＋ 添加')]),
      ]),
      h('div', { class: 'dw-add-title' }, [h('strong', form.id ? '编辑直连服务器' : '添加直连服务器'),
        form.id ? button('取消编辑', reset) : h('small', '填写账号后自动获取 Token 和用户 ID')]),
      h('form', { class: 'dw-add-form', onSubmit: event => { event.preventDefault(); save(); } }, [
        field('服务器名', form.name, value => form.name = value, { enter: save }),
        field('地址', form.url, value => form.url = value, { placeholder: 'https://emby.example.com', enter: save }),
        field('用户名', form.username, value => form.username = value, { enter: save }),
        field('密码', form.password, value => form.password = value, { type: 'password', placeholder: form.id ? '留空保留原密码' : '账号密码', enter: save, autocomplete: 'new-password' }),
        button(form.id ? '保存修改' : '添加服务器', save, { icon: saving.value ? undefined : form.id ? 'check' : 'add', primary: true, disabled: saving.value }),
      ]), notification(state),
    ]);
  }
});

// Render the original MP configuration schema with its original model bindings.
function renderNative(node, model) {
  if (!node) return null;
  const props = { ...(node.props || {}) };
  if (props.model) {
    const key = props.model; delete props.model;
    props.modelValue = model[key]; props['onUpdate:modelValue'] = value => model[key] = value;
  }
  const children = node.text || props.text || (node.content || []).map(child => renderNative(child, model));
  delete props.text;
  if (node.html) props.innerHTML = node.html;
  const component = node.component?.startsWith('V') ? resolveComponent(node.component) : node.component || 'div';
  return h(component, props, node.html ? undefined : typeof component === 'string' ? children : { default: () => children });
}
function formFields(row, models) {
  return row ? { ...row, content: (row.content || []).map(column => ({ ...column,
    content: (column.content || []).filter(field => models.includes(field.props?.model))
  })).filter(column => column.content.length) } : null;
}
export const Config = defineComponent({
  props: ['initialConfig', 'api', 'pluginId'], emits: ['save', 'close', 'switch', 'layout'],
  setup(props, { emit }) {
    const model = reactive({ ...(props.initialConfig || {}) });
    const rows = ref([]), error = ref(''), saving = ref(false);
    const login = useManagement(props);
    onMounted(async () => {
      emit('layout', { maxWidth: '76rem' });
      try {
        const form = apiValue(await props.api.get(`plugin/form/${encodeURIComponent(props.pluginId || 'DoubanWatchSync')}`));
        rows.value = form.conf?.[0]?.content || [];
        if (!rows.value.length) throw new Error('配置控件加载失败，请刷新后重试');
        Object.assign(model, { ...form.model, ...(props.initialConfig || {}) });
      } catch (e) { error.value = e.message || '配置加载失败'; }
    });
    async function save() {
      saving.value = true; error.value = '';
      try {
        const result = await props.api.put(`plugin/${encodeURIComponent(props.pluginId || 'DoubanWatchSync')}`, { ...model });
        if (result?.success === false) throw new Error(result.message || '保存失败');
        changed(); emit('close');
      } catch (e) { error.value = e.message || '设置保存失败'; }
      finally { saving.value = false; }
    }
    return () => h('div', { class: 'dw-root dw-config' }, [
      h('header', { class: 'dw-header' }, [h('div', [h('h2', '豆瓣观看同步'), h('p', '连接媒体服务器，管理观看记录')]), button('关闭', () => emit('close'), { icon: 'close' })]),
      h('div', { class: 'dw-config-body' }, [
        h('section', { class: 'dw-native' }, [h('h3', 'MoviePilot 媒体服务器'), ...rows.value.slice(0, 4).map(row => renderNative(row, model))]),
        h(ServerManager, { api: props.api, pluginId: props.pluginId }),
        h('section', { class: 'dw-native dw-settings' }, [h('h3', '豆瓣登录'),
          ...rows.value.slice(6, 7).map(row => renderNative(row, model)),
          renderNative(formFields(rows.value[7], ['use_cookiecloud']), model),
          ...rows.value.slice(11, 12).map(row => renderNative(row, model)),
          ...(model.auto_login ? rows.value.slice(12).map(row => renderNative(row, model)) : []),
          h('p', { class: 'dw-subtle' }, model.auto_login ? '实际同步时检查登录态，Cookie 过期后自动重新获取并重试当前同步。未通过安全验证时暂停续登，观看记录保留待同步。' : '手填 Cookie 留空时，从 MoviePilot 的 CookieCloud 获取豆瓣登录态。'),
          h('div', { class: 'dw-notice', role: 'status', 'aria-live': 'polite' }, [
            h('strong', login.data.value.login?.status || '读取登录状态…'),
            login.data.value.login?.message ? h('p', login.data.value.login.message) : null,
            login.data.value.login?.last_login ? h('small', `上次自动登录：${new Date(login.data.value.login.last_login * 1000).toLocaleString()}`) : null,
            login.data.value.login?.next_attempt ? h('small', ` · 重试冷却至：${new Date(login.data.value.login.next_attempt * 1000).toLocaleString()}`) : null,
          ]),
        ]),
        h('section', { class: 'dw-native dw-settings' }, [h('h3', '同步操作'),
          h('p', { class: 'dw-subtle' }, '需要手动处理历史记录时使用；勾选后保存执行一次。'),
          renderNative(formFields(rows.value[7], ['onlyonce', 'rescan']), model)]),
        h('section', { class: 'dw-native dw-settings' }, [h('h3', '仪表盘显示'),
          h('p', { class: 'dw-subtle' }, '控制仪表盘显示的月份、每月条目数量和封面，不影响实际同步。'),
          ...rows.value.slice(8, 10).map(row => renderNative(row, model))]),
        error.value ? h('div', { role: 'alert', class: 'dw-notice dw-error' }, error.value) : null,
      ]),
      h('footer', { class: 'dw-footer' }, [button('管理同步记录', () => emit('switch')), button(saving.value ? '正在保存…' : '保存设置', save, { primary: true, disabled: saving.value })]),
    ]);
  }
});

export const Page = defineComponent({
  props: ['api', 'pluginId', 'showSwitch'], emits: ['close', 'switch', 'layout'],
  setup(props, { emit }) {
    const state = useManagement(props), tab = ref('archive'), search = ref(''), page = ref(1), selected = ref(null), mode = ref('sync'), subject = ref(''), interest = ref('collect'), error = ref(''), saving = ref(false);
    const statusNames = { do: '在看', collect: '看过' };
    const statusOptions = record => /^(TV|电视剧)/.test(record?.type || '') ? ['do', 'collect'] : ['collect'];
    const names = { archive: '已同步', pending: '待同步', unmatched: '无条目' };
    const filtered = computed(() => (state.data.value[tab.value] || []).filter(record => !search.value ||
      [record.title, record.source_title, record.server_name, record.subject_id].join(' ').toLowerCase().includes(search.value.trim().toLowerCase())));
    watch([tab, search], () => page.value = 1);
    watch(filtered, value => { page.value = Math.min(page.value, Math.max(1, Math.ceil(value.length / 15))); });
    onMounted(() => emit('layout', { maxWidth: '76rem' }));
    function edit(record) {
      selected.value = record; subject.value = record.target_id || record.subject_id || ''; mode.value = 'sync'; error.value = '';
      const current = record.target_status || record.status;
      interest.value = statusOptions(record).includes(current) ? current : 'collect';
    }
    async function submit() {
      if (saving.value) return;
      if (mode.value === 'sync' && !/^[0-9]{1,15}$/.test(subject.value.trim())) { error.value = '请填写豆瓣页面中的数字编号'; return; }
      saving.value = true;
      if (await state.mutate('match', { key: selected.value.key, action: mode.value, subject_id: subject.value.trim(), status: interest.value })) selected.value = null;
      else error.value = state.message.value;
      saving.value = false;
    }
    return () => h('div', { class: 'dw-root dw-page' }, [
      h('header', { class: 'dw-header' }, [h('div', [h('h2', '豆瓣观看记录'), h('p', '修正匹配、重试同步和管理无条目内容')]),
        h('div', { class: 'dw-row-actions' }, [props.showSwitch !== false ? button('设置', () => emit('switch')) : null, button('关闭', () => emit('close'), { icon: 'close' })])]),
      h('div', { class: 'dw-page-body' }, [
        h('nav', { class: 'dw-tabs', 'aria-label': '同步记录分类' }, Object.entries(names).map(([key, text]) => h('button', {
          class: ['dw-tab', tab.value === key && 'dw-selected'], onClick: () => tab.value = key, type: 'button', 'aria-current': tab.value === key ? 'page' : undefined },
          [text, h('span', (state.data.value[key] || []).length)]))),
        h('div', { class: 'dw-record-toolbar' }, [field('搜索记录', search.value, value => search.value = value, { placeholder: '搜索名称、服务器或豆瓣编号' }), button('刷新记录', state.load, { icon: 'refresh' })]),
        notification(state),
        tab.value === 'unmatched' ? h('p', { class: 'dw-subtle' }, '此列表中的内容已停止自动匹配；以后有豆瓣条目时，可指定编号同步，或移回待同步。') : null,
        h('div', { class: 'dw-record-list' }, filtered.value.length ? filtered.value.slice((page.value - 1) * 15, page.value * 15).map(record => h('article', { class: 'dw-record', key: record.key }, [
          h('div', { class: 'dw-record-poster' }, record.image ? h('img', { src: record.image, alt: '', referrerpolicy: 'no-referrer', loading: 'lazy', onError: event => { event.target.style.display = 'none'; } }) : h('span', (record.title || '').slice(0, 2))),
          h('div', { class: 'dw-record-copy' }, [h('strong', record.source_title || record.title), h('div', { class: 'dw-record-meta' }, [
            h('span', record.type === 'TV' ? '电视剧' : record.type === 'MOV' ? '电影' : record.type), record.season ? h('span', `第${record.season}季`) : null,
            h('span', record.server_name || '媒体服务器'), statusNames[record.status] ? h('span', { class: record.status === 'collect' ? 'dw-text-good' : '' }, statusNames[record.status]) : null,
            record.manual ? h('span', '手动匹配') : null,
          ]), record.subject_id ? h('a', { href: `https://movie.douban.com/subject/${record.subject_id}/`, target: '_blank', rel: 'noopener noreferrer', class: 'dw-subject-link' }, `豆瓣 ${record.subject_id} · ${record.subject_name || record.title}`) : h('span', { class: 'dw-subtle' }, tab.value === 'unmatched' ? '已设为无条目' : '尚未匹配豆瓣条目'),
          record.operation ? h('small', { class: ['dw-operation', !record.processing && 'dw-text-error'] }, [record.processing ? h('i', { class: 'dw-spinner' }) : null, record.operation]) : null]),
          h('div', { class: 'dw-record-actions' }, [
            record.operation && !record.processing ? button(record.needs_cleanup ? '重试修正' : '重试', () => state.mutate('match', { action: 'retry', key: record.key })) : null,
            button(tab.value === 'unmatched' ? '指定编号' : '修正 / 重新同步', () => edit(record), { disabled: record.processing }),
            tab.value === 'unmatched' ? button('移回待同步', () => state.mutate('match', { action: 'restore', key: record.key }), { disabled: record.processing }) : null,
          ]),
        ])) : h('div', { class: 'dw-empty' }, search.value ? '没有匹配的记录' : `暂无${names[tab.value]}记录`)),
        h('div', { class: 'dw-pagination' }, [h('span', `共 ${filtered.value.length} 条`), button('上一页', () => page.value--, { disabled: page.value <= 1 }),
          h('span', `${page.value} / ${Math.max(1, Math.ceil(filtered.value.length / 15))}`), button('下一页', () => page.value++, { disabled: page.value * 15 >= filtered.value.length })]),
      ]),
      selected.value ? h('div', { class: 'dw-overlay', onClick: event => { if (event.target === event.currentTarget && !saving.value) selected.value = null; } }, [
        h('section', { role: 'dialog', 'aria-modal': 'true', 'aria-label': '修正豆瓣匹配', class: 'dw-match-dialog' }, [
          h('div', { class: 'dw-section-head' }, [h('h3', '修正豆瓣匹配'), button('关闭匹配窗口', () => selected.value = null, { icon: 'close', disabled: saving.value })]),
          h('strong', { class: 'dw-dialog-title' }, selected.value.source_title || selected.value.title),
          h('div', { class: 'dw-choice' }, [button('指定豆瓣编号', () => mode.value = 'sync', { primary: mode.value === 'sync' }), button('无条目', () => mode.value = 'ignore', { primary: mode.value === 'ignore' })]),
          mode.value === 'sync' ? h('div', [field('豆瓣编号', subject.value, value => subject.value = value, { placeholder: '例如 1292052', enter: submit }),
            h('span', { class: 'dw-subtle' }, '同步到豆瓣的状态'),
            h('div', { class: 'dw-choice', role: 'group', 'aria-label': '同步状态' }, statusOptions(selected.value).map(value => button(statusNames[value], () => interest.value = value, { primary: interest.value === value }))),
            /^\d+$/.test(subject.value) ? h('a', { class: 'dw-preview-link', href: `https://movie.douban.com/subject/${subject.value}/`, target: '_blank', rel: 'noopener noreferrer' }, '查看对应豆瓣条目 ↗') : null,
            h('p', { class: 'dw-subtle' }, selected.value.subject_id && selected.value.subject_id !== subject.value ? `会将新条目标记为“${statusNames[interest.value]}”，并撤销原编号 ${selected.value.subject_id} 的错误标记；未完成会保留待处理提示。` : `将重新向豆瓣提交“${statusNames[interest.value]}”状态。`)]) : h('p', { class: 'dw-subtle' }, '停止自动匹配并移入“无条目”。若原来已同步，也会撤销旧条目的错误标记。以后可以随时恢复。'),
          error.value ? h('p', { role: 'alert', class: 'dw-text-error' }, error.value) : null,
          h('div', { class: 'dw-dialog-actions' }, [button('取消', () => selected.value = null, { disabled: saving.value }), button(saving.value ? '正在提交…' : mode.value === 'ignore' ? '设为无条目' : '重新同步', submit, { primary: true, disabled: saving.value })]),
        ])]) : null,
    ]);
  }
});

export const Dashboard = defineComponent({
  props: ['api', 'pluginId', 'config', 'allowRefresh'],
  setup(props) {
    const config = ref(props.config || {});
    let timer, stopped = false;
    watch(() => props.config, value => config.value = value, { deep: true });
    async function refresh() {
      if (stopped || props.allowRefresh === false) return;
      try { config.value = apiValue(await props.api.get(`plugin/dashboard/${encodeURIComponent(props.pluginId || 'DoubanWatchSync')}/archive`)); } catch { /* Keep the last verified dashboard on transient failures. */ }
    }
    onMounted(() => { timer = setInterval(refresh, 60000); window.addEventListener('douban-watch-updated', refresh); });
    onUnmounted(() => { stopped = true; clearInterval(timer); window.removeEventListener('douban-watch-updated', refresh); });
    return () => h('div', { class: 'dw-root dw-dashboard' }, [h('div', { class: 'dw-dashboard-title' }, [h('strong', config.value.attrs?.title || '豆瓣观看同步'), h('span', config.value.attrs?.subtitle || '')]),
      ...(config.value.elements || []).map(node => renderNative(node, {}))]);
  }
});
