const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'home',filter:'open',data:null,chat:null,task:null,chatDrafts:{},chatPolishing:new Set()};
const viewNames = {home:'工作台',conversations:'会话与记忆',personas:'角色档案',activity:'执行记录',settings:'数字人设置'};
const statusNames = {open:'待处理',in_progress:'进行中',done:'已完成',needs_attention:'需要你决定',pending:'处理中',sent:'已发送',failed:'失败',uncertain:'待核对',auto:'自动回复',draft:'先留草稿',off:'不自动回复'};
async function api(path, options={}) {
  const r = await fetch(path,{...options,headers:{'Content-Type':'application/json','X-Workhub':'1',...options.headers}});
  const data = await r.json();
  if(!r.ok) throw Error(typeof data.detail==='string'?data.detail:'操作失败，请检查输入');
  return data;
}
const mutation = (method, data) => ({method,body:JSON.stringify(data)});
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');setTimeout(()=>$('#toast').classList.remove('show'),3500);}
function timeLabel(ts){if(!ts)return '';return new Date(ts.includes('T')?ts:ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function chatName(id){return state.data.conversations.find(c=>c.chat_id===id)?.name || '未关联会话';}
function humanPauseLabel(c){const seconds=Math.ceil(((c?.human_pause_until||0)*1000-Date.now())/1000);return seconds>0?`人工接管中 · 自动回复暂停 ${seconds} 秒`:'人工接管暂停已结束';}
function displayContent(text){const v=String(text||'');if(v.startsWith('[图片]'))return '图片';if(v.startsWith('[表情包]'))return '表情包';return v;}
function empty(title, sub='新的工作事项会出现在这里。'){return `<div class="empty"><div class="empty-icon">✓</div><strong>${esc(title)}</strong></div>`;}
function badge(status){return `<span class="badge ${esc(status)}">${esc(statusNames[status]||status)}</span>`;}
async function loadState(render=true){
  state.data=await api('/api/state');
  const {status,tasks,conversations,personas,actions}=state.data;
  $('#connectionDot').className=status.connection==='connected'?'connected':'';
  $('#connectionText').textContent={connected:'飞书已连接',starting:'正在启动',connecting:'正在连接',reconnecting:'正在重连',error:'连接异常'}[status.connection]||status.connection;
  $('#toggleAutomation').textContent=status.automation_enabled?'暂停自动回复':'恢复自动回复';
  $('.tiny-badge').textContent=status.active_calls?`处理 ${status.active_calls} 项中`:status.connection==='connected'?'已就绪':'待连接';
  const cards=[['等待处理',tasks.filter(t=>t.status==='open'||t.status==='in_progress').length,'需要继续推进的工作','☷'],['需要你决定',tasks.filter(t=>t.status==='needs_attention').length,'异常与需要判断的事项','◇'],['建立联系',personas.filter(p=>p.kind==='person').length,`${conversations.filter(c=>c.kind==='group').length} 个群聊也在这里`,'♧'],['已完成',tasks.filter(t=>t.status==='done').length,`${actions.filter(a=>a.status==='sent').length} 条消息已发出`,'↗']];
  $('#stats').innerHTML=cards.map(([label,value,note,icon])=>`<div class="stat"><div class="stat-label">${label}</div><span class="stat-icon">${icon}</span><div class="stat-value">${value.toString().padStart(2,'0')}</div></div>`).join('');
  if($('#humanPauseStatus')){const c=conversations.find(c=>c.chat_id===state.chat);$('#humanPauseStatus').textContent=c?.human_pause_until?' · '+humanPauseLabel(c):'';}
  document.querySelectorAll('[data-auto-switch]').forEach(el=>{const p=personas.find(p=>p.key===el.dataset.autoSwitch);if(p&&!el.disabled)el.checked=p.mode==='auto';});
  document.querySelectorAll('[data-auto-status]').forEach(el=>{const p=personas.find(p=>p.key===el.dataset.autoStatus);if(p)el.textContent=p.mode==='auto'?'开启':p.mode==='draft'?'仅草稿':'关闭';});
  if(render) renderView();
}
function renderView(){
  if(!state.data)return;
  document.body.dataset.view=state.view;
  if(state.view!=='conversations')document.body.dataset.chatOpen='false';
  $('.nav.active')?.classList.remove('active');$(`.nav[data-view="${state.view}"]`).classList.add('active');
  $('#breadcrumb').textContent=viewNames[state.view];
  $('#pageTitle').textContent={home:'工作台',conversations:'会话与记忆',personas:'联系人与角色',activity:'执行记录',settings:'数字人设置'}[state.view];
  ({home:renderHome,conversations:renderConversations,personas:renderPersonas,activity:renderActivity,settings:renderSettings}[state.view])();
}
function taskRow(t){return `<div class="task-row"><button class="task-dot ${t.status==='done'?'done':''}" data-done="${t.id}" aria-label="标记完成">${t.status==='done'?'✓':''}</button><div class="task-content"><div class="task-title" data-task="${t.id}">${esc(t.title)}</div><div class="task-meta"><span>${esc(chatName(t.chat_id))}</span><span>·</span><span>${esc(t.due_at||timeLabel(t.created_at))}</span>${t.priority==='high'?'<span class="badge high">优先处理</span>':''}${t.status==='needs_attention'?badge(t.status):''}</div></div><button class="button ghost" data-task="${t.id}">处理 →</button></div>`;}
function renderHome(){
  let tasks=state.data.tasks;
  const filtered=tasks.filter(t=>state.filter==='all'||(state.filter==='open'?['open','needs_attention'].includes(t.status):t.status===state.filter));
  $('#mainView').innerHTML=`<div class="card"><div class="card-head"><div><h2>我的待办 <span class="subtle">${tasks.filter(t=>t.status!=='done').length} 项</span></h2></div><button class="button secondary" id="newTask">＋ 添加待办</button></div><div class="tabs">${[['open','待处理'],['in_progress','进行中'],['done','已完成'],['all','全部']].map(([v,n])=>`<button class="tab ${v===state.filter?'active':''}" data-filter="${v}">${n}</button>`).join('')}</div>${filtered.length?filtered.map(taskRow).join(''):empty('暂无待办','明确的工作请求会自动提取到这里。')}</div><div class="card"><div class="card-head"><div><h2>最近的联系</h2></div><button class="button ghost" data-view="conversations">全部会话 →</button></div>${state.data.conversations.slice(0,4).map(conversationRow).join('')||empty('等待新的联系')}</div>${state.data.status.error?`<div class="hint danger-text">${esc(state.data.status.error)}</div>`:''}`;
}
function conversationRow(c){return `<button type="button" class="conversation-row ${state.chat===c.chat_id?'selected':''}" data-chat="${esc(c.chat_id)}"><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div><h3>${esc(c.name)}${c.kind==='group'?'<span class="badge group">群聊</span>':''}</h3><p>${esc(displayContent(c.last_message)||c.overview||'暂无消息')}</p></div>${c.open_tasks?`<span class="badge">${c.open_tasks} 项待办</span>`:''}</button>`;}
function renderConversations(){
  state.chat=null;document.body.dataset.chatOpen='false';
  $('#mainView').innerHTML=`<div class="card chat-list-card"><div class="card-head"><div><h2>消息</h2></div><span class="subtle">${state.data.conversations.length} 个会话</span></div><div class="list-search"><input id="conversationSearch" aria-label="搜索联系人或群聊" placeholder="搜索联系人或群聊"></div><div id="conversationList">${state.data.conversations.map(conversationRow).join('')||empty('暂无会话')}</div></div>`;
  $('#conversationSearch').addEventListener('input',e=>{const matches=state.data.conversations.filter(c=>c.name.includes(e.target.value)||(c.overview||'').includes(e.target.value));$('#conversationList').innerHTML=matches.map(conversationRow).join('')||empty('没有找到匹配的会话','试试其他姓名或关键词。');});
}
async function selectChat(id){
  if(state.view!=='conversations'){state.view='conversations';renderView();}
  state.chat=id;
  const c=state.data.conversations.find(c=>c.chat_id===id);if(!c)return;
  const d=await api(`/api/conversations/${encodeURIComponent(id)}`);
  if(state.chat!==id||state.view!=='conversations')return;
  document.body.dataset.chatOpen='true';
  $('#mainView').innerHTML='<div id="conversationDetail"></div>';
  $('#conversationDetail').innerHTML=`<div class="card wechat-room"><div class="chat-room-head"><button class="icon-button" id="chatBack" aria-label="返回会话列表" title="返回会话列表"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg></button><span class="avatar">${esc(c.kind==='group'?'群':c.name.slice(0,1))}</span><div class="chat-room-title"><h2>${esc(c.name)}</h2><p>${c.kind==='group'?'群聊':'私聊'}<span id="humanPauseStatus">${c.human_pause_until?' · '+esc(humanPauseLabel(c)):''}</span></p></div>${c.persona?autoReplySwitch(c.persona):''}</div><details class="chat-context"><summary>会话概览与记忆 <span>查看背景</span></summary><div class="chat-context-body"><div class="row-flex"><strong>会话概览</strong><button class="button ghost" id="refreshOverview">更新概览</button></div><div class="overview">${esc(d.context.overview||'暂无概览，收到新消息后会持续更新。')}</div><div class="label-heading">有效记忆</div>${d.memories.length?d.memories.map(m=>`<div class="memory-item"><span>${esc(m.content)}</span><button data-forget="${m.id}">移除</button></div>`).join(''):'<p class="subtle">暂无有效记忆</p>'}</div></details><div class="messages" id="conversationMessages">${messagesHtml(d.messages,id)}</div><div class="chat-compose-area"><form class="inline-compose" id="conversationSend"><div id="conversationReply" contenteditable="true" role="textbox" aria-multiline="true" aria-label="回复当前会话" data-placeholder="输入消息…"></div><input id="chatImageInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden><div class="chat-compose-toolbar"><button type="button" class="button secondary" id="chatAttachImage">＋ 图片</button><span class="subtle">Ctrl + Enter 发送</span><button type="button" class="button secondary" id="conversationPolish">✳ AI 润色</button><button class="button primary" id="conversationSendButton">发送</button></div></form><p class="subtle" id="conversationPolishHint"></p></div></div>`;
  $('#chatBack').addEventListener('click',()=>{renderConversations();window.scrollTo(0,0);$('#conversationSearch').focus({preventScroll:true});});
  state.chatMessageSignature=JSON.stringify(d.messages);
  window.scrollTo(0,0);
  $('#conversationMessages').scrollTop=$('#conversationMessages').scrollHeight;
  const replyInput=$('#conversationReply');
  state.chatRichDrafts ||= {};state.chatUploading ||= new Set();
  if(state.chatRichDrafts[id])replyInput.innerHTML=state.chatRichDrafts[id];else replyInput.textContent=state.chatDrafts[id]||'';
  const saveDraft=()=>{state.chatRichDrafts[id]=replyInput.innerHTML;state.chatDrafts[id]=editorText(replyInput);};
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
    if(existing+files.length>6){toast('最多 6 张图片');return;}
    state.chatUploading.add(id);updateBusy();
    try{for(const file of files){
      if(!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)||file.size>10*1024*1024){throw Error('仅支持 10 MB 以内的 PNG、JPG、GIF、WebP');}
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
    try{const r=await api(`/api/conversations/${id}/send-rich`,mutation('POST',{parts,request_id:crypto.randomUUID()}));toast(statusNames[r.status]||r.status);
      if(r.status==='sent'&&state.chatRichDrafts[id]===original){state.chatDrafts[id]='';state.chatRichDrafts[id]='';if(replyInput.isConnected)replyInput.innerHTML='';}
      await loadState(false);await refreshChatMessages();
    }catch(e){toast(e.message);}finally{b.disabled=false;}
  });
  $('#refreshOverview').addEventListener('click',async e=>{const b=e.target;b.disabled=true;b.textContent='分析中…';try{const j=await api(`/api/conversations/${id}/overview`,{method:'POST'});await waitJob(j.job_id);await loadState(false);if(state.chat===id&&state.view==='conversations')await selectChat(id);toast('会话概览已更新');}catch(e){toast(e.message);b.disabled=false;b.textContent='更新概览';}});
}
async function refreshChatMessages(){
  const id=state.chat;if(state.view!=='conversations'||!id||state.chatRefreshing||!$('#conversationMessages'))return;
  state.chatRefreshing=true;
  try{const d=await api(`/api/conversations/${encodeURIComponent(id)}`);if(state.view!=='conversations'||state.chat!==id||!$('#conversationMessages'))return;
    const signature=JSON.stringify(d.messages);if(signature===state.chatMessageSignature)return;
    const box=$('#conversationMessages'),top=box.scrollTop,nearBottom=box.scrollHeight-box.clientHeight-top<60;
    box.innerHTML=messagesHtml(d.messages,id);state.chatMessageSignature=signature;
    box.scrollTop=nearBottom?box.scrollHeight:top;
  }finally{state.chatRefreshing=false;}
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
const groupReplyNames={off:'不自动回复',all:'闲聊模式',at_me:'仅回复 @我',at_all:'仅回复 @全体'};
function groupReplySelect(p,form=false){return `<select ${form?'name="group_reply_policy"':`data-group-policy="${esc(p.key)}"`} aria-label="群聊回复方式">${Object.entries(groupReplyNames).map(([value,label])=>`<option value="${value}" ${(p.group_reply_policy||'off')===value?'selected':''}>${label}</option>`).join('')}</select>`;}
function autoReplySwitch(p){if(p.kind==='group')return groupReplySelect(p);return `<label class="auto-reply-switch"><input type="checkbox" role="switch" aria-label="${esc(p.name)} 自动回复" data-auto-switch="${esc(p.key)}" ${p.mode==='auto'?'checked':''}><span>自动回复</span><strong data-auto-status="${esc(p.key)}">${p.mode==='auto'?'开启':p.mode==='draft'?'仅草稿':'关闭'}</strong></label>`;}
function personaForm(p){return `<form class="form-grid persona-form" data-key="${esc(p.key)}"><label>角色 / 关系<input name="role" value="${esc(p.role)}" placeholder="例如：产品同事、项目负责人、客户"></label><label>你对这个人或群的了解<textarea name="description" rows="3" placeholder="负责什么、关注什么、沟通习惯、需要记住的背景">${esc(p.description)}</textarea></label><label>回复口吻<textarea name="reply_style" rows="2" placeholder="例如：直奔主题，少用表情；遇到排期先留待办">${esc(p.reply_style)}</textarea></label><label>自动处理方式${p.kind==='group'?groupReplySelect(p,true):`<select name="mode">${['auto','draft','off'].map(v=>`<option value="${v}" ${p.mode===v?'selected':''}>${statusNames[v]}</option>`).join('')}</select>`}</label><div><button class="button primary" type="submit">保存角色说明</button></div></form>`;}
function renderPersonas(){
  $('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>联系人与群的角色档案</h2><span class="subtle">${state.data.personas.length} 份</span></div>${state.data.personas.map(p=>`<div class="persona-row"><span class="avatar">${esc(p.kind==='group'?'群':p.name.slice(0,1))}</span><div><div class="row-flex"><h3>${esc(p.name)}</h3>${autoReplySwitch(p)}</div><p>${esc(p.role||'未设置角色')}</p><p>${esc(p.description||'')}</p><details class="form-inline"><summary class="subtle">编辑角色说明</summary><div class="section-pad">${personaForm(p)}</div></details></div></div>`).join('')||empty('暂无联系人','联系人会从收到的消息中自动建立。')}</div>`;
}
function renderActivity(){
  $('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>发送与执行记录</h2></div>${state.data.actions.map(a=>`<div class="activity-row"><div class="meta"><span>发给 ${esc(chatName(a.chat_id))} · ${esc(timeLabel(a.created_at))}</span>${badge(a.status)}</div><p>${esc(a.content)}</p>${a.error?`<p class="danger-text">${esc(a.error)}</p>`:''}</div>`).join('')||empty('还没有执行记录','主动发送和自动回复都会留在这里。')}</div>`;
}
function renderSettings(){
  $('#mainView').innerHTML=`<div class="card"><div class="card-head"><h2>回复设置</h2></div><div class="section-pad"><form id="settingsForm" class="form-grid"><label>你的角色、工作习惯与回复原则<textarea id="ownRules" rows="10">${esc(state.data.status.own_rules)}</textarea></label><label>回复尾缀<input id="replySuffix" maxlength="200" value="${esc(state.data.status.reply_suffix)}" placeholder="例如：reply by Bot"></label><label>消息停顿时间（秒）<input id="batchDelay" type="number" min="0.1" max="30" step="0.1" required value="${esc(state.data.status.batch_delay??1.5)}"></label><label>最长等待时间（秒）<input id="batchMaxWait" type="number" min="0.1" max="60" step="0.1" required value="${esc(state.data.status.batch_max_wait??5)}"></label><div><button class="button primary">保存设置</button></div></form><div class="label-heading">当前状态</div><p class="subtle">${esc(state.data.status.rules)}<br>模型：${esc(state.data.status.model||'默认模型')}<br>排队中的消息：${state.data.status.queued_messages}<br>自动回复：${state.data.status.automation_enabled?'运行中':'已暂停，仅生成草稿'}</p></div></div>`;
  $('#settingsForm').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/settings',mutation('PATCH',{own_rules:$('#ownRules').value,reply_suffix:$('#replySuffix').value,batch_delay:Number($('#batchDelay').value),batch_max_wait:Number($('#batchMaxWait').value)}));toast('数字人设置已保存');await loadState(false);}catch(e){toast(e.message);}});
}
function showTask(id){
  const t=state.data.tasks.find(t=>t.id===Number(id));if(!t)return;
  state.task=t;$('#taskTitle').textContent=t.title;$('#taskDetail').textContent=`${chatName(t.chat_id)} · ${statusNames[t.status]}${t.due_at?' · '+t.due_at:''}\n\n${t.details||'暂无补充说明'}`;$('#taskReply').value=t.draft_reply||'';$('#taskStatus').value=t.status;$('#taskPriority').value=t.priority;$('#taskPolishInstruction').value='';$('#taskPolishHint').textContent='';$('#taskSuffixPreview').textContent=state.data.status.reply_suffix?'发送时附加尾缀：'+state.data.status.reply_suffix:'当前未设置回复尾缀';$('#taskDialog').showModal();
}
async function waitJob(id,onUpdate){
  while(true){const j=await api(`/api/jobs/${id}`);if(j.status==='done')return j.result;if(j.status==='failed')throw Error(j.error);if(onUpdate)onUpdate(j);await new Promise(r=>setTimeout(r,1500));}
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
  try{const j=await api('/api/control',mutation('POST',{text}));const result=await waitJob(j.job_id);el.classList.remove('loading');el.textContent=result.answer||'已处理。';for(const action of result.actions||[]){const row=document.createElement('div');row.className='control-result'+(action.ok?'':' error');let label={send_message:'发送消息',create_task:'创建待办',complete_task:'完成任务',update_profile:'更新角色'}[action.type]||action.type;row.textContent=action.ok?`${label}：${action.data.status?statusNames[action.data.status]||action.data.status:'已完成'}`:`${label}：${action.error}`;el.append(row);}await loadState(state.view==='home'||state.view==='activity');}catch(e){el.classList.remove('loading');el.textContent='处理未完成：'+e.message;}finally{$('#controlSubmit').disabled=false;$('#controlThread').scrollTop=$('#controlThread').scrollHeight;if($('#assistantPanel').hidden)$('#assistantUnread').hidden=false;}
}
document.addEventListener('click',async e=>{
  const image=e.target.closest('.message-image');if(image){let viewer=$('#imageViewer');if(!viewer){viewer=document.createElement('dialog');viewer.id='imageViewer';viewer.innerHTML='<button class="icon-button" data-close aria-label="关闭图片">×</button><img alt="图片预览">';document.body.append(viewer);}viewer.querySelector('img').src=image.src;viewer.showModal();return;}
  const nav=e.target.closest('button[data-view]');if(nav){if(state.view!==nav.dataset.view){state.view=nav.dataset.view;renderView();}return;}
  const filter=e.target.closest('[data-filter]');if(filter){state.filter=filter.dataset.filter;renderHome();}
  const task=e.target.closest('[data-task]');if(task)showTask(task.dataset.task);
  const done=e.target.closest('[data-done]');if(done){const t=state.data.tasks.find(t=>t.id===Number(done.dataset.done));await api(`/api/tasks/${t.id}`,mutation('PATCH',{status:t.status==='done'?'open':'done'}));await loadState();}
  const chat=e.target.closest('[data-chat]');if(chat)await selectChat(chat.dataset.chat);
  const close=e.target.closest('[data-close]');if(close)close.closest('dialog').close();
  const forget=e.target.closest('[data-forget]');if(forget){await api(`/api/memories/${forget.dataset.forget}`,{method:'DELETE'});await selectChat(state.chat);}
  const cmd=e.target.closest('[data-command]');if(cmd)command(cmd.dataset.command);
  if(e.target.closest('#newTask')){$('#newTaskChat').innerHTML='<option value="">不关联会话</option>'+state.data.conversations.map(c=>`<option value="${esc(c.chat_id)}">${esc(c.name)}</option>`).join('');$('#newTaskDialog').showModal();}
});
document.addEventListener('change',async e=>{
  const group=e.target.closest('[data-group-policy]');
  if(group){const key=group.dataset.groupPolicy, policy=group.value;group.disabled=true;
    try{await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',{group_reply_policy:policy}));await loadState(false);
      document.querySelectorAll('[data-group-policy]').forEach(el=>{if(el.dataset.groupPolicy===key)el.value=policy;});
      const form=document.querySelector(`.persona-form[data-key="${CSS.escape(key)}"]`);if(form?.elements.group_reply_policy)form.elements.group_reply_policy.value=policy;
      toast(groupReplyNames[policy]);
    }catch(err){group.value=state.data.personas.find(p=>p.key===key)?.group_reply_policy||'off';toast(err.message);}finally{group.disabled=false;}return;
  }
  const toggle=e.target.closest('[data-auto-switch]');if(!toggle)return;
  const key=toggle.dataset.autoSwitch, mode=toggle.checked?'auto':'off';toggle.disabled=true;
  try{await api(`/api/personas/${encodeURIComponent(key)}`,mutation('PATCH',{mode}));await loadState(false);
    document.querySelectorAll('[data-auto-switch]').forEach(el=>{if(el.dataset.autoSwitch===key)el.checked=mode==='auto';});
    document.querySelectorAll('[data-auto-status]').forEach(el=>{if(el.dataset.autoStatus===key)el.textContent=mode==='auto'?'开启':'关闭';});
    document.querySelectorAll('.persona-form').forEach(form=>{if(form.dataset.key===key)form.elements.mode.value=mode;});
    toast(mode==='auto'?'已开启此联系人的自动回复':'已关闭此联系人的自动回复，记忆和待办仍会更新');
  }catch(err){toggle.checked=!toggle.checked;toast(err.message);}finally{toggle.disabled=false;}
});
document.addEventListener('submit',async e=>{
  if(e.target.matches('.persona-form')){e.preventDefault();const fields=Object.fromEntries(new FormData(e.target));try{await api(`/api/personas/${encodeURIComponent(e.target.dataset.key)}`,mutation('PATCH',fields));toast('角色说明已保存，下次回复生效');await loadState(false);}catch(e){toast(e.message);}}
});
$('#controlForm').addEventListener('submit',e=>{e.preventDefault();const text=$('#controlText').value.trim();if(!text)return;$('#controlText').value='';command(text);});
$('#controlText').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')$('#controlForm').requestSubmit();});
$('#refresh').addEventListener('click',()=>loadState().then(()=>toast('已刷新')).catch(e=>toast(e.message)));
$('#toggleAutomation').addEventListener('click',async()=>{await api('/api/settings',mutation('PATCH',{automation_enabled:!state.data.status.automation_enabled}));await loadState();toast(state.data.status.automation_enabled?'自动回复已恢复':'已暂停自动发送，新回复保留为草稿');});
$('#taskDone').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:'done'}));$('#taskDialog').close();await loadState();});
$('#taskSave').addEventListener('click',async()=>{await api(`/api/tasks/${state.task.id}`,mutation('PATCH',{status:$('#taskStatus').value,priority:$('#taskPriority').value}));await loadState(false);toast('任务状态已保存');});
$('#taskPolish').addEventListener('click',async()=>{
  const taskId=state.task.id, original=$('#taskReply').value;
  const button=$('#taskPolish');button.disabled=true;$('#taskSendButton').disabled=true;
  $('#taskPolishHint').textContent='润色中…';
  try{const j=await api(`/api/tasks/${taskId}/polish`,mutation('POST',{text:original,instruction:$('#taskPolishInstruction').value}));const result=await waitJob(j.job_id);await loadState(false);
    if(state.task?.id===taskId){if($('#taskReply').value===original){$('#taskReply').value=result.reply;$('#taskPolishHint').textContent='草稿已保存';}else{$('#taskPolishHint').textContent='AI 草稿已保存；已保留你正在修改的内容';}}
    toast('AI 草稿已生成，尚未发送');
  }catch(e){$('#taskPolishHint').textContent='润色未完成：'+e.message;toast(e.message);}finally{button.disabled=false;$('#taskSendButton').disabled=false;}
});
$('#taskReplyForm').addEventListener('submit',async e=>{e.preventDefault();let b=e.submitter;b.disabled=true;try{const r=await api(`/api/tasks/${state.task.id}/reply`,mutation('POST',{content:$('#taskReply').value,request_id:crypto.randomUUID()}));toast(statusNames[r.status]||r.status);if(r.status==='sent')$('#taskDialog').close();await loadState();}catch(e){toast(e.message);}finally{b.disabled=false;}});
$('#newTaskForm').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/tasks',mutation('POST',{title:$('#newTaskTitle').value,details:$('#newTaskDetails').value,chat_id:$('#newTaskChat').value,priority:'normal'}));$('#newTaskDialog').close();e.target.reset();await loadState();toast('待办已添加');}catch(e){toast(e.message);}});
$('#date').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
loadState().catch(e=>toast('连接工作台失败：'+e.message));
api('/api/control/history').then(jobs=>{for(const j of jobs){addBubble(j.input,'user');if(j.status==='done'){const r=JSON.parse(j.result);const summary=(r.actions||[]).map(a=>`${a.type}：${a.ok?(statusNames[a.data?.status]||'已完成'):a.error}`).join('\n');addBubble((r.answer||'已处理')+(summary?'\n\n'+summary:''));}else if(j.status==='failed')addBubble('处理未完成：'+j.error);else addBubble('这条指令正在后台处理，可稍后刷新查看结果。');}}).catch(()=>{});
setInterval(()=>loadState(state.view==='home'||state.view==='activity').then(()=>refreshChatMessages()).catch(()=>{}),5000);
