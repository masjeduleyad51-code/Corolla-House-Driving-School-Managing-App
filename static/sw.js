// Caches only public static files. Pages with personal data are never cached.
const C='corolla-static-v1',F=['/static/style.css','/static/icon-192.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(F)));self.skipWaiting()});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
 if(u.pathname.startsWith('/static/'))e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));
 else if(e.request.mode==='navigate')e.respondWith(fetch(e.request).catch(()=>new Response('<meta name=viewport content="width=device-width"><body style="font-family:sans-serif;padding:2rem"><h2>You are offline</h2><p>Corolla House needs a connection. Try again in a moment.</p>',{headers:{'Content-Type':'text/html'}})))});
