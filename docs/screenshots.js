// Regenerates the README screenshots from a running server. Read-only: opens views and panels, never sends a light command.
//   npm i -g playwright   (uses your installed Chrome)
//   node docs/screenshots.js http://127.0.0.1:8420 docs/screenshots
const { chromium } = require("playwright");
const BASE = process.argv[2];
const OUT = process.argv[3];
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ channel: "chrome" });
  const shot = async (page, name, opts = {}) => {
    await page.screenshot({ path: `${OUT}/${name}.jpg`, type: "jpeg", quality: 88, ...opts });
    console.log("saved", name);
  };

  // ---------- desktop ----------
  const desk = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: "dark" });
  await desk.goto(BASE + "/");
  await desk.waitForSelector(".tile");
  await wait(1500);
  await shot(desk, "desktop-home");

  // controls panel for one light
  await desk.evaluate(() => {
    const t = [...document.querySelectorAll(".tile")].find(t => t.textContent.includes("Tree Floor Lamp"));
    t.querySelector(".more").click();
  });
  await wait(900);
  await shot(desk, "desktop-controls");
  await desk.evaluate(() => document.querySelector(".seg button[data-t=color]").click());
  await wait(500);
  await shot(desk, "desktop-color");
  await desk.keyboard.press("Escape");
  await wait(500);

  // room map
  await desk.evaluate(() => localStorage.setItem("lights.view.Office", "map"));
  await desk.reload();
  await desk.waitForSelector("#room-Office .node");
  await wait(1800);
  await (await desk.$("#room-Office")).screenshot({ path: `${OUT}/room-map.jpg`, type: "jpeg", quality: 88 });
  console.log("saved room-map");

  // schedule (week + times only)
  await desk.goto(BASE + "/#schedule");
  await desk.waitForSelector("#week .tl");
  await wait(800);
  await desk.evaluate(() => { const cards = document.querySelectorAll(".card"); cards.forEach((c, i) => { if (i > 1) c.style.display = "none"; }); });
  await shot(desk, "schedule", { fullPage: true });

  // screen-sync picker (settings card only: the light list above it shows LAN addresses)
  await desk.goto(BASE + "/#settings");
  await desk.waitForSelector("#scrPick .mon");
  await wait(800);
  await (await desk.$(".card.desk")).screenshot({ path: `${OUT}/screen-sync.jpg`, type: "jpeg", quality: 88 });
  console.log("saved screen-sync");

  // ---------- phone ----------
  const phone = await browser.newPage({ viewport: { width: 412, height: 915 }, deviceScaleFactor: 2.6, isMobile: true, hasTouch: true, colorScheme: "dark" });
  await phone.goto(BASE + "/m");
  await phone.waitForSelector(".tile");
  await wait(1500);
  await shot(phone, "phone-home");
  await phone.evaluate(() => {
    const t = [...document.querySelectorAll(".tile")].find(t => t.textContent.includes("Corner Lamp"));
    t.querySelector(".more").click();
  });
  await wait(900);
  await shot(phone, "phone-controls");

  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
