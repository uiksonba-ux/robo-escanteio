const CACHE="baskaraido-operations-shell-v3";
const SHELL=["/app","/static/operations.html","/static/operations.webmanifest","/static/operations-icon.svg"];
self.addEventListener("install",event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener("activate",event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener("fetch",event=>{const url=new URL(event.request.url);if(event.request.method!=="GET"||url.pathname.startsWith("/api/"))return;if(event.request.mode==="navigate"&&url.pathname==="/app"){event.respondWith(fetch(event.request).catch(()=>caches.match("/app")));return}if(SHELL.includes(url.pathname))event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)))});
