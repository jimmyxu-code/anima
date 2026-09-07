const C='xk-entry-v4';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>self.clients.claim());
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);
 if(u.pathname.endsWith('current.txt')){
  /* 网络优先但放宽到 6s（CN 4G 访问 github.io 常 2-5s；1.5s 实锤
     会退到过期缓存=死地址，09-07 用户"打不开"根因）。超时才回缓存。 */
  e.respondWith((async()=>{
   try{
    const r=await Promise.race([fetch(e.request,{cache:'no-store'}),
     new Promise((_,rej)=>setTimeout(()=>rej(new Error('t')),6000))]);
    const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));
    return r;
   }catch(_){
    const h=await caches.match(e.request);
    return h||new Response('',{status:503});
   }
  })());
  return;
 }
 if(e.request.mode==='navigate'||/\.(html|webmanifest|png)$/.test(u.pathname)){
  e.respondWith(caches.match(e.request,{ignoreSearch:true})
   .then(h=>h||fetch(e.request)
   .then(r=>{const cp=r.clone();
    caches.open(C).then(c=>c.put(e.request,cp));return r})));
 }
});
