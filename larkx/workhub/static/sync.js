/* Pure record reconciliation, shared by the browser and Node regression tests. */
(function(root){
  function apply(data, packet, keys){
    const result=packet.reset?{}:{...(data||{})}, changed=new Set();
    for(const [name,patch] of Object.entries(packet.changes)){
      if(keys[name]){
        const key=keys[name], rows=new Map((result[name]||[]).map(row=>[row[key],row]));
        for(const id of patch.removed)rows.delete(id);
        for(const row of patch.upsert)rows.set(row[key],row);
        result[name]=patch.order?patch.order.map(id=>rows.get(id)).filter(Boolean):Array.from(rows.values());
      }else result[name]={...(result[name]||{}),...patch};
      changed.add(name);
    }
    result.epoch=packet.epoch;result.revision=packet.revision;
    return {data:result,changed};
  }
  const sync={apply};
  if(typeof module!=='undefined'&&module.exports)module.exports=sync;else root.WorkhubSync=sync;
})(globalThis);
