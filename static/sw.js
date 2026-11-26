// Minimal service worker: makes the page installable as an app. Always goes to the network
// (light state must be live), with a small offline notice if the server can't be reached.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    `<meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0d0e12;color:#8a8fa1;font:16px system-ui;padding:40px 24px">
     <h2 style="color:#eceef3">Can't reach the lights</h2>
     <p>The PC may be asleep or Tailscale may be off on this phone.</p></body>`,
    { headers: { "Content-Type": "text/html" } })));
});
