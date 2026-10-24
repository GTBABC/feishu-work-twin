/* Drafts contain only edited fields; an in-flight save cannot erase later edits. */
(function(root){
  function changes(info,group,draft){
    return Object.fromEntries(info.fields.filter(f=>f.group===group&&Object.hasOwn(draft,f.key)&&draft[f.key]!==info.values[f.key]).map(f=>[f.key,draft[f.key]]));
  }
  function reset(info,group){
    return Object.fromEntries(info.fields.filter(f=>f.group===group&&info.defaults[f.key]!==info.values[f.key]).map(f=>[f.key,info.defaults[f.key]]));
  }
  function saved(draft,submitted){
    const remaining={...draft};
    for(const [key,value] of Object.entries(submitted))if(remaining[key]===value)delete remaining[key];
    return remaining;
  }
  function startupDraft(info){
    const v=info.values,s=info.storage;
    return {data_dir:v.data_dir,port:v.port,...s,sqlite_path:s.sqlite_path||v.data_dir.replace(/[\\/]$/,'')+'/messages.db',password:''};
  }
  function startupPayload(draft){
    const keys=['data_dir','port','storage_kind',...(draft.storage_kind==='sqlite'?['sqlite_path']:['mysql_host','mysql_port','mysql_database','mysql_user','password'])];
    return Object.fromEntries(keys.map(key=>[key,draft[key]]));
  }
  const settings={changes,reset,saved,startupDraft,startupPayload};
  if(typeof module!=='undefined'&&module.exports)module.exports=settings;else root.WorkhubSettings=settings;
})(globalThis);
