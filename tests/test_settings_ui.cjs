const {test}=require('node:test');
const assert=require('node:assert/strict');
const drafts=require('../larkx/workhub/static/settings.js');
const info={fields:[{key:'suffix',group:'general'},{key:'enabled',group:'general'},{key:'pause',group:'messages'},{key:'model',group:'model'}],values:{suffix:'Bot',enabled:true,pause:120,model:'env-model'},defaults:{suffix:'',enabled:true,pause:0,model:''}};
test('saving a group posts only changed fields and preserves false/zero/empty',()=>{
  assert.deepEqual(drafts.changes(info,'general',{suffix:'',enabled:false,pause:0}),{suffix:'',enabled:false});
  assert.deepEqual(drafts.changes(info,'messages',{pause:0}),{pause:0});
  assert.deepEqual(drafts.changes(info,'model',{model:''}),{model:''});
});
test('reset fills only the selected group and never mutates effective values',()=>{
  assert.deepEqual(drafts.reset(info,'general'),{suffix:''});
  assert.equal(info.values.suffix,'Bot');
  assert.equal(info.values.pause,120);
});
test('a completed save cannot discard edits typed while it was running',()=>{
  assert.deepEqual(drafts.saved({suffix:'later',enabled:false},{suffix:'submitted',enabled:false}),{suffix:'later'});
});
test('unchanged input and an empty draft produce no write',()=>{
  assert.deepEqual(drafts.changes(info,'messages',{pause:120}),{});
  assert.deepEqual(drafts.changes(info,'general',{}),{});
});

const models=require('../larkx/workhub/static/models.js');
const paging=require('../larkx/workhub/static/paging.js');

test('page size follows viewport and row height, keeping the first visible record on resize',()=>{
  assert.equal(paging.capacity(900,300,80),6);
  assert.equal(paging.capacity(1080,300,80),8);
  assert.equal(paging.capacity(600,300,100),2);
  assert.equal(paging.capacity(300,290,100),1);
  assert.equal(paging.resizedPage(3,8,6),3);
  assert.deepEqual(paging.numbers(7,50),[1,6,7,8,50]);
});
test('model payload excludes response metadata and keeps blank key semantics',()=>{
  assert.deepEqual(models.payload({kind:'api',provider:'qwen',model:'m',api_key:'',key_set:true,protocol:'openai',label:'ignored'}),{kind:'api',provider:'qwen',model:'m',api_key:''});
});

test('startup payload sends only the selected storage fields',()=>{
  const draft={data_dir:'C:/data',port:9876,storage_kind:'sqlite',sqlite_path:'C:/data/a.db',mysql_host:'localhost',mysql_port:3306,mysql_database:'业务库',mysql_user:'u@ser',password:'p@ss:/?#',restart_required:true,current:{}};
  assert.deepEqual(drafts.startupPayload(draft),{data_dir:'C:/data',port:9876,storage_kind:'sqlite',sqlite_path:'C:/data/a.db'});
  assert.deepEqual(drafts.startupPayload({...draft,storage_kind:'mysql'}),{data_dir:'C:/data',port:9876,storage_kind:'mysql',mysql_host:'localhost',mysql_port:3306,mysql_database:'业务库',mysql_user:'u@ser',password:'p@ss:/?#'});
  assert.equal(drafts.startupPayload({...draft,storage_kind:'mysql',password:''}).password,'');
});

test('saved MySQL details populate the form without parsing a URI or exposing passwords',()=>{
  const draft=drafts.startupDraft({values:{data_dir:'C:/data',port:8765,storage_url:'mysql+pymysql://redacted'},storage:{storage_kind:'mysql',sqlite_path:'',mysql_host:'db.example',mysql_port:3307,mysql_database:'业务库',mysql_user:'user'}});
  assert.equal(draft.mysql_user,'user');
  assert.equal(draft.mysql_port,3307);
  assert.equal(draft.password,'');
  assert.equal(draft.sqlite_path,'C:/data/messages.db');
  assert.equal(Object.hasOwn(draft,'storage_url'),false);
});
