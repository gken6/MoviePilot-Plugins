import { share } from './remoteEntry.js';
const { h, ref, reactive, computed, onMounted, onUnmounted, defineComponent } = await share('vue');
const identity = props => {
  const special = props.pluginId === 'DianyingTelegramSign';
  return { id: props.pluginId || 'TgUniversalSign', special, title: special ? '癫影签到' : 'TG 万能签到', run: special ? 'run_daily' : 'run' };
};
const paths = { plus: 'M12 5v14M5 12h14', close: 'm6 6 12 12M6 18 18 6', refresh: 'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2', trash: 'M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 10v7m4-7v7', arrow: 'm9 5 7 7-7 7', play: 'm8 5 11 7-11 7Z', check: 'm5 12 4 4L19 6' };
const icon = name => h('svg', { viewBox: '0 0 24 24', width: 18, height: 18, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, [h('path', { d: paths[name] })]);
const button = (text, click, options = {}) => h('button', { type: 'button', class: ['sg-button', options.primary && 'sg-primary', options.iconOnly && 'sg-icon-button'], disabled: options.disabled, 'aria-label': text, title: text, onClick: click }, [options.icon ? icon(options.icon) : null, options.iconOnly ? null : text]);
function field(label, value, input, options = {}) {
  return h('label', { class: ['sg-field', options.wide && 'sg-wide'] }, [h('span', label), h(options.textarea ? 'textarea' : 'input', { value: value ?? '', type: options.type || 'text', rows: options.rows || 2,
    autocomplete: options.autocomplete || 'off', placeholder: options.placeholder || '', 'aria-label': label, onInput: event => input(event.target.value) }), options.hint ? h('small', options.hint) : null]);
}
function select(label, value, input, items) {
  return h('label', { class: 'sg-field' }, [h('span', label), h('select', { value, 'aria-label': label, onChange: event => input(event.target.value) }, items.map(([key, text]) => h('option', { value: key }, text)))]);
}
function toggle(label, checked, change, hint) {
  return h('label', { class: 'sg-toggle-row' }, [h('span', { class: 'sg-toggle-copy' }, [h('strong', label), hint ? h('small', hint) : null]),
    h('span', { class: 'sg-switch' }, [h('input', { type: 'checkbox', role: 'switch', 'aria-label': label, checked: !!checked, onChange: event => change(event.target.checked) }), h('i')])]);
}
const notice = state => state.message.value ? h('div', { role: state.failed.value ? 'alert' : 'status', 'aria-live': 'polite', class: ['sg-notice', state.failed.value && 'sg-error'] }, state.message.value) : null;
function unwrap(result) {
  if (result?.success === false) throw Error(result.message || '操作未完成');
  const value = result && typeof result.success === 'boolean' && Object.hasOwn(result, 'data') ? result.data : result;
  if (value?.success === false || value?.code && value.code !== 0) throw Error(value.message || value.msg || '操作未完成');
  return value;
}
async function api(props, method, path, body) { return unwrap(await props.api[method](`plugin/${identity(props).id}/${path}`, ...(body === undefined ? [] : [body]))); }
function overview(props) {
  const data = ref({ records: [], stats: { total: 0, success: 0, failed: 0 }, page: 1, pages: 1, total: 0 });
  const message = ref(''), failed = ref(false), loading = ref(false);
  let sequence = 0, stopped = false;
  async function load(query = '') {
    const current = ++sequence; loading.value = true;
    try { const value = await api(props, 'get', 'ui/overview' + query); if (!stopped && current === sequence) { data.value = value; message.value = ''; failed.value = false; } }
    catch (error) { if (current === sequence) { message.value = error.message || '读取失败'; failed.value = true; } }
    finally { if (current === sequence) loading.value = false; }
  }
  onUnmounted(() => { stopped = true; sequence++; });
  return { data, message, failed, loading, load };
}
function header(title, subtitle, close, extras = []) {
  return h('header', { class: 'sg-header' }, [h('div', { class: 'sg-brand' }, [h('div', { class: 'sg-logo' }, icon('check')), h('div', [h('h2', title), h('p', subtitle)])]), h('div', { class: 'sg-actions' }, [...extras, button('关闭', close, { icon: 'close', iconOnly: true })])]);
}
function metrics(data) {
  return h('div', { class: 'sg-metrics' }, [['近 7 天记录', data.stats?.total || 0, ''], ['执行成功', data.stats?.success || 0, 'sg-good'], ['执行失败', data.stats?.failed || 0, 'sg-bad']].map(([label, value, cls]) => h('div', { class: 'sg-metric' }, [h('span', label), h('strong', { class: cls }, value)])));
}
export const Config = defineComponent({
  props: ['initialConfig', 'api', 'pluginId'], emits: ['save', 'close', 'switch', 'layout'],
  setup(props, { emit }) {
    const info = identity(props), model = reactive({ ...(props.initialConfig || {}) }), state = overview(props);
    const tab = ref('tasks'), ready = ref(false), busy = ref(''), message = ref(''), failed = ref(false), loginMessage = ref('');
    const code = ref(''), password = ref(''), pairs = ref([]), scheduleMode = ref('custom'), dailyTime = ref('09:00'), spin = ref('none');
    let serial = 0;
    function unpack() {
      const targets = String(model.targets || '').replace(/\r\n?/g, '\n').split('\n'), commands = String(model.commands || '').replace(/\r\n?/g, '\n').split('\n');
      pairs.value = Array.from({ length: Math.max(targets.length, commands.length, 1) }, (_, i) => ({ key: ++serial, target: targets[i] || '', command: commands[i] || '' }));
      const daily = String(model.cron || '').match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$/);
      if (daily && +daily[1] < 60 && +daily[2] < 24) { scheduleMode.value = 'daily'; dailyTime.value = daily[2].padStart(2, '0') + ':' + daily[1].padStart(2, '0'); }
      spin.value = ['5', '10', '20'].includes(String(model.daily_spin_count)) ? String(model.daily_spin_count) : model.daily_spin_once ? 'once' : 'none';
    }
    onMounted(async () => {
      emit('layout', { maxWidth: '70rem' });
      try {
        const form = unwrap(await props.api.get(`plugin/form/${info.id}`));
        Object.assign(model, { ...form.model, ...(props.initialConfig || {}) }); unpack(); ready.value = true;
      } catch (error) { message.value = error.message || '配置加载失败'; failed.value = true; }
      state.load();
    });
    async function persist() {
      if (!ready.value) throw Error('配置尚未加载，请关闭后重新打开');
      const form = unwrap(await props.api.get(`plugin/form/${info.id}`));
      const payload = { ...model, phone_code_hash: form.model?.phone_code_hash || '', last_result: form.model?.last_result || '', onlyonce: false, send_code_once: false, login_code: '', login_password: '' };
      if (scheduleMode.value === 'daily') {
        const time = dailyTime.value.match(/^(\d{2}):(\d{2})$/);
        if (!time) throw Error('请选择每天执行的时间');
        payload.cron = `${Number(time[2])} ${Number(time[1])} * * *`;
      }
      if (info.special) { payload.daily_spin_once = spin.value === 'once'; payload.daily_spin_count = ['5', '10', '20'].includes(spin.value) ? spin.value : ''; }
      else {
        pairs.value.forEach((row, index) => { if (!row.target.trim() && row.command.trim()) throw Error(`第 ${index + 1} 项请填写目标，或清空该项动作`); });
        payload.targets = pairs.value.map(row => row.target).join('\n'); payload.commands = pairs.value.map(row => row.command).join('\n');
      }
      unwrap(await props.api.put(`plugin/${info.id}`, payload));
      model.cron = payload.cron; model.phone_code_hash = payload.phone_code_hash;
      window.dispatchEvent(new Event('tg-sign-updated'));
    }
    async function action(kind, route, body, close = false) {
      if (busy.value) return;
      busy.value = kind; message.value = ''; failed.value = false;
      try {
        await persist();
        if (route) {
          const result = await api(props, 'post', route, body || {});
          const rawResult = result?.msg || result?.message || '操作完成';
          const raw = String(rawResult).includes('需要二步验证') ? '需要二步验证密码，请在下方填写并提交。' : kind === 'send' ? '验证码已发送，请查看 Telegram 并在下方填写。' : rawResult;
          if (['send', 'login', '2fa', 'status'].includes(kind)) loginMessage.value = raw;
          message.value = kind === 'run' ? '执行完成，可在运行记录中查看结果' : raw;
          code.value = ''; password.value = '';
        } else message.value = '设置已保存';
        await state.load(); if (close) emit('close');
      } catch (error) { message.value = error.message || '操作未完成'; failed.value = true; }
      finally { busy.value = ''; }
    }
    async function checkStatus() {
      if (busy.value) return;
      busy.value = 'status'; message.value = ''; failed.value = false;
      try { await persist(); const result = await api(props, 'get', 'status'); loginMessage.value = result.msg || '状态已更新'; }
      catch (error) { message.value = error.message; failed.value = true; }
      finally { busy.value = ''; }
    }
    const disabled = () => !!busy.value || !ready.value;
    const account = () => h('section', { class: 'sg-panel' }, [
      h('div', { class: 'sg-section-title' }, [h('div', [h('h3', 'Telegram 账号'), h('p', '已有登录会话会继续使用，无需重复登录。')]), h('span', { class: 'sg-badge' }, state.data.value.session_saved ? '已保存登录会话' : '尚无登录会话')]),
      h('div', { class: 'sg-grid sg-three' }, [field('API ID', model.api_id, v => model.api_id = v), field('API Hash', model.api_hash, v => model.api_hash = v, { type: 'password' }), field('手机号', model.phone, v => model.phone = v, { placeholder: '+86…' })]),
      h('div', { class: 'sg-login' }, [h('div', { class: 'sg-section-title' }, [h('div', [h('h4', '登录验证'), h('p', '填写账号后直接发送验证码，连接设置会自动保存。')]), button(busy.value === 'status' ? '正在检查…' : '检查登录状态', checkStatus, { disabled: disabled(), icon: 'refresh' })]),
        h('div', { class: 'sg-login-line' }, [field('Telegram 验证码', code.value, v => code.value = v, { autocomplete: 'one-time-code' }), button(busy.value === 'send' ? '正在发送…' : '发送验证码', () => action('send', 'send_code'), { disabled: disabled() }), button('验证登录', () => action('login', info.special ? 'ui/login' : 'login', { code: code.value }), { disabled: disabled() || !code.value.trim(), primary: true })]),
        h('details', { class: 'sg-disclosure' }, [h('summary', '账号启用了二步验证'), h('div', { class: 'sg-login-line' }, [field('二步验证密码', password.value, v => password.value = v, { type: 'password', autocomplete: 'new-password' }), button('提交二步验证', () => action('2fa', info.special ? 'ui/login-2fa' : 'login_2fa', { password: password.value }), { disabled: disabled() || !password.value })])]),
        loginMessage.value ? h('div', { class: 'sg-login-result', role: 'status' }, loginMessage.value) : null,
      ]),
    ]);
    const schedule = () => h('section', { class: 'sg-panel' }, [h('div', { class: 'sg-section-title' }, [h('div', [h('h3', '执行计划'), h('p', '按 MoviePilot 服务器时间执行。')]), toggle('启用定时', model.enabled, v => model.enabled = v)]),
      h('div', { class: 'sg-grid sg-two' }, [select('执行周期', scheduleMode.value, v => scheduleMode.value = v, [['daily', '每天'], ['custom', '自定义 Cron']]), scheduleMode.value === 'daily' ? field('执行时间', dailyTime.value, v => dailyTime.value = v, { type: 'time' }) : field('Cron 表达式', model.cron, v => model.cron = v, { placeholder: '0 9 * * *' })])]);
    const tasks = () => h('section', { class: 'sg-panel' }, [h('div', { class: 'sg-section-title' }, [h('div', [h('h3', info.special ? '每日任务' : '签到任务'), h('p', info.special ? '选择每天执行的项目，转盘次数与单次转盘互斥。' : '目标与动作同行编辑，空动作保留为跳过项。')]), !info.special ? button('添加任务', () => pairs.value.push({ key: ++serial, target: '', command: '' }), { icon: 'plus', disabled: disabled() }) : null]),
      info.special ? h('div', [field('签到机器人', model.target, v => model.target = v, { placeholder: '@dianyingbalala_bot' }), h('div', { class: 'sg-task-options' }, [toggle('运气签到', model.daily_luck, v => model.daily_luck = v, '/lqd'), toggle('普通签到', model.daily_normal, v => model.daily_normal = v, '/qd'), select('每日转盘', spin.value, v => spin.value = v, [['none', '不执行'], ['once', '转盘 1 次'], ['5', '转盘 5 次'], ['10', '转盘 10 次'], ['20', '转盘 20 次']])])]) : h('div', [
        h('div', { class: 'sg-task-list' }, pairs.value.map((row, index) => h('div', { class: 'sg-task-row', key: row.key }, [h('span', { class: 'sg-row-number' }, String(index + 1).padStart(2, '0')), field(`目标 ${index + 1}`, row.target, v => row.target = v, { placeholder: '@机器人 / 频道 ID / t.me 链接' }), field(`动作 ${index + 1}`, row.command, v => row.command = v.replace(/\r?\n/g, ';'), { textarea: true, placeholder: '/start;等待:2;点击:签到' }), button(`删除任务 ${index + 1}`, () => pairs.value.splice(index, 1), { icon: 'trash', iconOnly: true, disabled: disabled() })]))),
        h('details', { class: 'sg-disclosure' }, [h('summary', '动作怎么写'), h('p', '同一任务内用半角分号 ; 分隔动作，按顺序执行。支持普通文本、发送:文字、点击:按钮文字、等待:秒数。'), h('code', '/start;等待:2;点击:签到')]),
      ])]);
    return () => h('div', { class: 'sg-root' }, [
      header(info.title, '账号、任务与运行记录', () => emit('close'), [button(busy.value === 'run' ? '正在执行…' : '立即执行', () => action('run', info.run), { icon: 'play', primary: true, disabled: disabled() })]),
      h('nav', { class: 'sg-tabs', 'aria-label': '设置分区' }, [['tasks', '任务设置'], ['account', '账号连接'], ['advanced', '通知与高级']].map(([key, label]) => h('button', { type: 'button', class: ['sg-tab', tab.value === key && 'sg-selected'], 'aria-current': tab.value === key ? 'page' : undefined, onClick: () => tab.value = key }, label))),
      h('main', { class: 'sg-body', 'aria-busy': !!busy.value }, [
        notice({ message, failed }), !ready.value ? h('div', { class: 'sg-empty' }, failed.value ? '配置未能加载' : '正在加载配置…') : tab.value === 'tasks' ? h('div', [schedule(), tasks(), h('p', { class: 'sg-footnote' }, '立即执行会先保存当前设置，再运行已配置任务。')]) : tab.value === 'account' ? account() : h('div', [
          h('section', { class: 'sg-panel' }, [h('h3', '消息通知'), toggle('成功后通知', model.notify, v => model.notify = v, '使用 MoviePilot 已配置的通知渠道'), toggle('失败后通知', model.notify_on_failure, v => model.notify_on_failure = v)]),
          h('section', { class: 'sg-panel' }, [h('h3', '网络与执行'), h('div', { class: 'sg-grid sg-three' }, [select('代理类型', model.proxy_type || 'none', v => model.proxy_type = v, [['none', '不使用代理'], ['socks5', 'SOCKS5'], ['http', 'HTTP']]), ...(model.proxy_type && model.proxy_type !== 'none' ? [field('代理地址', model.proxy_host, v => model.proxy_host = v), field('代理端口', model.proxy_port, v => model.proxy_port = v)] : [])]),
            h('div', { class: 'sg-grid sg-two sg-spaced' }, [field('等待回复（秒）', model.reply_wait_seconds, v => model.reply_wait_seconds = v, { type: 'number' }), !info.special ? field('动作间隔（秒）', model.command_interval_seconds, v => model.command_interval_seconds = v, { type: 'number' }) : null])]),
        ]),
      ]),
      h('footer', { class: 'sg-footer' }, [button('运行记录', () => emit('switch')), h('div', { class: 'sg-actions' }, [button('取消', () => emit('close'), { disabled: !!busy.value }), button(busy.value === 'save' ? '正在保存…' : '保存设置', () => action('save', null, null, true), { primary: true, disabled: disabled() })])]),
    ]);
  }
});
export const Page = defineComponent({
  props: ['api', 'pluginId'], emits: ['switch', 'close', 'layout'],
  setup(props, { emit }) {
    const info = identity(props), state = overview(props), filter = ref('all'), search = ref(''); let timer, searchTimer;
    function load(page = 1) { return state.load(`?page=${page}&state=${encodeURIComponent(filter.value)}&search=${encodeURIComponent(search.value)}`); }
    onMounted(() => { emit('layout', { maxWidth: '70rem' }); load(); timer = setInterval(() => load(state.data.value.page || 1), 15000); });
    onUnmounted(() => { clearInterval(timer); clearTimeout(searchTimer); });
    const records = computed(() => state.data.value.records || []);
    return () => h('div', { class: 'sg-root' }, [header(info.title, '运行记录 · 最近 7 天', () => emit('close'), [button('刷新', () => load(state.data.value.page || 1), { icon: 'refresh', disabled: state.loading.value })]),
      h('main', { class: 'sg-body sg-history-body' }, [metrics(state.data.value), notice(state),
        h('div', { class: 'sg-history-toolbar' }, [h('div', { class: 'sg-filter', 'aria-label': '记录筛选' }, [['all', '全部'], ['success', '成功'], ['failed', '失败']].map(([key, label]) => h('button', { type: 'button', class: filter.value === key ? 'sg-active' : '', 'aria-pressed': filter.value === key, onClick: () => { filter.value = key; load(); } }, label))),
          field('搜索记录', search.value, value => { search.value = value; clearTimeout(searchTimer); searchTimer = setTimeout(() => load(), 300); }, { placeholder: '搜索目标、任务或回复' })]),
        h('div', { class: 'sg-records', 'aria-busy': state.loading.value }, records.value.length ? records.value.map((item, index) => h('details', { class: 'sg-record', key: `${item.created_at}-${item.target}-${index}` }, [
          h('summary', [h('span', { class: ['sg-result-dot', item.success ? 'sg-success' : 'sg-failure'] }, icon(item.success ? 'check' : 'close')), h('div', { class: 'sg-record-copy' }, [h('strong', item.target || item.trigger || '签到任务'), h('p', item.summary || '暂无摘要')]), h('div', { class: 'sg-record-meta' }, [h('span', item.time || item.created_at || '—'), h('small', `${item.success ? '成功' : '失败'} · ${Number(item.duration || 0).toFixed(1)} 秒`)]), h('span', { class: 'sg-record-arrow' }, icon('arrow'))]),
          h('div', { class: 'sg-record-detail' }, [h('span', item.trigger || '运行详情'), h('pre', item.detail || item.summary || '暂无详细回复')]),
        ])) : h('div', { class: 'sg-empty' }, state.loading.value ? '正在读取记录…' : search.value || filter.value !== 'all' ? '没有符合条件的记录' : '暂无运行记录，执行任务后会显示在这里')),
        h('div', { class: 'sg-pagination' }, [h('span', `共 ${state.data.value.total || 0} 条`), button('上一页', () => load(state.data.value.page - 1), { disabled: state.loading.value || state.data.value.page <= 1 }), h('span', `${state.data.value.page || 1} / ${state.data.value.pages || 1}`), button('下一页', () => load(state.data.value.page + 1), { disabled: state.loading.value || state.data.value.page >= state.data.value.pages })]),
      ]), h('footer', { class: 'sg-footer' }, [h('span', { class: 'sg-footnote' }, state.data.value.enabled ? `定时已开启 · ${state.data.value.cron}` : '定时未开启'), button('编辑设置', () => emit('switch'), { primary: true })]),
    ]);
  }
});
