(function(root){
  function capacity(viewport,top,rowHeight,footer=60){return Math.max(1,Math.min(100,Math.floor((viewport-top-footer-24)/rowHeight)));}
  function resizedPage(page,oldSize,newSize){return Math.floor((page-1)*oldSize/newSize)+1;}
  function numbers(page,pages){return [...new Set([1,page-1,page,page+1,pages])].filter(n=>n>=1&&n<=pages).sort((a,b)=>a-b);}
  const paging={capacity,resizedPage,numbers};
  if(typeof module!=='undefined'&&module.exports)module.exports=paging;else root.WorkhubPaging=paging;
})(globalThis);
