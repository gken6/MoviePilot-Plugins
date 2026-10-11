import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const mounted=[],unmounted=[],listeners={};
const Vue={h:(type,props,children)=>Array.isArray(props)||typeof props==='string'||typeof props==='number'?({type,props:{},children:props}):({type,props:props||{},children}),ref:value=>({value}),reactive:value=>value,
 computed:fn=>({get value(){return fn();}}),onMounted:fn=>mounted.push(fn),onUnmounted:fn=>unmounted.push(fn),watch:()=>{},resolveComponent:name=>({name}),defineComponent:component=>component};
globalThis.__federation_shared__={default:{vue:{'3':{get:async()=>()=>Vue}}}};
globalThis.window={addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener:()=>{},dispatchEvent:()=>{}};
const {Config,Dashboard,Page}=await import(pathToFileURL(new URL('../../../plugins.v3/doubanwatchsync/frontend/dist/assets/watch.js', import.meta.url).pathname));
const rows=Array.from({length:11},(_,i)=>({component:'VRow',content:[{component:'VSwitch',props:{model:'enabled',label:'row-'+i}}]}));
rows[7]={component:'VRow',content:['use_cookiecloud','onlyonce','rescan'].map(model=>({component:'VCol',content:[{component:'VSwitch',props:{model,label:model}}]}))};
rows.push({component:'VRow',content:[{component:'VSwitch',props:{model:'auto_login',label:'Cookie 过期自动重新登录'}}]},
 {component:'VRow',content:[{component:'VTextField',props:{model:'douban_username',label:'豆瓣账号'}},{component:'VTextField',props:{model:'douban_password',label:'豆瓣密码',type:'password'}}]});
const calls=[];
const props={pluginId:'DoubanWatchSync',initialConfig:{enabled:true},api:{get:async path=>{calls.push(path);return {success:true,data:{conf:[{component:'VForm',content:rows}],model:{enabled:false}}};}}};
const render=Config.setup(props,{emit:()=>{}});
await mounted.pop()();
const tree=render();
function flatten(node){if(!node||typeof node!=='object')return[];let children=node.children;if(children?.default)children=children.default();return [node,...(Array.isArray(children)?children:[children]).flatMap(flatten)];}
const fields=flatten(tree).filter(n=>n.type?.name==='VSwitch'&&n.props.label?.startsWith('row-'));
assert.equal(fields.length,7);assert.ok(fields.every(n=>n.props.modelValue===true));
assert.ok(fields.every(n=>!['row-4','row-5','row-10'].includes(n.props.label)));
for(const title of ['豆瓣登录','同步操作','仪表盘显示']) assert.ok(flatten(tree).some(n=>n.type==='h3'&&n.children===title));
for(const label of ['use_cookiecloud','onlyonce','rescan']) assert.equal(flatten(tree).filter(n=>n.props.label===label).length,1);
assert.equal(calls[0],'plugin/form/DoubanWatchSync');
assert.ok(!flatten(tree).some(n=>n.props.label==='豆瓣密码'));
assert.ok(!flatten(tree).some(n=>n.props.label==='使用 MP 智能助手尝试滑块验证'));
flatten(tree).find(n=>n.props.label==='Cookie 过期自动重新登录').props['onUpdate:modelValue'](true);
const password=flatten(render()).find(n=>n.props.label==='豆瓣密码');
assert.equal(password.props.type,'password');
password.props['onUpdate:modelValue']('simulated-password');
assert.equal(flatten(render()).find(n=>n.props.label==='豆瓣密码').props.modelValue,'simulated-password');
const dashboardProps={pluginId:'DoubanWatchSync',api:{get:async()=>({success:true,data:{attrs:{subtitle:'共 2 部'},elements:[{component:'VCardText',props:{text:'correct title'}}]}})},config:{elements:[]},allowRefresh:true};
const dashboard=Dashboard.setup(dashboardProps);
await mounted.pop()();await listeners['douban-watch-updated']();
assert.ok(flatten(dashboard()).some(n=>n.children?.default?.()==='correct title'));
for (const type of ['MOV', '电影', 'TV', '电视剧']) {
  const writes=[];
  const record={key:'record',title:'test',type,status:'do',subject_id:'200'};
  const management={servers:[],archive:[record],pending:[],unmatched:[]};
  const pageRender=Page.setup({pluginId:'DoubanWatchSync',api:{get:async()=>management,post:async(path,body)=>{writes.push(body);return {success:true,data:{success:true,message:'已提交'}};}}},{emit:()=>{}});
  for(const fn of mounted.splice(0))await fn();
  await flatten(pageRender()).find(n=>n.props['aria-label']==='修正 / 重新同步').props.onClick();
  const dialog=flatten(pageRender()).find(n=>n.props.role==='dialog');
  const choices=flatten(dialog).find(n=>n.props['aria-label']==='同步状态');
  const labels=flatten(choices).filter(n=>n.type==='button').map(n=>n.props['aria-label']);
  assert.deepEqual(labels,/^(TV|电视剧)/.test(type)?['在看','看过']:['看过']);
  flatten(choices).find(n=>n.props['aria-label']==='看过').props.onClick();
  await flatten(pageRender()).find(n=>n.props['aria-label']==='重新同步').props.onClick();
  assert.equal(writes[0].status,'collect');
}
const failedPage=Page.setup({api:{get:async()=>({servers:[],archive:[{key:'r',title:'test',type:'MOV',status:'collect',subject_id:'200'}],pending:[],unmatched:[]}),post:async()=>({success:true,data:{success:false,message:'明确失败'}})}},{emit:()=>{}});
for(const fn of mounted.splice(0))await fn();
flatten(failedPage()).find(n=>n.props['aria-label']==='修正 / 重新同步').props.onClick();
await flatten(failedPage()).find(n=>n.props['aria-label']==='重新同步').props.onClick();
assert.ok(flatten(failedPage()).some(n=>n.props.role==='alert'&&n.children==='明确失败'));
for(const fn of unmounted)fn();
console.log('前端配置、登录控制、仪表盘刷新、影片/剧集状态选项、所选状态提交及失败提示：通过');
