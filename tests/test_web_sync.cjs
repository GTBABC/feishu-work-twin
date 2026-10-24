const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const sync=require('../larkx/workhub/static/sync.js');

test('deltas update records, remove old records and follow server order',()=>{
  const original={tasks:[{id:1,title:'a'},{id:2,title:'b'}],personas:[],status:{own_rules:'rules'}};
  const packet={epoch:'e',revision:3,reset:false,changes:{tasks:{upsert:[{id:2,title:'new'},{id:3,title:'c'}],removed:[1],order:[3,2]},status:{active_calls:1}}};
  const {data,changed}=sync.apply(original,packet,{tasks:'id'});
  assert.deepEqual(data.tasks,[{id:3,title:'c'},{id:2,title:'new'}]);
  assert.equal(data.personas,original.personas);
  assert.equal(data.status.own_rules,'rules');
  assert.deepEqual([...changed],['tasks','status']);
  assert.equal(original.tasks[0].id,1);
});

test('idle responses retain collection identity and restart replaces stale state',()=>{
  const original={tasks:[{id:1}],status:{connection:'connected'}};
  const idle=sync.apply(original,{epoch:'e',revision:2,reset:false,changes:{}},{tasks:'id'});
  assert.equal(idle.data.tasks,original.tasks);
  assert.equal(idle.changed.size,0);
  const reset=sync.apply(original,{epoch:'new',revision:1,reset:true,changes:{tasks:{upsert:[],removed:[],order:[]},status:{connection:'starting'}}},{tasks:'id'});
  assert.deepEqual(reset.data.tasks,[]);
  assert.equal(reset.data.status.connection,'starting');
});

async function page(){
  const elements=new Map(),events={},timers=[];
  function element(selector){
    if(!elements.has(selector)){
      let html='';
      const value={writes:0,value:'',textContent:'',className:'',hidden:true,dataset:{},style:{},scrollTop:0,scrollHeight:200,clientHeight:100,
        listeners:{},classList:{add(){},remove(){}},addEventListener:(name,handler)=>{value.listeners[name]=handler;},setAttribute(){},focus(){}};
      Object.defineProperty(value,'innerHTML',{get:()=>html,set:next=>{html=next;value.writes++;}});
      elements.set(selector,value);
    }
    return elements.get(selector);
  }
  let reply={epoch:'root',revision:1,reset:true,changes:{
    tasks:{upsert:[],removed:[],order:[]},conversations:{upsert:[],removed:[],order:[]},personas:{upsert:[],removed:[],order:[]},actions:{upsert:[],removed:[],order:[]},
    status:{connection:'connected',active_calls:0}
  }};
  const requests=[],collections={tasks:[],conversations:[],personas:[],actions:[]};
  const context=vm.createContext({WorkhubPaging:require('../larkx/workhub/static/paging.js'),URLSearchParams,WorkhubModels:require('../larkx/workhub/static/models.js'),WorkhubSync:sync,WorkhubSettings:require('../larkx/workhub/static/settings.js'),Set,Map,Date,JSON,Promise,console,Node:{TEXT_NODE:3,ELEMENT_NODE:1},
    document:{hidden:false,body:{dataset:{}},querySelector:element,querySelectorAll:()=>[],addEventListener:(name,handler)=>{events[name]=handler;}},
    setTimeout:(handler)=>{timers.push(handler);return timers.length;},clearTimeout(){},
    fetch:async path=>{requests.push(path);return {ok:true,json:async()=>{
      if(path==='/api/control/history')return [];
      if(path.startsWith('/api/lists/')){const url=new URL(path,'http://test'),key=url.pathname.split('/').at(-1),size=Number(url.searchParams.get('size')||10);let items=collections[key];const query=url.searchParams.get('query');if(query)items=items.filter(r=>(r.name||r.title||'').includes(query));const total=items.length,pages=Math.max(1,Math.ceil(total/size)),page=Math.min(Number(url.searchParams.get('page')||1),pages);return {items:items.slice((page-1)*size,page*size),page,pages,size,total,revision:1};}
      return reply;
    }};}
  });
  vm.runInContext(fs.readFileSync(require.resolve('../larkx/workhub/static/app.js'),'utf8')+'\nglobalThis.pageApi={state,paintServerDraft,homeNotice,loadState,refreshChatMessages,poll,renderSettings,renderSettingsPanel,renderConversations,refreshList,fitList,replyPolicySelect,get loading(){return stateLoading;}};',context);
  await context.pageApi.loading;
  return {context,elements,element,events,timers,requests,api:context.pageApi,setReply:value=>{reply=value;for(const [key,id] of Object.entries({tasks:'id',conversations:'chat_id',personas:'key',actions:'id'})){const delta=value.changes?.[key];if(delta){const rows=new Map(collections[key].map(r=>[r[id],r]));for(const r of delta.upsert||[])rows.set(r[id],r);for(const removed of delta.removed||[])rows.delete(removed);collections[key]=delta.order?delta.order.map(id=>rows.get(id)):Array.from(rows.values());}}}};
}

test('idle and status polls do not rebuild main view or stats',async()=>{
  const p=await page(), main=p.element('#mainView'), stats=p.element('#stats');
  const mainWrites=main.writes,statsWrites=stats.writes;
  p.setReply({epoch:'root',revision:1,reset:false,changes:{}});
  await p.api.loadState();
  p.setReply({epoch:'root',revision:2,reset:false,changes:{status:{active_calls:1}}});
  await p.api.loadState();
  assert.equal(main.writes,mainWrites);assert.equal(stats.writes,statsWrites);
  assert.equal(p.element('.tiny-badge').textContent,'处理 1 项中');
});

test('conversation list updates retain the page, search and editor drafts until refresh',async()=>{
  const p=await page();p.api.state.view='conversations';
  await p.api.renderConversations();
  p.element('#conversationSearch').value='同事';
  p.element('#conversationReply').innerHTML='未发送的草稿';
  const writes=p.element('#mainView').writes;
  p.setReply({epoch:'root',revision:2,reset:false,changes:{conversations:{upsert:[{chat_id:'c',name:'同事',kind:'private'}],removed:[],order:['c']}}});
  await p.api.loadState();
  assert.equal(p.element('#mainView').writes,writes);
  assert.equal(p.element('#conversationSearch').value,'同事');
  assert.equal(p.element('#conversationReply').innerHTML,'未发送的草稿');
  assert.match(p.element('#listPager').innerHTML,/内容有更新/);
  await p.api.refreshList('conversations');
  assert.match(p.element('#conversationList').innerHTML,/同事/);
});

test('message, overview and memory updates leave composer intact',async()=>{
  const p=await page();p.api.state.view='conversations';p.api.state.chat='c';
  p.api.state.chatData={epoch:'chat',revision:1,messages:[],memories:[{id:1,content:'old'}],context:{overview:'old'}};
  p.element('#conversationReply').innerHTML='正在输入';
  p.setReply({epoch:'chat',revision:2,reset:false,changes:{messages:{upsert:[{id:1,msg_id:'m',content:'新消息',create_time:0}],removed:[],order:[1]},memories:{upsert:[],removed:[1],order:[]},context:{overview:'新概览'}}});
  await p.api.refreshChatMessages();
  assert.match(p.element('#conversationMessages').innerHTML,/新消息/);
  assert.equal(p.element('#chatOverview').textContent,'新概览');
  assert.match(p.element('#chatMemories').innerHTML,/暂无有效记忆/);
  assert.equal(p.element('#conversationReply').innerHTML,'正在输入');
  assert.match(p.requests.at(-1),/since=1&epoch=chat/);
});

test('hidden pages skip polling and concurrent polls do not duplicate requests',async()=>{
  const p=await page();const before=p.requests.length;
  p.context.document.hidden=true;await p.api.poll();
  assert.equal(p.requests.length,before);
  p.context.document.hidden=false;p.setReply({epoch:'root',revision:1,reset:false,changes:{}});
  await Promise.all([p.api.poll(),p.api.poll()]);
  assert.equal(p.requests.length,before+1);
});

test('paging searches the whole list and retains the page on return and resize',async()=>{
  const p=await page();
  p.setReply({epoch:'root',revision:2,reset:false,changes:{conversations:{upsert:Array.from({length:30},(_,i)=>({chat_id:'c'+i,name:'同事 '+i,kind:'private'})),removed:[],order:Array.from({length:30},(_,i)=>'c'+i)}}});
  await p.api.loadState();p.api.state.view='conversations';await p.api.renderConversations();
  const list=p.api.state.lists.conversations;
  assert.ok(list.size<20);assert.equal(list.total,30);
  list.page=2;await p.api.refreshList('conversations');
  const first=list.items[0].chat_id;
  await p.api.renderConversations();assert.equal(list.page,2);assert.equal(list.items[0].chat_id,first);
  p.context.innerHeight=650;assert.equal(p.api.fitList('conversations'),true);
  await p.api.refreshList('conversations');assert.ok(list.items.some(c=>c.chat_id===first));
  list.query='同事 29';list.page=1;await p.api.refreshList('conversations');
  assert.equal(list.total,1);assert.equal(list.items[0].name,'同事 29');
});

test('an older page response cannot replace newer search results',async()=>{
  const p=await page();p.api.state.view='conversations';await p.api.renderConversations();
  const waiting=[];p.context.fetch=()=>new Promise(resolve=>waiting.push(data=>resolve({ok:true,json:async()=>data})));
  const first=p.api.refreshList('conversations');
  p.api.state.lists.conversations.query='新搜索';
  const second=p.api.refreshList('conversations');
  waiting[1]({items:[{chat_id:'new',name:'新搜索',kind:'private'}],total:1,page:1,pages:1,size:1,revision:2});await second;
  waiting[0]({items:[{chat_id:'old',name:'旧结果',kind:'private'}],total:1,page:1,pages:1,size:1,revision:1});await first;
  assert.equal(p.api.state.lists.conversations.items[0].chat_id,'new');
  assert.match(p.element('#conversationList').innerHTML,/新搜索/);
  assert.doesNotMatch(p.element('#conversationList').innerHTML,/旧结果/);
});

test('settings group rendering retains drafts and reset waits for save',async()=>{
  const p=await page();p.api.state.view='settings';
  const info={groups:[{key:'general',label:'常规',description:'general'},{key:'messages',label:'消息处理',description:'messages'},{key:'model',label:'模型与上下文'},{key:'advanced',label:'性能与高级'},{key:'connection',label:'连接与存储'}],fields:[{key:'reply_suffix',group:'general',kind:'text',maximum:200,label:'回复尾缀',effect:'next call'},{key:'batch_delay',group:'messages',kind:'integer',minimum:1,maximum:30,step:1,scale:1,label:'消息停顿'}],values:{reply_suffix:'Bot',batch_delay:2},defaults:{reply_suffix:'',batch_delay:2},sources:{reply_suffix:'默认值',batch_delay:'默认值'}};
  p.setReply(info);await p.api.renderSettings();
  assert.match(p.element('#mainView').innerHTML,/连接与存储/);
  p.element('#settingsForm').listeners.input({target:{dataset:{settingKey:'reply_suffix'},value:'未保存的尾缀'}});
  p.api.state.settingsGroup='messages';p.api.renderSettingsPanel();
  assert.match(p.element('#mainView').innerHTML,/消息停顿/);
  p.api.state.settingsGroup='general';p.api.renderSettingsPanel();
  assert.match(p.element('#mainView').innerHTML,/未保存的尾缀/);
  const requests=p.requests.length;
  p.element('#settingsReset').listeners.click();
  assert.equal(p.requests.length,requests);
  assert.equal(p.api.state.settingsDrafts.general.reply_suffix,'');
  assert.equal(p.api.state.settingsInfo.values.reply_suffix,'Bot');
});

test('boolean settings preserve unchecked values',async()=>{
  const p=await page();p.api.state.view='settings';
  const info={groups:[{key:'general',label:'常规'}],fields:[{key:'mark_read',group:'general',kind:'boolean',label:'自动标记已读'}],values:{mark_read:true},defaults:{mark_read:true}};
  p.setReply(info);await p.api.renderSettings();
  assert.match(p.element('#mainView').innerHTML,/id="setting-mark_read"[^>]*checked/);
  p.element('#settingsForm').listeners.input({target:{dataset:{settingKey:'mark_read'},checked:false}});
  assert.equal(p.api.state.settingsDrafts.general.mark_read,false);
  p.api.renderSettingsPanel();
  assert.doesNotMatch(p.element('#mainView').innerHTML,/id="setting-mark_read"[^>]*checked/);
});

test('reply selectors distinguish inherited defaults from explicit overrides',async()=>{
  const p=await page();
  p.api.state.data.status.default_private_reply='draft';p.api.state.data.status.default_group_reply='at_me';
  const person={key:'user:u',name:'同事',kind:'person',mode:'off'};
  const group={key:'group:g',name:'群',kind:'group',group_reply_policy:'inherit'};
  assert.match(p.api.replyPolicySelect(person),/value="inherit" >跟随默认（仅草稿）/);
  assert.match(p.api.replyPolicySelect(person),/value="off" selected>不回复/);
  assert.match(p.api.replyPolicySelect(group),/value="inherit" selected>跟随默认（仅 @我）/);
  assert.match(p.api.replyPolicySelect(person,true),/name="mode"/);
  p.api.state.data.status.default_group_reply='off';
  assert.match(p.api.replyPolicySelect(group),/跟随默认（不回复）/);
});

test('CLI and API selection retain per-provider drafts without activating them',async()=>{
  const p=await page();p.api.state.view='settings';p.api.state.settingsGroup='model';
  const models={active:{kind:'cli',provider:'codex'},presets:{cli:{codex:{label:'Codex'},cursor:{label:'Cursor CLI'}},api:{deepseek:{label:'DeepSeek'},qwen:{label:'通义千问'}}},reasoning:{low:'低'},profiles:{cli:{codex:{model:'legacy',reasoning:'low'},cursor:{model:''}},api:{deepseek:{base_url:'https://api.deepseek.com/v1',model:'',vision:false,key_set:true},qwen:{base_url:'https://fixture.example/v1',model:'',vision:false}}}};
  p.setReply({groups:[{key:'model',label:'模型与上下文'}],fields:[],values:{},defaults:{},models});await p.api.renderSettings();
  assert.match(p.element('#mainView').innerHTML,/CLI（本地工具）/);
  assert.match(p.element('#mainView').innerHTML,/value="legacy"/);
  const requests=p.requests.length;
  p.element('#modelConnectionForm').listeners.change({target:{dataset:{modelKey:'kind'},value:'api'}});
  assert.match(p.element('#mainView').innerHTML,/接口地址/);
  assert.match(p.element('#mainView').innerHTML,/已设置，留空保留/);
  p.element('#modelConnectionForm').listeners.input({target:{dataset:{modelKey:'model'},value:'draft-model'}});
  p.element('#modelConnectionForm').listeners.change({target:{dataset:{modelKey:'provider'},value:'qwen'}});
  assert.doesNotMatch(p.element('#mainView').innerHTML,/value="draft-model"/);
  p.element('#modelConnectionForm').listeners.change({target:{dataset:{modelKey:'provider'},value:'deepseek'}});
  assert.match(p.element('#mainView').innerHTML,/value="draft-model"/);
  assert.equal(p.requests.length,requests);
  assert.equal(models.active.kind,'cli');
  p.element('#modelConnectionForm').listeners.change({target:{dataset:{modelKey:'kind'},value:'cli'}});
  p.setReply({path:'C:/tools/codex.cmd'});
  const picking=p.element('#browseModelPath').listeners.click();
  p.element('#modelConnectionForm').listeners.change({target:{dataset:{modelKey:'provider'},value:'cursor'}});
  await picking;
  assert.equal(p.api.state.modelDrafts['cli:codex'].path,'C:/tools/codex.cmd');
  assert.equal(p.api.state.modelDrafts['cli:cursor']?.path,undefined);
  assert.equal(p.requests.at(-1),'/api/settings/path-picker');
  assert.equal(models.active.provider,'codex');
});

test('new settings assets show a restart hint when the running backend is older',async()=>{
  const p=await page();p.api.state.view='settings';p.api.state.settingsGroup='connection';
  p.setReply({groups:[{key:'connection',label:'连接与存储'}],fields:[],values:{},defaults:{},startup:{values:{data_dir:'C:/data',storage_url:'sqlite:///C:/data/messages.db',port:8765}}});
  await p.api.renderSettings();
  assert.match(p.element('#mainView').innerHTML,/请重启工作台/);
  assert.doesNotMatch(p.element('#mainView').innerHTML,/id="startupForm"/);
});

test('startup fields remain editable and keep SQLite drafts while switching storage',async()=>{
  const p=await page();p.api.state.view='settings';p.api.state.settingsGroup='connection';
  const startup={values:{data_dir:'C:/data',storage_url:'sqlite:///C:/data/messages.db',port:8765},current:{data_dir:'C:/data',storage_url:'sqlite:///C:/data/messages.db',port:8765},storage:{storage_kind:'sqlite',sqlite_path:'C:/data/messages.db',mysql_host:'localhost',mysql_port:3306,mysql_database:'',mysql_user:''},restart_required:false};
  p.setReply({groups:[{key:'connection',label:'连接与存储'}],fields:[],values:{},defaults:{},path_picker:{available:true},startup,connection:{'登录区域':'feishu','服务地址':'http://127.0.0.1:8765','模型接入':'cli: codex'}});
  await p.api.renderSettings();
  assert.match(p.element('#mainView').innerHTML,/aria-label="数据目录" value="C:\/data"/);
  assert.match(p.element('#mainView').innerHTML,/数据库文件/);
  assert.doesNotMatch(p.element('#mainView').innerHTML,/newDatabase|新建…/);
  assert.match(p.element('#mainView').innerHTML,/文件不存在时自动创建/);
  const writes=p.requests.length;
  p.element('#startupForm').listeners.input({target:{dataset:{startupKey:'port'},value:'9876'}});
  p.element('#startupStorageKind').listeners.change({target:{value:'mysql'}});
  assert.match(p.element('#mainView').innerHTML,/数据库密码/);
  assert.match(p.element('#mainView').innerHTML,/数据库账号/);
  assert.match(p.element('#mainView').innerHTML,/数据库主机/);
  p.element('#startupForm').listeners.input({target:{dataset:{startupKey:'mysql_user'},value:'test-user'}});
  p.element('#startupForm').listeners.input({target:{dataset:{startupKey:'mysql_port'},value:'3307'}});
  p.element('#startupStorageKind').listeners.change({target:{value:'sqlite'}});
  assert.equal(p.api.state.startupDraft.sqlite_path,'C:/data/messages.db');
  p.element('#startupStorageKind').listeners.change({target:{value:'mysql'}});
  assert.equal(p.api.state.startupDraft.mysql_user,'test-user');
  assert.equal(p.api.state.startupDraft.mysql_port,3307);
  p.element('#startupStorageKind').listeners.change({target:{value:'sqlite'}});
  assert.equal(p.api.state.startupDraft.port,9876);
  assert.equal(p.requests.length,writes);
  assert.equal(startup.values.port,8765);
  const platform=p.element('#mainView').innerHTML.match(/<select id="messagePlatform"[\s\S]*?<\/select>/)[0];
  assert.equal((platform.match(/<option /g)||[]).length,1);
  assert.match(platform,/>飞书<\/option>/);
  p.setReply({path:'C:/选择的目录'});
  await p.element('#browseDataDir').listeners.click();
  assert.equal(p.api.state.startupDraft.data_dir,'C:/选择的目录');
  assert.equal(startup.values.data_dir,'C:/data');
  assert.equal(p.requests.at(-1),'/api/settings/path-picker');
  p.setReply({path:'C:/选择的目录/new.db'});
  await p.element('#browseDatabase').listeners.click();
  assert.equal(p.api.state.startupDraft.sqlite_path,'C:/选择的目录/new.db');
  p.setReply({path:null});
  await p.element('#browseDatabase').listeners.click();
  assert.equal(p.api.state.startupDraft.sqlite_path,'C:/选择的目录/new.db');
  assert.equal(p.requests.length,writes+3);
});


test('server reply draft is escaped and never overwrites a nonempty composer',async()=>{
  const p=await page();p.api.state.chat='c';
  p.api.state.chatData={draft:{key:'d',chat_id:'c',content:'<script>fixture</script>',updated_at:'v1'}};
  const editor=p.element('#conversationReply');editor.childNodes=[{nodeType:3,textContent:'手写内容'}];editor.querySelector=()=>null;editor.textContent='手写内容';
  const before=p.requests.length;p.api.paintServerDraft();
  assert.match(p.element('#serverReplyDraft').innerHTML,/&lt;script&gt;/);
  p.element('#serverDraftUse').onclick();assert.equal(editor.textContent,'手写内容');
  assert.match(p.element('#toast').textContent,/输入框已有内容/);
  editor.childNodes=[];p.element('#serverDraftUse').onclick();
  assert.equal(editor.textContent,'<script>fixture</script>');
  assert.equal(p.api.state.loadedServerDraft.key,'d');
  assert.equal(p.requests.length,before);
});

test('a newer server draft updates its preview while preserving the composer and used version',async()=>{
  const p=await page();p.api.state.chat='c';p.api.state.chatData={draft:{key:'d',chat_id:'c',content:'第一版',updated_at:'v1'}};
  p.api.state.loadedServerDraft={...p.api.state.chatData.draft};
  p.element('#conversationReply').innerHTML='用户正在修改';p.api.paintServerDraft();
  const writes=p.element('#serverReplyDraft').writes;p.api.paintServerDraft();assert.equal(p.element('#serverReplyDraft').writes,writes);
  p.api.state.chatData.draft={key:'d',chat_id:'c',content:'第二版',updated_at:'v2'};p.api.paintServerDraft();
  assert.match(p.element('#serverReplyDraft').innerHTML,/第二版/);
  assert.equal(p.element('#conversationReply').innerHTML,'用户正在修改');
  assert.equal(p.api.state.loadedServerDraft.updated_at,'v1');
});

test('technical incidents and uncertain sends appear as notices without adding tasks',async()=>{
  const p=await page();const stats=p.element('#stats').writes;
  p.setReply({epoch:'root',revision:2,reset:false,changes:{status:{system_incidents:1,uncertain_sends:2}}});
  await p.api.loadState();assert.equal(p.element('#homeError').hidden,false);
  assert.match(p.element('#homeError').textContent,/1 项系统异常/);assert.match(p.element('#homeError').textContent,/2 条发送结果待核对/);
  assert.equal(p.api.state.data.tasks.length,0);assert.equal(p.element('#stats').writes,stats);
});
