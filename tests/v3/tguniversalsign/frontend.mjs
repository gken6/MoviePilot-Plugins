import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const mounted=[],unmounted=[];
const Vue={h:(type,props,children)=>Array.isArray(props)||typeof props==='string'||typeof props==='number'?{type,props:{},children:props}:{type,props:props||{},children},ref:value=>({value}),reactive:value=>value,computed:fn=>({get value(){return fn();}}),onMounted:fn=>mounted.push(fn),onUnmounted:fn=>unmounted.push(fn),defineComponent:c=>c};
globalThis.__federation_shared__={default:{vue:{'3':{get:async()=>()=>Vue}}}};
globalThis.window={dispatchEvent:()=>{}};
const root=path.resolve(path.dirname(new URL(import.meta.url).pathname),'../../../plugins.v3/tguniversalsign');
const {Config,Page}=await import(pathToFileURL(path.join(root,'frontend/dist/assets/sign.js')));
function flatten(n){return !n||typeof n!=='object'?[]:[n,...(Array.isArray(n.children)?n.children:[n.children]).flatMap(flatten)];}
const find=(render,label)=>flatten(render()).find(n=>n.props['aria-label']===label);
async function mount(){for(const fn of mounted.splice(0))await fn();await new Promise(r=>setImmediate(r));}
const defaultConfig={enabled:true,api_id:'12345',api_hash:'simulated-hash',phone:'+000',targets:'@a\n@skip\n@c',commands:'/first\n\n/third',cron:'0 9 * * *',proxy_type:'none',reply_wait_seconds:'8',command_interval_seconds:'2',notify:true,notify_on_failure:true,phone_code_hash:'stale'};
const recordFixture=[{time:'2026-10-30 09:00:12',created_at:'2026-10-30T09:00:12',target:'@movie_checkin_bot',success:true,trigger:'定时',duration:4.2,summary:'签到成功，获得 20 积分，连续签到 12 天。',detail:'签到成功\n获得积分：20\n连续签到：12 天'}, {time:'2026-10-30 09:00:08',target:'@daily_bonus_bot',success:false,trigger:'定时',duration:8,summary:'等待机器人回复超时，可稍后重试。',detail:'等待回复超时。未收到新的机器人消息。'}, {time:'2026-10-29 09:00:10',target:'@movie_checkin_bot',success:true,trigger:'手动',duration:3.1,summary:'今日已签到，无需重复操作。',detail:'今日已签到。'}];
function fixture(id='TgUniversalSign',extra={}) {
 const calls=[],emits=[];let saved={...defaultConfig,...extra};
 const overview={records:recordFixture,stats:{total:36,success:34,failed:2},total:3,page:1,pages:1,session_saved:true,enabled:true,cron:saved.cron};
 const props={pluginId:id,initialConfig:{...saved},api:{
  get:async p=>{calls.push(['get',p]);return p.includes('plugin/form/')?{success:true,data:{model:{...saved,phone_code_hash:'fresh'},conf:[]}}:{success:true,data:overview};},
  put:async(p,b)=>{calls.push(['put',p,b]);saved={...b};return{success:true};},
  post:async(p,b)=>{calls.push(['post',p,b]);return {success:true,data:{code:0,msg:p.endsWith('login')?'需要二步验证密码':'完成'}};},
 }};
 return {props,calls,emits,overview,render:Config.setup(props,{emit:(...args)=>emits.push(args)})};
}
const tg=fixture();await mount();
assert.equal(tg.calls.filter(c=>c[0]==='post').length,0);
assert.ok(!tg.calls.some(c=>c[1].endsWith('/status')));
assert.equal(find(tg.render,'目标 2').props.value,'@skip');assert.equal(find(tg.render,'动作 2').props.value,'');
await find(tg.render,'保存设置').props.onClick();
let payload=tg.calls.find(c=>c[0]==='put')[2];
assert.equal(payload.targets,defaultConfig.targets);assert.equal(payload.commands,defaultConfig.commands);
assert.equal(payload.phone_code_hash,'fresh');assert.equal(payload.onlyonce,false);assert.equal(payload.send_code_once,false);
assert.equal(payload.api_hash,'simulated-hash');assert.equal(payload.cron,'0 9 * * *');
const editing=fixture();await mount();
find(editing.render,'动作 1').props.onInput({target:{value:'/start\n点击:签到'}});
await find(editing.render,'立即执行').props.onClick();
assert.equal(editing.calls.find(c=>c[0]==='put')[2].commands.split('\n')[0],'/start;点击:签到');
assert.equal(editing.calls.find(c=>c[0]==='post')[1],'plugin/TgUniversalSign/run');
const invalid=fixture();await mount();
find(invalid.render,'目标 1').props.onInput({target:{value:''}});
await find(invalid.render,'保存设置').props.onClick();
assert.ok(!invalid.calls.some(c=>c[0]==='put'));assert.ok(flatten(invalid.render()).some(n=>n.props.role==='alert'));
const login=fixture();await mount();
flatten(login.render()).find(n=>n.type==='button'&&n.children==='账号连接').props.onClick();
assert.equal(find(login.render,'API Hash').props.type,'password');
find(login.render,'Telegram 验证码').props.onInput({target:{value:'simulated-code'}});
await find(login.render,'验证登录').props.onClick();
assert.deepEqual(login.calls.find(c=>c[0]==='post'),['post','plugin/TgUniversalSign/login',{code:'simulated-code'}]);
assert.equal(find(login.render,'Telegram 验证码').props.value,'');
assert.ok(flatten(login.render()).some(n=>n.children==='需要二步验证密码，请在下方填写并提交。'));
const dy=fixture('DianyingTelegramSign',{target:'@dianyingbalala_bot',daily_luck:true,daily_normal:false,daily_spin_once:true,daily_spin_count:'5'});await mount();
assert.equal(find(dy.render,'每日转盘').props.value,'5');
await find(dy.render,'保存设置').props.onClick();payload=dy.calls.find(c=>c[0]==='put')[2];
assert.equal(payload.daily_spin_count,'5');assert.equal(payload.daily_spin_once,false);assert.equal(payload.target,'@dianyingbalala_bot');
flatten(dy.render()).find(n=>n.type==='button'&&n.children==='账号连接').props.onClick();
find(dy.render,'二步验证密码').props.onInput({target:{value:'simulated-password'}});
await find(dy.render,'提交二步验证').props.onClick();
assert.deepEqual(dy.calls.find(c=>c[0]==='post'),['post','plugin/DianyingTelegramSign/ui/login-2fa',{password:'simulated-password'}]);
assert.ok(dy.calls.every(c=>!c[1].includes('password=')&&!c[1].includes('code=')));
const historyCalls=[];const page=Page.setup({pluginId:'TgUniversalSign',api:{get:async p=>{historyCalls.push(p);return{success:true,data:tg.overview};}}},{emit:()=>{}});await mount();
assert.equal(flatten(page()).filter(n=>n.type==='details'&&n.props.class==='sg-record').length,3);
assert.ok(flatten(page()).some(n=>n.type==='pre'&&n.children.includes('获得积分')));
assert.ok(!flatten(page()).some(n=>n.props.innerHTML||n.props.onClick?.toString().includes('send_code')));
flatten(page()).find(n=>n.type==='button'&&n.children==='失败').props.onClick();await new Promise(r=>setImmediate(r));
assert.ok(historyCalls.at(-1).includes('state=failed'));
if(process.env.SIGN_UI_PREVIEW){
 const dir=path.resolve(process.env.SIGN_UI_PREVIEW);fs.mkdirSync(dir,{recursive:true});
 const css=fs.readFileSync(path.join(root,'frontend/dist/assets/sign.css'),'utf8');
 const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
 function html(n){if(n==null||n===false)return'';if(typeof n!=='object')return esc(n);if(Array.isArray(n))return n.map(html).join('');const p=n.props||{};let attrs='';for(let[k,v]of Object.entries(p)){if(k.startsWith('on')||['key','value','rows','type'].includes(k)&&n.type!=='input'&&n.type!=='textarea'||v==null||v===false)continue;if(k==='class')v=[v].flat(Infinity).filter(Boolean).join(' ');if(k==='value'&&n.type==='textarea')continue;if(typeof v==='object'||typeof v==='function')continue;attrs+=' '+k+(v===true?'':'="'+esc(v)+'"');}return'<'+n.type+attrs+'>'+(n.type==='textarea'?esc(p.value):html(n.children))+(n.type==='input'?'':'</'+n.type+'>');}
 const clean=fixture();await mount();const special=fixture('DianyingTelegramSign',{target:'@dianyingbalala_bot',daily_luck:true,daily_normal:true,daily_spin_once:false,daily_spin_count:'5'});await mount();
 const scenes=[['tg-tasks',clean.render()],['dianying-tasks',special.render()],['history',page()]];
 flatten(clean.render()).find(n=>n.type==='button'&&n.children==='账号连接').props.onClick();scenes.push(['tg-account',clean.render()]);
 for(const[name,tree]of scenes)fs.writeFileSync(path.join(dir,name+'.html'),'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>签到插件界面预览</title><style>body{margin:0;padding:32px 16px;background:#f3f5f9;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}.preview{max-width:1060px;margin:auto;border-radius:16px;overflow:hidden;box-shadow:0 10px 45px #18253a10}'+css+'</style><div class="preview">'+html(tree)+'</div></html>');
}
for(const fn of unmounted)fn();
console.log('签到前端：配置保留、同行动作、直接登录、执行保存顺序、筛选与历史展开通过');
