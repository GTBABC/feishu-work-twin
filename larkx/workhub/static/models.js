/* Model connection drafts are separate from runtime/context settings. */
(function(root){
  function id(kind,provider){return kind+':'+provider;}
  function config(info,selection,drafts){return {...info.profiles[selection.kind][selection.provider],...(drafts[id(selection.kind,selection.provider)]||{}),...selection};}
  function payload(config){return Object.fromEntries(['kind','provider','model','path','base_url','reasoning','vision','api_key','clear_key'].filter(key=>Object.hasOwn(config,key)).map(key=>[key,config[key]]));}
  const models={id,config,payload};
  if(typeof module!=='undefined'&&module.exports)module.exports=models;else root.WorkhubModels=models;
})(globalThis);
