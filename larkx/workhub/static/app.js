const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'home',filter:'open',data:null,chat:null,task:null,chatDrafts:{},chatPolishing:new Set()};
const viewNames = {home:'工作台',conversations:'会话与记忆',personas:'角色档案',activity:'执行记录',settings:'数字人设置'};
const statusNames = {open:'待处理',in_progress:'进行中',done:'已完成',needs_attention:'需要你决定',pending:'处理中',sent:'已发送',failed:'失败',uncertain:'待核对',resolved:'已恢复',archived:'已归档',checked:'已核对',auto:'自动回复',draft:'先留草稿',off:'不自动回复'};
async function api(path, options={}) {
  const r = await fetch(path,{...options,headers:{'Content-Type':'application/json','X-Workhub':'1',...options.headers}});
  const data = await r.json();
  if(!r.ok) throw Error(typeof data.detail==='string'?data.detail:'操作失败，请检查输入');
  return data;
}
const mutation = (method, data) => ({method,body:JSON.stringify(data)});
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');setTimeout(()=>$('#toast').classList.remove('show'),3500);}
function timeLabel(ts){if(!ts)return '';return new Date(ts.includes('T')?ts:ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function listRows(key){return state.lists?.[key]?.items||state.data?.[key]||[];}
function findProfile(key){return listRows('personas').find(p=>p.key===key)||[...listRows('conversations'),...state.data.conversations].find(c=>c.persona?.key===key)?.persona||(state.chatPersona?.key===key?state.chatPersona:null);}
function chatName(id){return [...listRows('conversations'),...state.data.conversations].find(c=>c.chat_id===id)?.name || '未关联会话';}
function humanPauseLabel(c){const seconds=Math.ceil(((c?.human_pause_until||0)*1000-Date.now())/1000);return seconds>0?`人工接管中 · 自动回复暂停 ${seconds} 秒`:'人工接管暂停已结束';}
function displayContent(text){const v=String(text||'');if(v.startsWith('[图片]'))return '图片';if(v.startsWith('[表情包]'))return '表情包';return v;}
function empty(title, sub='新的工作事项会出现在这里。'){return `<div class="empty"><div class="empty-icon">✓</div><strong>${esc(title)}</strong></div>`;}
function badge(status){return `<span class="badge ${esc(status)}">${esc(statusNames[status]||status)}</span>`;}
const listIDs={tasks:'homeTasks',conversations:'conversationList',personas:'personaList',actions:'activityList'};
function activeCollection(){return state.view==='conversations'&&state.chat?null:({home:'tasks',conversations:'conversations',personas:'personas',activity:'actions'}[state.view]);}
function listState(key){state.lists||={};return state.lists[key]||=( {page:1,size:1,query:'',kind:'all',status:key==='tasks'?'open':'all',start:'',end:'',items:[],total:0,pages:1,loaded:false,version:0,scrollTop:0});}
function listToolbar(key){
  const p=listState(key),label={tasks:'搜索待办',conversations:'搜索联系人或群聊',personas:'搜索姓名或角色',actions:'搜索执行内容或会话'}[key];
  const options=(values,selected)=>values.map(([v,label])=>`<option value="${v}" ${v===selected?'selected':''}>${label}</option>`).join('');
  return `<div class="list-toolbar" id="listToolbar"><input id="${key==='conversations'?'conversationSearch':'listSearch'}" data-list-field="query" aria-label="${label}" placeholder="${label}" maxlength="200" value="${esc(p.query)}">${['conversations','personas'].includes(key)?`<select data-list-field="kind" aria-label="类型筛选">${options([['all','全部'],['private','个人'],['group','群组']],p.kind)}</select>`:''}${key==='actions'?`<select data-list-field="status" aria-label="执行状态">${options([['all','全部状态'],['pending','处理中'],['sent','已发送'],['failed','失败'],['uncertain','待核对'],['resolved','已恢复'],['checked','已核对'],['archived','已归档']],p.status)}</select><label>从<input data-list-field="start" aria-label="开始日期" type="date" value="${esc(p.start)}"></label><label>至<input data-list-field="end" aria-label="结束日期" type="date" value="${esc(p.end)}"></label>`:''}</div>`;
}
function listBody(key){return `<div id="${listIDs[key]}" class="paged-rows" tabindex="-1"></div><div id="listPager" class="list-pager" aria-label="列表分页"></div>`;}
function fitList(key){
  const p=listState(key),box=$('#'+listIDs[key]);if(!box)return false;
  const height=typeof getComputedStyle==='function'?parseFloat(getComputedStyle(box).getPropertyValue('--list-row-height')):0;
  const rowHeight=height||({tasks:80,conversations:76,personas:88,actions:88}[key]);
  const viewport=globalThis.innerHeight||900,top=box.getBoundingClientRect?.().top??320,footer=$('#listPager')?.getBoundingClientRect?.().height||60;
  const size=WorkhubPaging.capacity(viewport,top,rowHeight,footer);
  box.style.height=(size*rowHeight)+'px';
  const changed=size!==p.size;p.page=WorkhubPaging.resizedPage(p.page,p.size,size);p.size=size;return changed;
}
function renderPager(key){
  if(activeCollection()!==key||!$('#listPager'))return;
  const p=listState(key),pages=WorkhubPaging.numbers(p.page,p.pages);let previous=0;
  $('#listPager').innerHTML=`<span class="page-total">共 ${p.total} 条</span><button class="list-update" data-list-refresh ${p.dirty?'':'hidden'}>内容有更新 · 刷新</button><nav aria-label="页码"><button data-list-page="${p.page-1}" ${p.page<=1?'disabled':''} aria-label="上一页">‹</button>${pages.map(n=>{const gap=previous&&n-previous>1?' <span>…</span> ':'';previous=n;return gap+`<button data-list-page="${n}" ${n===p.page?'aria-current="page"':''}>${n}</button>`;}).join('')}<button data-list-page="${p.page+1}" ${p.page>=p.pages?'disabled':''} aria-label="下一页">›</button></nav>`;
}
function personaRow(p){return `<div class="persona-row"><span class="avatar">${esc(p.kind==='group'?'群':p.name.slice(0,1))}</span><div><h3>${esc(p.name)}</h3><p>${esc(p.role||'未设置角色')}${p.description?' · '+esc(p.description):''}</p></div>${replyPolicySelect(p)}<button class="button ghost" data-persona="${esc(p.key)}">编辑</button></div>`;}
function activityRow(a){return `<button type="button" class="activity-row" data-action="${a.id}"><div class="meta"><span>${esc(a.kind==='system'&&!a.chat_id?'模型服务':a.chat_name||chatName(a.chat_id))} · ${esc(timeLabel(a.created_at))}</span>${badge(a.status)}</div><p>${a.kind==='system'?'系统异常 · ':a.kind==='archive'?'归档 · ':''}${esc(a.content||a.error||'无正文')}</p></button>`;}
function paintList(key){
  if(activeCollection()!==key)return;
  const p=listState(key),box=$('#'+listIDs[key]);if(!box)return;
  box.innerHTML=p.items.map(({tasks:taskRow,conversations:conversationRow,personas:personaRow,actions:activityRow})[key]).join('')||empty(p.query?'没有匹配的记录':'暂无记录');
  box.scrollTop=p.scrollTop;renderPager(key);
}
async function refreshList(key){
  const p=listState(key),version=++p.version;
  const submitted={page:p.page,size:p.size,query:p.query,kind:p.kind,status:p.status,start:p.start,end:p.end};
  const box=$('#'+listIDs[key]);if(activeCollection()===key)box?.setAttribute('aria-busy','true');
  try{
    const data=await api('/api/lists/'+key+'?'+new URLSearchParams(submitted));
    if(p.version!==version)return;
    Object.assign(p,data,{loaded:true,dirty:false});paintList(key);
  }catch(e){if(p.version===version&&activeCollection()===key){box.innerHTML=`${empty('列表加载失败')}<p class="section-pad danger-text">${esc(e.message)}</p><button class="button secondary" data-list-refresh>重试</button>`;}}
  finally{if(p.version===version&&activeCollection()===key)$('#'+listIDs[key])?.setAttribute('aria-busy','false');}
}
function mountList(key){
  const p=listState(key),resized=fitList(key),toolbar=$('#listToolbar');
  const change=e=>{const field=e.target.dataset.listField;if(!field)return;clearTimeout(p.searchTimer);p[field]=e.target.value;p.page=1;p.scrollTop=0;p.version++;p.searchTimer=setTimeout(()=>refreshList(key),field==='query'?250:0);};
  toolbar.addEventListener('input',e=>{if(e.target.dataset.listField==='query')change(e);});
  toolbar.addEventListener('change',e=>{if(e.target.dataset.listField!=='query')change(e);});
  $('#'+listIDs[key]).addEventListener('scroll',e=>{p.scrollTop=e.target.scrollTop;});
  if(p.loaded&&!resized){paintList(key);return Promise.resolve();}
  return refreshList(key);
}
let resizeTimer;
globalThis.addEventListener?.('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{const key=activeCollection();if(key&&fitList(key))refreshList(key);},150);});
function openListDetail(kind,id){
  const row=listRows(kind).find(r=>kind==='personas'?r.key===id:r.id===Number(id));if(!row)return;
  $('#listDetailTitle').textContent=kind==='personas'?row.name:'执行详情';
  $('#listDetailBody').innerHTML=kind==='personas'?personaForm({...row,...state.personaDrafts?.[row.key]}):`<div class="meta">${esc(row.chat_name||chatName(row.chat_id))} · ${esc(timeLabel(row.created_at))} ${badge(row.status)}</div><p class="record-content">${esc(row.content)}</p>${row.error?`<p class="record-content danger-text">${esc(row.error)}</p>`:''}${row.kind==='send'&&row.status==='uncertain'?'<button type="button" class="button secondary" id="confirmSendChecked">已核对</button>':''}`;
  if($('#confirmSendChecked'))$('#confirmSendChecked').onclick=async()=>{try{await api(`/api/actions/${row.id}/checked`,{method:'POST'});$('#listDetailDialog').close();await loadState(true);}catch(e){toast(e.message);}};
  $('#listDetailDialog').showModal();
}
let stateLoading=Promise.resolve();
function loadState(refresh=false){
  stateLoading=stateLoading.catch(()=>{}).then(()=>updateState(refresh));
  return stateLoading;
}
async function updateState(refresh=false){
  const initial=!state.data;
  const packet=await api(`/api/updates?scope=web&since=${state.data?.revision||0}&epoch=${encodeURIComponent(state.data?.epoch||'')}`);
  const update=WorkhubSync.apply(state.data,packet,{tasks:'id',conversations:'chat_id',personas:'key',actions:'id'});
  state.data=update.data;const changed=update.changed;
  const {status,conversations}=state.data;
  $('#connectionDot').className=status.connection==='connected'?'connected':'';
  const connectionLabel={connected:'飞书已连接',starting:'正在启动',connecting:'正在连接',reconnecting:'正在重连',error:'连接异常'}[status.connection]||status.connection;
  $('#connectionText').textContent=connectionLabel;
  if($('#settingsConnection'))$('#settingsConnection').textContent=connectionLabel;
  $('.tiny-badge').textContent=status.active_calls?`处理 ${status.active_calls} 项中`:status.connection==='connected'?'已就绪':'待连接';
  if(initial||JSON.stringify(status.totals)!==state.statsSignature){
  const totals=status.totals||{};state.statsSignature=JSON.stringify(status.totals);
  const cards=[['等待处理',totals.waiting||0,'','☷'],['需要你决定',totals.attention||0,'','◇'],['建立联系',totals.contacts||0,'','♧'],['已完成',totals.completed||0,'','↗']];
  $('#stats').innerHTML=cards.map(([label,value,note,icon])=>`<div class="stat"><div class="stat-label">${label}</div><span class="stat-icon">${icon}</span><div class="stat-value">${value.toString().padStart(2,'0')}</div></div>`).join('');
  }
  if($('#humanPauseStatus')){const c=conversations.find(c=>c.chat_id===state.chat);$('#humanPauseStatus').textContent=c?.human_pause_until?' · '+humanPauseLabel(c):'';}
  document.querySelectorAll('[data-reply-policy]').forEach(el=>{
    const p=findProfile(el.dataset.replyPolicy);
    if(p&&!el.disabled){el.value=p.kind==='group'?p.group_reply_policy:p.mode;el.options[0].textContent=replyOptions(p).inherit;}
  });
  if(initial)await renderView();
  else {
    if(state.view==='home')refreshHome(changed);
    for(const [key,p] of Object.entries(state.lists||{}))if(p.loaded&&(packet.reset||p.revision!==status.list_revision)){p.dirty=true;if(activeCollection()===key)renderPager(key);}
    if(refresh&&activeCollection())await refreshList(activeCollection());
  }
}
function renderView(){
  if(!state.data)return;
  globalThis.scrollTo?.(0,0);
  document.body.dataset.view=state.view;
  if(state.view!=='conversations')document.body.dataset.chatOpen='false';
  $('.nav.active')?.classList.remove('active');$(`.nav[data-view="${state.view}"]`).classList.add('active');
  $('#breadcrumb').textContent=viewNames[state.view];
  $('#pageTitle').textContent={home:'工作台',conversations:'会话与记忆',personas:'联系人与角色',activity:'执行记录',settings:'数字人设置'}[state.view];
  return ({home:renderHome,conversations:renderConversations,personas:renderPersonas,activity:renderActivity,settings:renderSettings}[state.view])();
}
function taskRow(t){return `<div class="task-row"><button class="task-dot ${t.status==='done'?'done':''}" data-done="${t.id}" aria-label="标记完成">${t.status==='done'?'✓':''}</button><div class="task-content"><div class="task-title" data-task="${t.id}">${esc(t.title)}</div><div class="task-meta"><span>${esc(t.chat_name||chatName(t.chat_id))}</span><span>·</span><span>${esc(t.due_at||timeLabel(t.created_at))}</span>${t.priority==='high'?'<span class="badge high">优先处理</span>':''}${t.status==='needs_attention'?badge(t.status):''}</div></div><button class="button ghost" data-task="${t.id}">处理 →</button></div>`;}
function renderHome(){
  const p=listState('tasks');p.status=state.filter;
  $('#mainView').innerHTML=`<div class="card paged-card" data-collection="tasks"><div class="card-head"><h2>我的待办</h2><button class="button secondary" id="newTask">＋ 添加待办</button></div><div class="tabs">${[['open','待处理'],['in_progress','进行中'],['done','已完成'],['all','全部']].map(([v,n])=>`<button class="tab ${v===state.filter?'active':''}" data-filter="${v}">${n}</button>`).join('')}</div>${listToolbar('tasks')}${listBody('tasks')}</div><div class="card home-recent"><div class="card-head"><h2>最近的联系</h2><button class="button ghost" data-view="conversations">全部会话 →</button></div><div id="recentContacts">${state.data.conversations.slice(0,4).map(conversationRow).join('')||empty('等待新的联系')}</div></div><div id="homeError" class="hint danger-text" ${homeNotice()?'':'hidden'}>${esc(homeNotice())}</div>`;
  return mountList('tasks');
}
function homeNotice(){const s=state.data?.status||{};return [s.error,s.system_incidents?`${s.system_incidents} 项系统异常，请查看执行记录`:null,s.uncertain_sends?`${s.uncertain_sends} 条发送结果待核对，请查看执行记录`:null].filter(Boolean).join('；');}
function refreshHome(changed){
  if(changed.has('status')&&$('#homeError')){$('#homeError').textContent=homeNotice();$('#homeError').hidden=!homeNotice();}
  if(changed.has('conversations')&&$('#recentContacts'))$('#recentContacts').innerHTML=state.data.conversations.slice(0,4).map(conversationRow).join('')||empty('等待新的联系');
}
function conversationRow(c){return `<button type="button" class="conversation-row ${state.chat===c.chat_id?'selected':''}" data-chat="${esc(c.chat_id)}"><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div><h3>${esc(c.name)}${c.kind==='group'?'<span class="badge group">群聊</span>':''}</h3><p>${esc(displayContent(c.last_message)||c.overview||'暂无消息')}</p></div>${c.open_tasks?`<span class="badge">${c.open_tasks} 项待办</span>`:''}</button>`;}
function renderConversations(){
  state.chat=null;document.body.dataset.chatOpen='false';
  $('#mainView').innerHTML=`<div class="card chat-list-card paged-card" data-collection="conversations"><div class="card-head"><h2>消息</h2></div>${listToolbar('conversations')}${listBody('conversations')}</div>`;
  return mountList('conversations');
}
async function selectChat(id){
  if(state.view!=='conversations'){state.view='conversations';renderView();}
  state.chat=id;
  const c=[...listRows('conversations'),...state.data.conversations].find(c=>c.chat_id===id);if(!c)return;
  const d=await api(`/api/conversations/${encodeURIComponent(id)}`);
  if(state.chat!==id||state.view!=='conversations')return;
  state.chatHistory={id,items:[],before:d.history?.next_before_id||null,busy:false};
  state.chatPersona=c.persona;
  document.body.dataset.chatOpen='true';
  $('#mainView').innerHTML='<div id="conversationDetail"></div>';
  $('#conversationDetail').innerHTML=`<div class="card wechat-room"><div class="chat-room-head"><button class="icon-button" id="chatBack" aria-label="返回会话列表" title="返回会话列表"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg></button><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div class="chat-room-title"><h2>${esc(c.name)}</h2><p>${c.kind==='group'?'群聊':'私聊'}<span id="humanPauseStatus">${c.human_pause_until?' · '+esc(humanPauseLabel(c)):''}</span></p></div>${c.persona?replyPolicySelect(c.persona):''}</div><details class="chat-context"><summary>会话概览与记忆 <span>查看背景</span></summary><div class="chat-context-body"><div class="row-flex"><strong>会话概览</strong><button class="button ghost" id="refreshOverview">更新概览</button></div><div class="overview" id="chatOverview">${esc(d.context.overview||'暂无概览，收到新消息后会持续更新。')}</div><div class="label-heading">有效记忆</div><div id="chatMemories">${d.memories.length?d.memories.map(m=>`<div class="memory-item"><span>${esc(m.content)}</span><button data-forget="${m.id}">移除</button></div>`).join(''):'<p class="subtle">暂无有效记忆</p>'}</div></div></details><div class="messages" id="conversationMessages">${messagesHtml(d.messages,id)}</div><div class="chat-compose-area"><div id="serverReplyDraft"></div><form class="inline-compose" id="conversationSend"><div id="conversationReply" contenteditable="true" role="textbox" aria-multiline="true" aria-label="回复当前会话" data-placeholder="输入消息…"></div><input id="chatImageInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden><div class="chat-compose-toolbar"><button type="button" class="button secondary" id="chatAttachImage">＋ 图片</button><span class="subtle">Ctrl + Enter 发送</span><button type="button" class="button secondary" id="conversationPolish">✳ AI 润色</button><button class="button primary" id="conversationSendButton">发送</button></div></form><p class="subtle" id="conversationPolishHint"></p></div></div>`;
  $('#chatBack').addEventListener('click',()=>{renderConversations();window.scrollTo(0,0);$('#conversationSearch').focus({preventScroll:true});});
  state.chatData=d;
  $('#conversationMessages').innerHTML=chatMessagesHtml(d.messages,id);
  $('#conversationMessages').addEventListener('scroll',()=>{if($('#conversationMessages').scrollTop<40)loadEarlierMessages();});
  window.scrollTo(0,0);
  $('#conversationMessages').scrollTop=$('#conversationMessages').scrollHeight;
  const replyInput=$('#conversationReply');
  state.chatRichDrafts ||= {};state.chatUploading ||= new Set();
  if(state.chatRichDrafts[id])replyInput.innerHTML=state.chatRichDrafts[id];else replyInput.textContent=state.chatDrafts[id]||'';
  const saveDraft=()=>{state.chatRichDrafts[id]=replyInput.innerHTML;state.chatDrafts[id]=editorText(replyInput);if(!state.chatDrafts[id].trim()&&!replyInput.querySelector('figure')&&state.loadedServerDraft?.chat_id===id)state.loadedServerDraft=null;};
  paintServerDraft();
  replyInput.addEventListener('input',saveDraft);
  replyInput.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='Enter'&&!$('#conversationSendButton').disabled){e.preventDefault();$('#conversationSend').requestSubmit($('#conversationSendButton'));}});
  let savedRange=null;
  const rememberRange=()=>{const selection=window.getSelection();if(selection.rangeCount&&replyInput.contains(selection.anchorNode))savedRange=selection.getRangeAt(0).cloneRange();};
  ['keyup','mouseup','input'].forEach(event=>replyInput.addEventListener(event,rememberRange));
  const updateBusy=()=>{const busy=state.chatPolishing.has(id)||state.chatUploading.has(id);$('#conversationPolish').disabled=busy;$('#conversationSendButton').disabled=busy;$('#chatAttachImage').disabled=state.chatUploading.has(id);};
  updateBusy();
  async function insertImages(files){
    if(state.chatUploading.has(id)){toast('图片上传中，请稍后');return;}
    const existing=editorParts(replyInput).filter(p=>p.type==='image').length;
    if(existing+files.length>state.data.status.limits.send_images){toast(`最多 ${state.data.status.limits.send_images} 张图片`);return;}
    state.chatUploading.add(id);updateBusy();
    try{for(const file of files){
      if(!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)||file.size>state.data.status.limits.upload_bytes){throw Error('仅支持 10 MB 以内的 PNG、JPG、GIF、WebP');}
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const asset=await api('/api/media/upload',mutation('POST',{data}));
      const figure=document.createElement('figure');figure.contentEditable='false';figure.className='compose-image';
      const img=document.createElement('img');img.src=asset.url;img.dataset.uploadId=asset.upload_id;img.alt='待发送图片';figure.append(img);
      const remove=document.createElement('button');remove.type='button';remove.dataset.removeImage='1';remove.setAttribute('aria-label','移除图片');remove.textContent='×';figure.append(remove);
      const target=replyInput.isConnected?replyInput:(state.chat===id&&state.view==='conversations'?$('#conversationReply'):null);
      if(target){
        if(target===replyInput&&savedRange&&replyInput.contains(savedRange.commonAncestorContainer)){savedRange.collapse(false);savedRange.insertNode(figure);savedRange.setStartAfter(figure);savedRange.collapse(true);window.getSelection().removeAllRanges();window.getSelection().addRange(savedRange);}
        else target.append(figure);
        target.append(document.createElement('br'));
        if(target===replyInput)saveDraft();else target.dispatchEvent(new Event('input',{bubbles:true}));
      }else{state.chatRichDrafts[id]=(state.chatRichDrafts[id]||'')+figure.outerHTML+'<br>';}
    }}catch(e){toast(e.message||'图片上传失败');}
    finally{state.chatUploading.delete(id);if(state.chat===id&&state.view==='conversations'&&$('#conversationSendButton')){$('#conversationSendButton').disabled=state.chatPolishing.has(id);$('#conversationPolish').disabled=state.chatPolishing.has(id);$('#chatAttachImage').disabled=false;}}
  }
  $('#chatAttachImage').addEventListener('click',()=>{rememberRange();$('#chatImageInput').click();});
  $('#chatImageInput').addEventListener('change',e=>{const files=Array.from(e.target.files);e.target.value='';insertImages(files);});
  replyInput.addEventListener('paste',e=>{
    e.preventDefault();rememberRange();
    const files=Array.from(e.clipboardData.files).filter(f=>f.type.startsWith('image/'));
    if(files.length){insertImages(files);return;}
    const text=e.clipboardData.getData('text/plain');const selection=window.getSelection();
    if(selection.rangeCount&&replyInput.contains(selection.anchorNode)){const range=selection.getRangeAt(0);range.deleteContents();const node=document.createTextNode(text);range.insertNode(node);range.setStartAfter(node);range.collapse(true);selection.removeAllRanges();selection.addRange(range);}else replyInput.append(document.createTextNode(text));saveDraft();
  });
  replyInput.addEventListener('dragover',e=>e.preventDefault());
  replyInput.addEventListener('drop',e=>{e.preventDefault();const files=Array.from(e.dataTransfer.files).filter(f=>f.type.startsWith('image/'));if(files.length)insertImages(files);});
  replyInput.addEventListener('click',e=>{const remove=e.target.closest('[data-remove-image]');if(remove){remove.closest('figure').remove();saveDraft();}});
  $('#conversationPolish').addEventListener('click',async()=>{
    const original=replyInput.innerHTML,text=editorText(replyInput);saveDraft();state.chatPolishing.add(id);updateBusy();$('#conversationPolishHint').textContent='润色中…';
    try{const j=await api(`/api/conversations/${encodeURIComponent(id)}/polish`,mutation('POST',{text}));const result=await waitJob(j.job_id);
      if(replyInput.isConnected&&replyInput.innerHTML===original){const images=Array.from(replyInput.querySelectorAll('figure'));replyInput.textContent=result.reply;for(const image of images){replyInput.append(document.createElement('br'),image);}saveDraft();$('#conversationPolishHint').textContent='已润色';}
      else{if(!replyInput.isConnected){const draft=document.createElement('div');draft.innerHTML=state.chatRichDrafts[id]||'';if(draft.innerHTML===original){const images=Array.from(draft.querySelectorAll('figure'));draft.textContent=result.reply;for(const image of images)draft.append(document.createElement('br'),image);state.chatRichDrafts[id]=draft.innerHTML;state.chatDrafts[id]=result.reply;toast('草稿已润色');}else toast('草稿已变更，未覆盖当前内容');}else toast('草稿已变更，未覆盖当前内容');}
    }catch(e){toast(e.message);}finally{state.chatPolishing.delete(id);if(state.chat===id&&state.view==='conversations'&&$('#conversationSendButton')){$('#conversationSendButton').disabled=state.chatUploading.has(id);$('#conversationPolish').disabled=state.chatUploading.has(id);}}
  });
  $('#conversationSend').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter,original=replyInput.innerHTML,parts=editorParts(replyInput);saveDraft();
    if(!parts.some(p=>p.type==='image'||p.text?.trim())){toast('请输入消息或添加图片');return;}b.disabled=true;
    try{const r=await api(`/api/conversations/${id}/send-rich`,mutation('POST',{parts,request_id:crypto.randomUUID(),anchor:state.loadedServerDraft?.chat_id===id?state.loadedServerDraft.anchor||'':''}));toast(statusNames[r.status]||r.status);
      if(r.status==='sent')await clearUsedServerDraft(id);
      if(r.status==='sent'&&state.chatRichDrafts[id]===original){state.chatDrafts[id]='';state.chatRichDrafts[id]='';if(replyInput.isConnected)replyInput.innerHTML='';}
      await loadState(true);await refreshChatMessages();
    }catch(e){toast(e.message);}finally{b.disabled=false;}
  });
  $('#refreshOverview').addEventListener('click',async e=>{const b=e.target;b.disabled=true;b.textContent='分析中…';try{const j=await api(`/api/conversations/${id}/overview`,{method:'POST'});await waitJob(j.job_id);await loadState(true);if(state.chat===id&&state.view==='conversations')await refreshChatMessages();toast('会话概览已更新');b.disabled=false;b.textContent='更新概览';}catch(e){toast(e.message);b.disabled=false;b.textContent='更新概览';}});
}
function paintServerDraft(){
  const box=$('#serverReplyDraft');if(!box)return;
  const draft=state.chatData?.draft||{},signature=draft.key+':'+draft.updated_at;
  if(box.dataset.version===signature)return;box.dataset.version=signature;
  box.innerHTML=draft.content?`<details class="server-draft"><summary>AI 回复草稿${draft.anchor?' · 话题':''}</summary><p class="record-content">${esc(draft.content)}</p><button type="button" class="button secondary" id="serverDraftUse">使用草稿</button> <button type="button" class="button ghost" id="serverDraftDismiss">忽略</button></details>`:'';
  if(!draft.content)return;
  $('#serverDraftUse').onclick=()=>{const editor=$('#conversationReply');if(editorText(editor).trim()||editor.querySelector('figure')){toast('输入框已有内容，请先处理后再使用草稿');return;}editor.textContent=draft.content;state.chatDrafts[state.chat]=draft.content;state.chatRichDrafts||={};state.chatRichDrafts[state.chat]=editor.innerHTML;state.loadedServerDraft={...draft};editor.focus();};
  $('#serverDraftDismiss').onclick=async()=>{try{await api(`/api/conversations/${encodeURIComponent(state.chat)}/draft/clear`,mutation('POST',{key:draft.key,version:draft.updated_at}));await refreshChatMessages();}catch(e){toast(e.message);}};
}
async function clearUsedServerDraft(id){
  const used=state.loadedServerDraft;if(!used||used.chat_id!==id)return;
  state.loadedServerDraft=null;
  try{await api(`/api/conversations/${encodeURIComponent(id)}/draft/clear`,mutation('POST',{key:used.key,version:used.updated_at}));}
  catch{toast('回复已发送，草稿已有更新，请查看新草稿');}
}
async function refreshChatMessages(){
  const id=state.chat;if(state.view!=='conversations'||!id||state.chatRefreshing||!$('#conversationMessages'))return;
  state.chatRefreshing=true;
  try{
    const previous=state.chatData;
    const packet=await api(`/api/conversations/${encodeURIComponent(id)}?since=${previous?.revision||0}&epoch=${encodeURIComponent(previous?.epoch||'')}`);
    if(state.view!=='conversations'||state.chat!==id||state.chatData!==previous||!$('#conversationMessages'))return;
    const update=WorkhubSync.apply(previous,packet,{messages:'id',memories:'id'});state.chatData=update.data;
    paintServerDraft();
    if(update.changed.has('messages')){
      const box=$('#conversationMessages'),top=box.scrollTop,nearBottom=box.scrollHeight-box.clientHeight-top<60;
      if(state.chatHistory?.id===id){const ids=new Set(update.data.messages.map(m=>m.id));state.chatHistory.items.push(...previous.messages.filter(m=>!ids.has(m.id)));}
      box.innerHTML=chatMessagesHtml(update.data.messages,id);box.scrollTop=nearBottom?box.scrollHeight:top;
    }
    if(update.changed.has('context')&&$('#chatOverview'))$('#chatOverview').textContent=update.data.context.overview||'暂无概览，收到新消息后会持续更新。';
    if(update.changed.has('memories')&&$('#chatMemories'))$('#chatMemories').innerHTML=update.data.memories.map(m=>`<div class="memory-item"><span>${esc(m.content)}</span><button data-forget="${m.id}">移除</button></div>`).join('')||'<p class="subtle">暂无有效记忆</p>';
  }finally{state.chatRefreshing=false;}
}
function chatMessagesHtml(messages,id){
  const h=state.chatHistory?.id===id?state.chatHistory:null,rows=[...new Map([...(h?.items||[]),...messages].map(m=>[m.id,m])).values()].sort((a,b)=>(a.create_time||0)-(b.create_time||0)||a.id-b.id);
  return (h?.before?`<div class="history-more"><button data-history ${h.busy?'disabled':''}>${h.busy?'加载中…':'更早的消息'}</button></div>`:'')+messagesHtml(rows,id);
}
async function loadEarlierMessages(){
  const h=state.chatHistory;if(!h||!h.before||h.busy||state.chat!==h.id||state.view!=='conversations')return;
  h.busy=true;
  try{
    const data=await api(`/api/conversations/${encodeURIComponent(h.id)}/history?before_id=${h.before}`);
    if(state.chatHistory!==h||state.chat!==h.id||state.view!=='conversations')return;
    const box=$('#conversationMessages'),top=box.scrollTop,height=box.scrollHeight;
    h.items.push(...data.messages);h.before=data.next_before_id;h.busy=false;
    box.innerHTML=chatMessagesHtml(state.chatData.messages,h.id);box.scrollTop=box.scrollHeight-height+top;
  }catch(e){toast(e.message);}finally{h.busy=false;}
}
function editorParts(editor){
  const parts=[];const addText=text=>{if(!text)return;if(parts.at(-1)?.type==='text')parts.at(-1).text+=text;else parts.push({type:'text',text});};
  function walk(node){
    if(node.nodeType===Node.TEXT_NODE){addText(node.textContent);return;}
    if(node.nodeType!==Node.ELEMENT_NODE)return;
    if(node.matches('button'))return;
    if(node.matches('img[data-upload-id]')){parts.push({type:'image',upload_id:node.dataset.uploadId});return;}
    if(node.tagName==='BR'){addText('\n');return;}
    for(const child of node.childNodes)walk(child);
    if(['DIV','P','FIGURE'].includes(node.tagName)&&node!==editor)addText('\n');
  }
  for(const child of editor.childNodes)walk(child);
  if(parts.at(-1)?.type==='text')parts.at(-1).text=parts.at(-1).text.replace(/\n+$/,'');
  return parts.filter(p=>p.type==='image'||p.text);
}
function editorText(editor){return editorParts(editor).filter(p=>p.type==='text').map(p=>p.text).join('');}
function messagesHtml(messages,id){return messages.map(m=>{
  const parts=m.display_parts||[];
  const body=parts.length?parts.map((p,index)=>p.type==='image'?`<img loading="lazy" class="message-image ${p.sticker?'sticker-image':''}" src="/api/conversations/${encodeURIComponent(id)}/media/${encodeURIComponent(m.msg_id)}/${index}" alt="${p.sticker?'表情包':'图片'}" ${p.display_width?`style="width:${Math.min(600,Math.max(24,Number(p.display_width)))}px"`:''}>`:esc(p.text)).join(''):esc(displayContent(m.content));
  return `<div class="message ${m.direction==='out'?'out':''}"><div class="message-meta">${esc(m.sender_name||'我')} · ${esc(timeLabel(String(m.create_time)))}</div><div class="message-body">${body}</div></div>`;
}).join('')||'<p class="subtle">暂无消息</p>';}
const groupReplyNames={off:'不回复',all:'自动回复',at_me:'仅 @我',at_all:'仅 @全体'};
const privateReplyNames={auto:'自动回复',draft:'仅草稿',off:'不回复'};
function replyOptions(p){
  const group=p.kind==='group',names=group?groupReplyNames:privateReplyNames;
  const inherited=state.data.status[group?'default_group_reply':'default_private_reply']||'off';
  return {inherit:`跟随默认（${names[inherited]}）`,...names};
}
function replyPolicySelect(p,form=false){
  const value=p.kind==='group'?(p.group_reply_policy||'inherit'):(p.mode||'inherit');
  return `<select class="reply-policy" ${form?`name="${p.kind==='group'?'group_reply_policy':'mode'}"`:`data-reply-policy="${esc(p.key)}"`} aria-label="${esc(p.name)}回复方式">${Object.entries(replyOptions(p)).map(([v,label])=>`<option value="${v}" ${v===value?'selected':''}>${esc(label)}</option>`).join('')}</select>`;
}
function personaForm(p){return `<form class="form-grid persona-form" data-key="${esc(p.key)}"><label>角色 / 关系<input name="role" value="${esc(p.role)}" placeholder="例如：产品同事、项目负责人、客户"></label><label>你对这个人或群的了解<textarea name="description" rows="3" placeholder="负责什么、关注什么、沟通习惯、需要记住的背景">${esc(p.description)}</textarea></label><label>回复口吻<textarea name="reply_style" rows="2" placeholder="例如：直奔主题，少用表情；遇到排期先留待办">${esc(p.reply_style)}</textarea></label><label>回复方式${replyPolicySelect(p,true)}</label><div><button class="button primary" type="submit">保存角色说明</button></div></form>`;}
function renderPersonas(){
  $('#mainView').innerHTML=`<div class="card paged-card" data-collection="personas"><div class="card-head"><h2>联系人与群的角色档案</h2></div>${listToolbar('personas')}${listBody('personas')}</div>`;
  return mountList('personas');
}
function renderActivity(){
  $('#mainView').innerHTML=`<div class="card paged-card" data-collection="actions"><div class="card-head"><h2>发送与执行记录</h2></div>${listToolbar('actions')}${listBody('actions')}</div>`;
  return mountList('actions');
}
async function renderSettings(){
  const version=state.settingsRenderVersion=(state.settingsRenderVersion||0)+1;
  try{
    const info=await api('/api/settings');
    if(state.view!=='settings'||version!==state.settingsRenderVersion)return;
    state.settingsInfo=info;state.settingsGroup ||= 'general';state.settingsDrafts ||= {};
    renderSettingsPanel();
  }catch(e){if(state.view==='settings')$('#mainView').innerHTML=`<div class="card">${empty('设置加载失败')}<p class="section-pad danger-text">${esc(e.message)}</p></div>`;}
}
function settingsField(field,info,draft){
  const value=Object.hasOwn(draft,field.key)?draft[field.key]:info.values[field.key];
  const attrs=`id="setting-${esc(field.key)}" name="${esc(field.key)}" data-setting-key="${esc(field.key)}"${field.description?` aria-describedby="setting-help-${esc(field.key)}"`:''}`;
  let input;
  if(field.kind==='boolean')input=`<input class="settings-switch" type="checkbox" role="switch" ${attrs} ${value?'checked':''}>`;
  else if(field.kind==='textarea')input=`<textarea ${attrs} rows="7" maxlength="${field.maximum}">${esc(value)}</textarea>`;
  else if(field.kind==='select')input=`<select ${attrs}>${field.choices.map(c=>`<option value="${esc(c)}" ${c===value?'selected':''}>${esc(field.choice_labels?.[c]||c)}</option>`).join('')}</select>`;
  else if(field.kind==='text')input=`<input ${attrs} value="${esc(value)}" maxlength="${field.maximum}" placeholder="${field.key==='codex_model'?'默认模型':field.key==='reply_suffix'?'留空不添加':''}">`;
  else {const scale=field.scale||1;input=`<div class="settings-number"><input ${attrs} type="number" min="${field.minimum/scale}" max="${field.maximum/scale}" step="${field.step/scale}" required value="${Number((value/scale).toFixed(4))}"><span>${esc(field.unit)}</span></div>`;}
  return `<div class="settings-field${field.kind==='textarea'?' settings-field-wide':''}"><div class="settings-caption"><label class="settings-label" for="setting-${esc(field.key)}">${esc(field.label)}</label>${field.description?`<p class="settings-help" id="setting-help-${esc(field.key)}">${esc(field.description)}</p>`:''}</div>${input}</div>`;
}
function pathBrowseButton(id,label,text='浏览…'){
  const available=state.settingsInfo.path_picker?.available;
  return `<button id="${id}" type="button" class="button secondary" data-pick-path aria-label="${label}" ${state.pathPicking||!available?'disabled':''} ${available?'':'title="当前环境请手动填写路径"'}>${state.pathPicking?'选择中…':text}</button>`;
}
function pathControl(input,buttons){return `<div class="settings-path">${input}<div class="path-buttons">${buttons}</div></div>`;}
async function pickLocalPath(kind,initial){
  if(state.pathPicking)return null;
  state.pathPicking=true;
  document.querySelectorAll('[data-pick-path]').forEach(button=>{button.disabled=true;});
  try{return (await api('/api/settings/path-picker',mutation('POST',{kind,initial}))).path;}
  catch(e){toast(e.message);return null;}
  finally{state.pathPicking=false;}
}
function renderModelConnection(){
  const info=state.settingsInfo.models;if(!info)return '';
  state.modelSelection ||= {...info.active};state.modelDrafts ||= {};
  const selection=state.modelSelection,c=WorkhubModels.config(info,selection,state.modelDrafts),presets=info.presets[c.kind];
  const row=(label,control)=>`<div class="settings-field"><label class="settings-label">${label}</label>${control}</div>`;
  const select=(key,options,value)=>`<select data-model-key="${key}" aria-label="${key==='kind'?'接入方式':key==='provider'?'工具或服务商':'推理强度'}">${Object.entries(options).map(([key,label])=>`<option value="${esc(key)}" ${key===value?'selected':''}>${esc(label)}</option>`).join('')}</select>`;
  const input=(key,value,placeholder='',type='text')=>`<input data-model-key="${key}" aria-label="${esc({path:'程序路径',base_url:'接口地址',api_key:'API Key',model:'模型名称'}[key])}" type="${type}" value="${esc(value||'')}" placeholder="${esc(placeholder)}" ${key==='api_key'?'autocomplete="new-password"':''}>`;
  return `<form id="modelConnectionForm"><fieldset class="model-controls" ${state.modelBusy?'disabled':''}>${row('接入方式',select('kind',{cli:'CLI（本地工具）',api:'API（模型接口）'},c.kind))}${row(c.kind==='cli'?'本地工具':'服务商',select('provider',Object.fromEntries(Object.entries(presets).map(([key,p])=>[key,p.label])),c.provider))}${c.kind==='cli'?row('程序路径',pathControl(input('path',c.path,'自动检测，可填写完整程序路径'),pathBrowseButton('browseModelPath','选择 CLI 程序'))):row('接口地址',input('base_url',c.base_url,'服务商提供的 API 基础地址'))+row('API Key',`<div>${input('api_key',c.api_key,c.key_set?'已设置，留空保留':'填写 API Key','password')}${c.key_set?`<label class="model-clear"><input data-model-key="clear_key" type="checkbox" ${c.clear_key?'checked':''}> 清除已保存密钥</label>`:''}</div>`) }
  ${row('模型',`<div class="model-model-input">${input('model',c.model,c.kind==='cli'?'跟随工具默认':'填写模型 ID 或接入点 ID')}<datalist id="connectionModels">${(state.modelOptions?.[WorkhubModels.id(c.kind,c.provider)]||[]).map(model=>`<option value="${esc(model)}"></option>`).join('')}</datalist></div>`)}${c.kind==='cli'&&c.provider==='codex'?row('推理强度',select('reasoning',info.reasoning,c.reasoning)):''}${c.kind==='api'?row('图片理解',`<input class="settings-switch" data-model-key="vision" aria-label="图片理解" type="checkbox" role="switch" ${c.vision?'checked':''}>`):''}
  <div class="settings-actions"><button type="submit" class="button primary">保存连接</button>${c.kind==='cli'?'<button type="button" class="button secondary" id="modelCheck">检查环境</button>':''}<button type="button" class="button secondary" id="modelTest">测试调用</button>${c.kind==='api'?'<button type="button" class="button secondary" id="modelList">获取模型</button>':''}</div></fieldset><p id="modelResult" class="settings-help" role="status">${esc(state.modelNotice||'')}</p>${c.kind==='cli'&&c.provider!=='codex'?'<p class="settings-help">此 CLI 暂不支持附图；登录状态以测试调用结果为准。</p>':''}</form>`;
}
function bindModelConnection(){
  const form=$('#modelConnectionForm');if(!form||!state.settingsInfo.models)return;
  const info=state.settingsInfo.models;
  if($('#browseModelPath'))$('#browseModelPath').addEventListener('click',async()=>{
    const selection={...state.modelSelection},id=WorkhubModels.id(selection.kind,selection.provider);
    const config=WorkhubModels.config(info,selection,state.modelDrafts);
    const path=await pickLocalPath('program',config.path||'');
    if(path){state.modelDrafts[id] ||= {};state.modelDrafts[id].path=path;}
    if(state.view==='settings')renderSettingsPanel();
  });
  form.addEventListener('input',e=>{
    const key=e.target.dataset.modelKey;if(!key||['kind','provider'].includes(key))return;
    const id=WorkhubModels.id(state.modelSelection.kind,state.modelSelection.provider);state.modelDrafts[id] ||= {};
    state.modelDrafts[id][key]=['vision','clear_key'].includes(key)?e.target.checked:e.target.value;
  });
  form.addEventListener('change',e=>{
    const key=e.target.dataset.modelKey;if(!['kind','provider'].includes(key))return;
    if(key==='kind'){state.modelSelection={kind:e.target.value,provider:e.target.value==='cli'?'codex':'deepseek'};}
    else state.modelSelection={...state.modelSelection,provider:e.target.value};
    state.modelNotice='';renderSettingsPanel();
  });
  const run=async(action)=>{
    if(state.modelBusy)return;
    const selection={...state.modelSelection},c=WorkhubModels.config(info,selection,state.modelDrafts),body=WorkhubModels.payload(c);
    state.modelBusy=action;$('.model-controls').disabled=true;$('#modelResult').textContent=action==='test'?'正在调用模型验证…':'正在处理…';
    try{
      if(action==='save'){
        const models=await api('/api/models/connection',mutation('PUT',body));
        state.settingsInfo.models=models;delete state.modelDrafts[WorkhubModels.id(selection.kind,selection.provider)];
        await loadState(true);state.modelNotice='连接已保存';
      }else{
        const result=await api('/api/models/'+action,mutation('POST',body));
        if(action==='list'){state.modelOptions ||= {};state.modelOptions[WorkhubModels.id(c.kind,c.provider)]=result.models;state.modelNotice=`已获取 ${result.models.length} 个模型，可选择或手动填写`;}
        else state.modelNotice=[result.message,result.login,result.path].filter(Boolean).join(' · ');
      }
    }catch(error){state.modelNotice=error.message;}
    finally{state.modelBusy='';if(state.view==='settings')renderSettingsPanel();}
  };
  form.addEventListener('submit',e=>{e.preventDefault();run('save');});
  if($('#modelCheck'))$('#modelCheck').addEventListener('click',()=>run('check'));$('#modelTest').addEventListener('click',()=>run('test'));
  if($('#modelList'))$('#modelList').addEventListener('click',()=>run('list'));
  const modelInput=form.querySelector?.('[data-model-key="model"]');if(modelInput)modelInput.setAttribute('list','connectionModels');
}
function openConnectionLogin(){
  if($('#connectionLoginDialog'))return;
  const dialog=document.createElement('dialog');dialog.id='connectionLoginDialog';
  dialog.innerHTML=`<div class="dialog-head"><h2>扫码登录</h2><button class="icon-button" type="button" aria-label="关闭">×</button></div><div class="row-flex"><select aria-label="登录区域"><option value="feishu">飞书（国内版）</option><option value="lark">Lark（国际版）</option></select><button class="button primary" type="button" id="loginRefresh">获取二维码</button></div><img alt="登录二维码" hidden><p class="settings-help" role="status">使用对应 App 扫码并确认登录。</p>`;
  document.body.append(dialog);dialog.querySelector('select').value=state.settingsInfo.connection['登录区域'];
  let stopped=false,timer,identity='';
  const notice=message=>{dialog.querySelector('[role="status"]').textContent=message;};
  const refresh=async()=>{
    clearTimeout(timer);$('#loginRefresh').disabled=true;notice('正在获取二维码…');
    const requestId=crypto.randomUUID();identity=requestId;
    try{
      const result=await api('/api/connection/login',mutation('POST',{region:dialog.querySelector('select').value,id:requestId}));
      if(stopped||identity!==requestId)return;const img=dialog.querySelector('img');img.src=result.image;img.hidden=false;notice('等待扫码');timer=setTimeout(poll,2000);
    }catch(e){if(!stopped)notice(e.message);}
    finally{if(!stopped)$('#loginRefresh').disabled=false;}
  };
  const poll=async()=>{
    if(stopped)return;const requestId=identity;
    try{
      const result=await api('/api/connection/login/poll',mutation('POST',{id:identity}));
      if(stopped||identity!==requestId)return;notice(result.message);
      if(result.done){identity='';dialog.querySelector('img').hidden=true;state.settingsInfo=await api('/api/settings');if(state.view==='settings')renderSettingsPanel();}
      else timer=setTimeout(poll,2000);
    }catch(e){if(!stopped)notice(e.message);}
  };
  dialog.querySelector('.icon-button').addEventListener('click',()=>dialog.close());
  dialog.querySelector('select').addEventListener('change',()=>{clearTimeout(timer);api('/api/connection/login/cancel',mutation('POST',{id:identity})).catch(()=>{});identity='';dialog.querySelector('img').hidden=true;notice('点击获取二维码。');});
  $('#loginRefresh').addEventListener('click',refresh);
  dialog.addEventListener('close',()=>{stopped=true;clearTimeout(timer);api('/api/connection/login/cancel',mutation('POST',{id:identity})).catch(()=>{});dialog.remove();});
  dialog.showModal();
}
function renderStartupConnection(){
  const info=state.settingsInfo,c=info.startup;
  if(!c.storage)return '<p class="settings-help" role="status">设置页面已更新，请重启工作台后使用连接与存储设置。</p>';
  state.startupDraft ||= WorkhubSettings.startupDraft(c);
  const d=state.startupDraft,sqlite=d.storage_kind==='sqlite';
  const row=(label,control,help='')=>`<div class="settings-field"><div class="settings-caption"><label class="settings-label">${label}</label>${help?`<p class="settings-help">${esc(help)}</p>`:''}</div>${control}</div>`;
  const input=(key,value,extra='')=>`<input data-startup-key="${key}" aria-label="${{data_dir:'数据目录',sqlite_path:'数据库文件',mysql_host:'数据库主机',mysql_port:'数据库端口',mysql_database:'数据库名',mysql_user:'数据库账号',port:'服务端口',password:'数据库密码'}[key]}" value="${esc(value)}" ${extra}>`;
  const current=info.connection;
  return `<div class="settings-field"><label class="settings-label" for="messagePlatform">消息平台</label><select id="messagePlatform" aria-label="消息平台"><option value="feishu">飞书</option></select></div>
  <div class="settings-field"><span class="settings-label">连接状态</span><div class="connection-status"><span id="settingsConnection">${esc($('#connectionText').textContent)}</span><button type="button" class="button secondary" id="connectionReconnect">重新连接</button></div></div>
  <div class="settings-field"><span class="settings-label">登录区域</span><span>${current['登录区域']==='lark'?'Lark（国际版）':'飞书（国内版）'} <button type="button" class="button secondary" id="connectionLogin">切换登录</button></span></div>
  <form id="startupForm"><fieldset class="model-controls" ${state.startupBusy?'disabled':''}>
  ${row('数据目录',pathControl(input('data_dir',d.data_dir,'required'),pathBrowseButton('browseDataDir','选择数据目录')),c.values.data_dir!==c.current.data_dir?'当前：'+c.current.data_dir:'')}
  ${row('存储方式',`<select id="startupStorageKind" aria-label="存储方式"><option value="sqlite" ${sqlite?'selected':''}>SQLite</option><option value="mysql" ${!sqlite?'selected':''}>MySQL</option></select>`)}
  ${sqlite?row('数据库文件',pathControl(input('sqlite_path',d.sqlite_path,'required placeholder="数据库文件的绝对路径"'),pathBrowseButton('browseDatabase','选择已有数据库文件')),'文件不存在时自动创建。'):
    row('数据库主机',input('mysql_host',d.mysql_host,'required placeholder="localhost 或服务器地址"'))+
    row('数据库端口',input('mysql_port',d.mysql_port,'type="number" min="1" max="65535" step="1" required'))+
    row('数据库名',input('mysql_database',d.mysql_database,'required'))+
    row('数据库账号',input('mysql_user',d.mysql_user,'required autocomplete="off"'))+
    row('数据库密码',input('password',d.password||'',`type="password" autocomplete="new-password" placeholder="${c.password_set?'已设置，留空保留':'填写数据库密码'}"`))}
  ${c.values.storage_url!==c.current.storage_url?`<p class="settings-help">当前数据库：${esc(c.current.storage_url)}</p>`:''}
  ${row('服务端口',input('port',d.port,'type="number" min="1024" max="65535" step="1" required'))}
  <p class="settings-help">下次启动生效。切换目录或数据库不会迁移旧数据；新目录需重新登录并设置模型密钥。</p>
  <div class="settings-actions"><button type="submit" class="button primary">保存启动配置</button><button type="button" class="button secondary" id="startupCheck">测试存储</button>${c.restart_required?'<button type="button" class="button secondary" id="startupUndo">撤销待生效修改</button>':''}</div>
  </fieldset><p class="settings-help" id="startupResult" role="status">${esc(state.startupNotice||'')}</p></form>
  ${c.restart_required?`<div class="startup-pending">有配置等待重启。当前地址：${esc(current['服务地址'])}；下次启动：<a href="http://127.0.0.1:${c.values.port}">http://127.0.0.1:${c.values.port}</a></div>`:''}`;
}
function bindStartupConnection(){
  const form=$('#startupForm');
  const browse=async(kind,key)=>{
    const draft=state.startupDraft,initial=draft[key];
    const path=await pickLocalPath(kind,initial);
    if(path)draft[key]=path;
    if(state.view==='settings')renderSettingsPanel();
  };
  $('#browseDataDir').addEventListener('click',()=>browse('directory','data_dir'));
  if($('#browseDatabase'))$('#browseDatabase').addEventListener('click',()=>browse('database','sqlite_path'));
  form.addEventListener('input',e=>{
    const key=e.target.dataset.startupKey;if(!key)return;
    const d=state.startupDraft;
    d[key]=['port','mysql_port'].includes(key)?Number(e.target.value):e.target.value;
  });
  $('#startupStorageKind').addEventListener('change',e=>{
    state.startupDraft.storage_kind=e.target.value;renderSettingsPanel();
  });
  const run=async(action,payload=state.startupDraft)=>{
    if(state.startupBusy)return;state.startupBusy=true;form.querySelector('fieldset').disabled=true;
    const submitted=WorkhubSettings.startupPayload(payload);
    try{
      const result=await api('/api/settings/startup'+(action==='check'?'/check':action==='reset'?'/reset':''),mutation(action==='save'?'PUT':'POST',submitted));
      if(action==='check'){state.startupNotice=result.message;}
      else {state.settingsInfo.startup=result;state.startupDraft=WorkhubSettings.startupDraft(result);state.startupNotice=result.restart_required?'已保存，下次启动生效。':'已恢复当前配置。';}
    }catch(e){state.startupNotice=e.message;}
    finally{state.startupBusy=false;if(state.view==='settings')renderSettingsPanel();}
  };
  form.addEventListener('submit',e=>{e.preventDefault();run('save');});
  $('#startupCheck').addEventListener('click',()=>run('check'));
  if($('#startupUndo'))$('#startupUndo').addEventListener('click',()=>run('reset'));
  $('#connectionReconnect').addEventListener('click',async()=>{try{await api('/api/connection/reconnect',mutation('POST',{}));toast('正在重新连接');}catch(e){toast(e.message);}});
  $('#connectionLogin').addEventListener('click',openConnectionLogin);
}
function renderSettingsPanel(){
  const info=state.settingsInfo,group=state.settingsGroup,draft=state.settingsDrafts[group]||{};
  const fields=info.fields.filter(f=>f.group===group&&!['completed_task_limit','actions_limit'].includes(f.key)&&!(info.models&&['codex_model','codex_reasoning_effort'].includes(f.key))&&!(f.key==='api_output_tokens'&&(state.modelSelection||info.models?.active)?.kind!=='api'));
  $('#mainView').innerHTML=`<div class="card settings-card"><div class="card-head"><h2>数字人设置</h2></div><nav class="settings-tabs" aria-label="设置分类">${info.groups.map(g=>`<button type="button" data-settings-group="${g.key}" aria-current="${g.key===group?'page':'false'}" class="${g.key===group?'active':''}">${esc(g.label)}${Object.keys(WorkhubSettings.changes(info,g.key,state.settingsDrafts[g.key]||{})).length?' ·':''}</button>`).join('')}</nav><div class="section-pad">${group==='connection'?renderStartupConnection():`${group==='model'?renderModelConnection():''}<form id="settingsForm" class="form-grid">${fields.map(f=>settingsField(f,info,draft)).join('')}<div class="settings-actions"><button class="button primary" id="settingsSave" type="submit" ${state.settingsSaving?'disabled':''}>${state.settingsSaving?'保存中…':'保存修改'}</button><button class="button secondary" id="settingsReset" type="button">恢复默认</button><span class="subtle" id="settingsDirty"></span></div><p class="settings-help" id="settingsResult" role="status"></p></form>`}</div></div>`;
  document.querySelectorAll('[data-settings-group]').forEach(button=>button.addEventListener('click',()=>{state.settingsGroup=button.dataset.settingsGroup;renderSettingsPanel();}));
  if(group==='model')bindModelConnection();
  if(group==='connection'&&info.startup.storage)bindStartupConnection();
  const form=$('#settingsForm');if(!form)return;
  const dirtyLabel=()=>{const count=Object.keys(WorkhubSettings.changes(info,group,state.settingsDrafts[group]||{})).length;$('#settingsDirty').textContent=count?`${count} 项修改尚未保存`:'';};
  dirtyLabel();
  form.addEventListener('input',e=>{
    const key=e.target.dataset.settingKey;if(!key)return;
    const field=fields.find(f=>f.key===key);
    const value=field.kind==='boolean'?e.target.checked:['integer','number'].includes(field.kind)?Number(e.target.value)*(field.scale||1):e.target.value;
    state.settingsDrafts[group] ||= {};state.settingsDrafts[group][key]=value;dirtyLabel();
  });
  $('#settingsReset').addEventListener('click',()=>{state.settingsDrafts[group]=WorkhubSettings.reset({...info,fields},group);renderSettingsPanel();$('#settingsResult').textContent='已恢复默认，保存后生效。';});
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(state.settingsSaving)return;
    const submitted=WorkhubSettings.changes(info,group,state.settingsDrafts[group]||{});
    if(!Object.keys(submitted).length){$('#settingsResult').textContent='没有需要保存的修改。';return;}
    state.settingsSaving=true;$('#settingsSave').disabled=true;$('#settingsSave').textContent='保存中…';
    try{
      await api('/api/settings',mutation('PATCH',submitted));
      state.settingsDrafts[group]=WorkhubSettings.saved(state.settingsDrafts[group]||{},submitted);
      state.settingsInfo=await api('/api/settings');await loadState(true);toast('已保存');
      if(state.view==='settings')renderSettingsPanel();
    }catch(err){if(state.view==='settings'&&$('#settingsResult'))$('#settingsResult').textContent=err.message;toast(err.message);}
    finally{state.settingsSaving=false;if(state.view==='settings'&&$('#settingsSave')){$('#settingsSave').disabled=false;$('#settingsSave').textContent='保存修改';}}
  });
}
function showTask(id){
  const t=listRows('tasks').find(t=>t.id===Number(id));if(!t)return;
  state.task=t;$('#taskTitle').textContent=t.title;$('#taskDetail').textContent=`${t.chat_name||chatName(t.chat_id)} · ${statusNames[t.status]}${t.due_at?' · '+t.due_at:''}\n\n${t.details||'暂无补充说明'}`;$('#taskReply').value=t.draft_reply||'';$('#taskStatus').value=t.status;$('#taskPriority').value=t.priority;$('#taskPolishInstruction').value='';$('#taskPolishHint').textContent='';$('#taskSuffixPreview').textContent=state.data.status.reply_suffix?'发送时附加尾缀：'+state.data.status.reply_suffix:'当前未设置回复尾缀';$('#taskDialog').showModal();
}
async function waitJob(id,onUpdate){
  while(true){const j=await api(`/api/jobs/${id}`);if(j.status==='done')return j.result;if(j.status==='failed')throw Error(j.error);if(onUpdate)onUpdate(j);await new Promise(r=>setTimeout(r,(state.data?.status.job_poll_interval??2)*1000));}
}
function addBubble(text, kind='assistant'){let el=document.createElement('div');el.className=kind+'-bubble';el.textContent=text;$('#controlThread').append(el);$('#controlThread').scrollTop=$('#controlThread').scrollHeight;return el;}
function setAssistantOpen(open){
  $('#assistantPanel').hidden=!open;$('#assistantLauncher').setAttribute('aria-expanded',String(open));
  if(open){$('#assistantUnread').hidden=true;$('#controlText').focus();$('#controlThread').scrollTop=$('#controlThread').scrollHeight;}
  else $('#assistantLauncher').focus();
}
$('#assistantLauncher').addEventListener('click',()=>setAssistantOpen($('#assistantPanel').hidden));
$('#assistantClose').addEventListener('click',()=>setAssistantOpen(false));
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#assistantPanel').hidden&&!document.querySelector('dialog[open]')){e.preventDefault();setAssistantOpen(false);}});
async function command(text){
  addBubble(text,'user');const el=addBubble('正在整理上下文并处理…');el.classList.add('loading');$('#controlSubmit').disabled=true;
  try{const j=await api('/api/control',mutation('POST',{text}));const result=await waitJob(j.job_id);el.classList.remove('loading');el.textContent=result.answer||'已处理。';for(const action of result.actions||[]){const row=document.createElement('div');row.className='control-result'+(action.ok?'':' error');let label={send_message:'发送消息',create_task:'创建待办',complete_task:'完成任务',update_profile:'更新角色'}[action.type]||action.type;row.textContent=action.ok?`${label}：${action.data.status?statusNames[action.data.status]||action.data.status:'已完成'}`:`${label}：${action.error}`;el.append(row);}await loadState(true);}catch(e){el.classList.remove('loading');el.textContent='处理未完成：'+e.message;}finally{$('#controlSubmit').disabled=false;$('#controlThread').scrollTop=$('#controlThread').scrollHeight;if($('#assistantPanel').hidden)$('#assistantUnread').hidden=false;}
}
document.addEventListener('click',async e=>{
  const page=e.target.closest('[data-list-page]');if(page){const key=activeCollection(),p=listState(key);p.page=Number(page.dataset.listPage);p.scrollTop=0;await refreshList(key);return;}
  if(e.target.closest('[data-list-refresh]')){const key=activeCollection();if(key)await refreshList(key);return;}
  const persona=e.target.closest('[data-persona]');if(persona){openListDetail('personas',persona.dataset.persona);return;}
  const action=e.target.closest('[data-action]');if(action){openListDetail('actions',action.dataset.action);return;}
  if(e.target.closest('[data-history]')){await loadEarlierMessages();return;}
  const image=e.target.closest('.message-image');if(image){let viewer=$('#imageViewer');if(!viewer){viewer=document.createElement('dialog');viewer.id='imageViewer';viewer.innerHTML='<button class="icon-button" data-close aria-label="关闭图片">×</button><img alt="图片预览">';document.body.append(viewer);}viewer.querySelector('img').src=image.src;viewer.showModal();return;}
  const nav=e.target.closest('button[data-view]');if(nav){if(state.view!==nav.dataset.view){state.view=nav.dataset.view;renderView();}return;}
  const filter=e.target.closest('[data-filter]');if(filter){state.filter=filter.dataset.filter;const p=listState('tasks');p.page=1;p.status=state.filter;p.loaded=false;p.version++;renderHome();return;}
  const task=e.target.closest('[data-task]');if(task)showTask(task.dataset.task);
  const done=e.target.closest('[data-done]');if(done){const t=listRows('tasks').find(t=>t.id===Number(done.dataset.done));await api(`/api/tasks/${t.id}`,mutation('PATCH',{status:t.status==='done'?'open':'done'}));await loadState(true);}
  const chat=e.target.closest('[data-chat]');if(chat)await selectChat(chat.dataset.chat);
  const close=e.target.closest('[data-close]');if(close)close.closest('dialog').close();
  const forget=e.target.closest('[data-forget]');if(forget){await api(`/api/memories/${forget.dataset.forget}`,{method:'DELETE'});await refreshChatMessages();}
  const cmd=e.target.closest('[data-command]');if(cmd)command(cmd.dataset.command);
  if(e.target.closest('#newTask')){$('#newTaskChatSearch').value='';$('#newTaskChat').innerHTML='<option value="">不关联会话</option>';$('#newTaskDialog').showModal();fillTaskChats('');}
});
document.addEventListener('change',async e=>{
  const select=e.target.closest('[data-reply-policy]');if(!select)return;
  const key=select.dataset.replyPolicy,p=findProfile(key);
  if(!p)return;
  const field=p.kind==='group'?'group_reply_policy':'mode',value=select.value,previous=p[field];select.disabled=true;
  try{
    await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',{[field]:value}));await loadState(true);
    document.querySelectorAll('[data-reply-policy]').forEach(el=>{if(el.dataset.replyPolicy===key){el.value=value;el.options[0].textContent=replyOptions(p).inherit;}});
    const form=document.querySelector(`.persona-form[data-key="${CSS.escape(key)}"]`);if(form?.elements[field])form.elements[field].value=value;
    toast('回复方式已更新');
  }catch(err){select.value=previous;toast(err.message);}finally{select.disabled=false;}
});
document.addEventListener('input',e=>{const form=e.target.closest('.persona-form');if(!form||!e.target.name)return;state.personaDrafts||={};state.personaDrafts[form.dataset.key]||={};state.personaDrafts[form.dataset.key][e.target.name]=e.target.value;});
document.addEventListener('submit',async e=>{
  if(e.target.matches('.persona-form')){e.preventDefault();const key=e.target.dataset.key,fields=Object.fromEntries(new FormData(e.target));try{await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',fields));if(state.personaDrafts?.[key])state.personaDrafts[key]=WorkhubSettings.saved(state.personaDrafts[key],fields);toast('角色说明已保存，下次回复生效');await loadState(true);}catch(e){toast(e.message);}}
});
$('#controlForm').addEventListener('submit',e=>{e.preventDefault();const text=$('#controlText').value.trim();if(!text)return;$('#controlText').value='';command(text);});
let taskChatVersion=0,taskChatTimer;
async function fillTaskChats(query){const version=++taskChatVersion,select=$('#newTaskChat'),chosen=select.value,label=select.selectedOptions?.[0]?.textContent;try{const data=await api('/api/lists/conversations?size=30&query='+encodeURIComponent(query));if(version!==taskChatVersion)return;const items=data.items;if(chosen&&!items.some(c=>c.chat_id===chosen))items.unshift({chat_id:chosen,name:label||chosen});select.innerHTML='<option value="">不关联会话</option>'+items.map(c=>`<option value="${esc(c.chat_id)}">${esc(c.name)}</option>`).join('');select.value=chosen;}catch(e){toast(e.message);}}
$('#newTaskChatSearch').addEventListener('input',e=>{clearTimeout(taskChatTimer);taskChatVersion++;taskChatTimer=setTimeout(()=>fillTaskChats(e.target.value),250);});
$('#controlText').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')$('#controlForm').requestSubmit();});
$('#refresh').addEventListener('click',()=>loadState(true).then(()=>toast('已刷新')).catch(e=>toast(e.message)));
$('#taskDone').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:'done'}));$('#taskDialog').close();await loadState(true);});
$('#taskSave').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:$('#taskStatus').value,priority:$('#taskPriority').value}));await loadState(true);toast('任务状态已保存');});
$('#taskPolish').addEventListener('click',async()=>{
  const taskId=state.task.id, original=$('#taskReply').value;
  const button=$('#taskPolish');button.disabled=true;$('#taskSendButton').disabled=true;
  $('#taskPolishHint').textContent='润色中…';
  try{const j=await api(`/api/tasks/${taskId}/polish`,mutation('POST',{text:original,instruction:$('#taskPolishInstruction').value}));const result=await waitJob(j.job_id);await loadState(true);
    if(state.task?.id===taskId){if($('#taskReply').value===original){$('#taskReply').value=result.reply;$('#taskPolishHint').textContent='草稿已保存';}else{$('#taskPolishHint').textContent='AI 草稿已保存；已保留你正在修改的内容';}}
    toast('AI 草稿已生成，尚未发送');
  }catch(e){$('#taskPolishHint').textContent='润色未完成：'+e.message;toast(e.message);}finally{button.disabled=false;$('#taskSendButton').disabled=false;}
});
$('#taskReplyForm').addEventListener('submit',async e=>{e.preventDefault();let b=e.submitter;b.disabled=true;try{const r=await api(`/api/tasks/${state.task.id}/reply`,mutation('POST',{content:$('#taskReply').value,request_id:crypto.randomUUID()}));toast(statusNames[r.status]||r.status);if(r.status==='sent')$('#taskDialog').close();await loadState(true);}catch(e){toast(e.message);}finally{b.disabled=false;}});
$('#newTaskForm').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/tasks',mutation('POST',{title:$('#newTaskTitle').value,details:$('#newTaskDetails').value,chat_id:$('#newTaskChat').value,priority:'normal'}));$('#newTaskDialog').close();e.target.reset();await loadState(true);toast('待办已添加');}catch(e){toast(e.message);}});
$('#date').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
loadState().catch(e=>toast('连接工作台失败：'+e.message));
api('/api/control/history').then(jobs=>{for(const j of jobs){addBubble(j.input,'user');if(j.status==='done'){const r=JSON.parse(j.result);const summary=(r.actions||[]).map(a=>`${a.type}：${a.ok?(statusNames[a.data?.status]||'已完成'):a.error}`).join('\n');addBubble((r.answer||'已处理')+(summary?'\n\n'+summary:''));}else if(j.status==='failed')addBubble('处理未完成：'+j.error);else addBubble('这条指令正在后台处理，可稍后刷新查看结果。');}}).catch(()=>{});
let pollTimer=null,polling=false;
async function poll(){
  if(polling)return;
  polling=true;
  try{if(!document.hidden){await loadState();await refreshChatMessages();}}catch{}finally{
    polling=false;pollTimer=setTimeout(poll,(state.data?.status.poll_interval??5)*1000);
  }
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden){clearTimeout(pollTimer);poll();}});
pollTimer=setTimeout(poll,(state.data?.status.poll_interval??5)*1000);
