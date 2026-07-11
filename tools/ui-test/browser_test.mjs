/* Full-browser regression for the unified `akcli view` server.
 *
 * Expects AKCLI_VIEW_URL to point at a running server whose live timeline is
 * seeded with the fixture from tests/test_webui_browser.py:
 *   step 1: single sheet, 1 ERC error (markable position)
 *   step 2: two sheets, ERC = step-1's error + 1 NEW warning
 *   state.watcher_error is set (the banner chip must show it)
 * Exits 0 when every check passes and no unexpected JS error fired.
 */
import puppeteer from "puppeteer-core";

const BASE = process.env.AKCLI_VIEW_URL || "http://127.0.0.1:8765";
const CHROME = process.env.CHROME_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SHOTS = process.env.SHOT_DIR || null;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: "new",
  args: ["--no-first-run", "--window-size=1600,1000"],
});
const page = await browser.newPage();
await page.setViewport({width: 1600, height: 1000});
const errors = [];
page.on("pageerror", e => errors.push("pageerror: " + e.message));
page.on("console", m => {
  // expected-4xx fetches (API validation contract) are not defects
  if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
    errors.push("console: " + m.text());
});
page.on("dialog", d => d.accept());

const fails = [];
const check = (name, cond) => {
  if (!cond) fails.push(name);
  console.log((cond ? "  ok " : "FAIL "), name);
};
const shot = async name => { if (SHOTS) await page.screenshot({path: `${SHOTS}/${name}.png`}); };
const setField = (sel, v) => page.evaluate((sel, v) => {
  const el = document.querySelector(sel);
  el.value = v; el.dispatchEvent(new Event("input"));
}, sel, v);

/* ================= hub (entry page) ================= */
// SSE keeps the connection open -> networkidle never fires
await page.goto(BASE + "/", {waitUntil: "domcontentloaded"});
await page.waitForSelector("#cards .card", {timeout: 8000});
await sleep(700);                       // let the status fetches land
check("hub: two entry cards", (await page.$$eval("#cards .card", els => els.length)) === 2);
check("hub: calc card shows calculator count",
      (await page.$eval("#calcstatus", el => el.textContent)).includes("calculators"));
const liveStatus = await page.$eval("#livestatus", el => el.textContent);
check("hub: live card shows the watched file", liveStatus.includes("t.kicad_sch"));
check("hub: live card shows step count", liveStatus.includes("2"));
await shot("hub");

check("hub: shared chrome tabs with ⌂ active",
      (await page.$eval(".tabs a.on", el => el.textContent.trim())) === "⌂");

/* ================= calc bench ================= */
await page.click("#calccard");          // enter through the hub
await page.waitForSelector(".tile", {timeout: 8000});
check("calc: home launcher tiles", (await page.$$(".tile")).length >= 50);
check("calc: shared chrome tabs with CALC active",
      (await page.$eval(".tabs a.on", el => el.textContent)).includes("calc"));
check("calc: live nav link visible when watching",
      await page.$eval("#livelink", el => !el.hidden));

await page.click('.tile[data-name="trackwidth"]');
await page.waitForSelector("#formcard");
check("calc: default placeholder in engineering notation",
      (await page.$eval('#form [data-param="thickness"]', el => el.placeholder)) === "35u");
await page.type('#form [data-param="i"]', "2");
await page.type('#form [data-param="dtemp"]', "10");
await page.waitForSelector("#rescard:not([hidden]) table", {timeout: 5000});
const width = await page.$eval("#results .val .num", el => el.textContent);
check("calc: auto-computed external_width = " + width, width.includes("781.4"));
check("calc: diagram caption annotated with live values",
      (await page.$eval("#an1", el => el.textContent)).includes("781.4"));
check("calc: parse hint", (await page.$$eval(".pmeta .parsed",
      els => els.map(e => e.textContent).join(" "))).includes("2 A"));
check("calc: CLI mirror", (await page.$eval("#clicmd", el => el.textContent))
      .includes("akcli calc trackwidth i=2 dtemp=10"));
check("calc: hash share URL", await page.evaluate(() =>
      location.hash.includes("calc=trackwidth") && location.hash.includes("i=2")));
await shot("calc-run");

await setField('#form [data-param="i"]', "4k7m");   // junk -> flagged, no crash
await sleep(450);
check("calc: junk input flagged", await page.$eval('#form [data-param="i"]',
      el => el.classList.contains("bad")));
check("calc: unparseable hint",
      (await page.$eval('[data-parsed-for="i"]', el => el.textContent)) === "unparseable");
await setField('#form [data-param="i"]', "3");
await sleep(700);
check("calc: delta chip after change", (await page.$$eval(".delta", els => els.length)) > 0);

await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
await page.waitForSelector("#palov:not([hidden])");
await page.type("#palinput", "i2c");
await page.keyboard.press("Enter");
await page.waitForFunction(() =>
      document.querySelector("#title")?.textContent.includes("i2c-pullup"));
check("calc: palette jump", true);
check("calc: op-list button on mappable calc", await page.$eval("#opsbtn", el => !el.hidden));

await page.goto(BASE + "/calc#calc=rescolor&value=zzz", {waitUntil: "networkidle0"});
await sleep(400);
check("calc: hash junk stays client-side",
      (await page.$eval("#error", el => el.textContent)) === "");
check("calc: offending field flagged", await page.$eval('#form [data-param="value"]',
      el => el.classList.contains("bad")));
await page.goto(BASE + "/calc#calc=ohm&v=5&i=2&r=3", {waitUntil: "networkidle0"});
await sleep(500);
check("calc: server error surfaced", (await page.$eval("#error", el => el.textContent))
      .includes("exactly two"));
await page.click("#themebtn");
check("calc: theme flips", await page.evaluate(() =>
      document.documentElement.dataset.theme === "light"));
await shot("calc-light");

/* ================= live watch ================= */
// networkidle never fires here: the page keeps an SSE connection open
await page.goto(BASE + "/live", {waitUntil: "domcontentloaded"});
await page.waitForSelector(".step", {timeout: 8000});
await page.waitForFunction(() => !document.getElementById("frame").hidden, {timeout: 8000});
check("live: 2 timeline steps", (await page.$$(".step")).length === 2);
check("live: shared chrome tabs with LIVE active",
      (await page.$eval(".tabs a.on", el => el.textContent)).includes("live"));
check("live: svg inlined", await page.$("#art svg") !== null);
check("live: ERC chip totals", (await page.$eval("#erctot", el => el.textContent)).includes("1E"));
check("live: NEW tag on the fresh violation",
      (await page.$$eval(".newtag", els => els.length)) === 1);
check("live: +new indicator in panel header",
      (await page.$eval("#erccnt", el => el.textContent)).includes("+1 new"));
check("live: watcher error banner chip",
      (await page.$eval("#werr", el => !el.hidden)) &&
      (await page.$eval("#werrmsg", el => el.textContent)).includes("exploded"));
check("live: sheet tabs on the multi-sheet step",
      await page.$eval("#sheettabs", el => !el.hidden) &&
      (await page.$$eval("#sheettabs button", els => els.length)) === 2);
await page.click("#sheettabs button:nth-child(2)");
await sleep(300);
check("live: child sheet selected", await page.$eval("#sheettabs button:nth-child(2)",
      el => el.classList.contains("on")));
check("live: note bar visible when watching", await page.$eval("#notebar", el => !el.hidden));
await page.type("#noteinp", "ui-test note");
await page.keyboard.press("Enter");
await sleep(300);
check("live: note accepted (input cleared)",
      (await page.$eval("#noteinp", el => el.value)) === "");
await shot("live-step2");

/* keyboard a11y: Tab reaches the timeline step buttons, Enter activates one */
await page.focus("#clearbtn");
await page.keyboard.press("Tab");          // -> newest step (#2)
await page.keyboard.press("Tab");          // -> step #1
check("live: Tab reaches a step button", await page.evaluate(() =>
      document.activeElement.classList.contains("step") &&
      document.activeElement.dataset.i === "0"));
await page.keyboard.press("Enter");
await sleep(400);
check("live: Enter activates the focused step", await page.$eval('.step[data-i="0"]',
      el => el.classList.contains("active") && el.getAttribute("aria-current") === "step"));

/* step 1: single sheet -> markers work */
await page.click('.step[data-i="0"]');
await sleep(400);
await page.keyboard.press("e");           // ERC markers on
await page.waitForFunction(() => !document.getElementById("ovl").hidden);
check("live: ERC marker drawn", (await page.$$eval("#ovl .mk", els => els.length)) === 1);
await page.click(".viol");
await sleep(300);
check("live: jump zooms in", await page.evaluate(() =>
      document.getElementById("zlvl").textContent !== "fit"));
await page.keyboard.press("f");
await page.keyboard.press("l");           // back to live
await sleep(300);
await page.click("#diff");
await page.waitForFunction(() => !document.getElementById("ghost").hidden);
check("live: diff ghost visible", await page.$("#ghost svg") !== null);
await shot("live-diff");

/* clear (dialog auto-accepted) */
await page.click("#clearbtn");
await page.waitForFunction(() => !document.getElementById("empty").hidden, {timeout: 5000});
check("live: timeline cleared to empty state", true);

/* keyboard h returns to the hub from a subpage */
await page.keyboard.press("h");
await page.waitForSelector("#cards .card", {timeout: 5000});
check("live: `h` returns to the hub", new URL(page.url()).pathname === "/");

console.log("");
if (errors.length){ console.log("JS ERRORS:"); errors.forEach(e => console.log(" ", e)); }
console.log(fails.length || errors.length
  ? `RESULT: ${fails.length} fails, ${errors.length} js errors`
  : "RESULT: all browser checks passed, zero JS errors");
await browser.close();
process.exit(fails.length || errors.length ? 1 : 0);
