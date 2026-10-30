// Drives the `gita-reels studio` web UI in headless Chromium (Playwright, preinstalled in the container).
// Start the studio first (see SKILL.md), then:
//
//   node .claude/skills/run-gita-studio/driver.mjs flow [--episode e02] [--url http://127.0.0.1:8765]
//       plan a reel (template planner + animatic provider, no keys, no spend), generate a take per shot,
//       approve them, assemble, and wait for the validated final.mp4. Screenshots at every step.
//   node .claude/skills/run-gita-studio/driver.mjs ss [--project <id>]
//       one screenshot of the studio, optionally with a project opened.
//
// Screenshots land in $SHOTS (default /tmp/gita-studio-shots/). Exit code 0 = flow completed.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

// The container's browsers live in /opt/pw-browsers; a shell without PLAYWRIGHT_BROWSERS_PATH looks in ~/.cache instead.
// Playwright reads it when the module loads, so set it before require().
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/pw-browsers')) process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
// ESM `import 'playwright'` ignores NODE_PATH, and the container's Playwright lives outside this repo
// (/opt/node-tools/node_modules), so resolve it the CommonJS way, which does honour NODE_PATH.
const require = createRequire(import.meta.url);
let chromium;
for (const p of ['playwright', '/opt/node-tools/node_modules/playwright']) {
  try { ({ chromium } = require(p)); break; } catch {}
}
if (!chromium) throw new Error('playwright not found: npm i -g playwright, or set NODE_PATH');

const args = process.argv.slice(2);
const cmd = args[0] || 'flow';
const opt = (k, d) => { const i = args.indexOf(`--${k}`); return i >= 0 ? args[i + 1] : d; };
const URL = opt('url', 'http://127.0.0.1:8765');
const EPISODE = opt('episode', 'e02');
const SHOTS = process.env.SHOTS || '/tmp/gita-studio-shots';
fs.mkdirSync(SHOTS, { recursive: true });

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage();
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('dialog', d => { errors.push(`alert: ${d.message()}`); d.dismiss(); });  // the UI reports API errors with alert()

let n = 0;
async function ss(name) {
  const f = path.join(SHOTS, `${String(++n).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: f, fullPage: true });
  console.log('screenshot', f);
}
const api = p => page.evaluate(async p => (await fetch(p)).json(), p);

async function openProject(id) {
  await page.click(`#plist > div[data-id="${id}"]`);
  await page.waitForSelector('.shot');
}

try {
  await page.goto(URL);
  await page.waitForFunction(() => document.querySelector('#keys')?.textContent.includes('Claude'));
  console.log('keys:', await page.textContent('#keys'));

  if (cmd === 'ss') {
    const id = opt('project');
    if (id) await openProject(id);
    await ss(id ? `project-${id}` : 'studio');
  } else if (cmd === 'flow') {
    const before = new Set((await api('/api/projects')).map(p => p.id));
    await page.selectOption('#f-episode', EPISODE);
    await page.selectOption('#f-aspect', '9:16');
    // 1080p, not 720p: at 720x1280 the animatic's locked-off shot trips the frozen-picture check (see SKILL.md)
    await page.selectOption('#f-res', '1080p');
    await page.selectOption('#f-provider', 'animatic');
    await page.selectOption('#f-planner', 'template');
    await ss('form');
    await page.click('#create');

    // the plan job runs as a subprocess; the project list refreshes on the 3 s tick when it finishes
    let id;
    for (let t = 0; t < 60 && !id; t++) {
      await page.waitForTimeout(1000);
      id = (await api('/api/projects')).map(p => p.id).find(p => !before.has(p));
    }
    if (!id) throw new Error('no new project after 60 s; see reels/.jobs/*.log');
    console.log('project', id);
    await page.waitForSelector(`#plist > div[data-id="${id}"]`, { timeout: 15000 });
    await openProject(id);
    await ss('planned');

    const count = await page.locator('.shot').count();
    for (let i = 0; i < count; i++) {
      // re-query each time: every click re-renders #main
      await page.locator(`.shot[data-i="${i}"] [data-gen]`).click();
      await page.waitForTimeout(300);
    }
    console.log(`requested ${count} takes`);
    await page.waitForFunction(n => document.querySelectorAll('.shot [data-sel]').length >= n, count,
                               { timeout: 180000, polling: 1000 });
    await ss('takes-ready');

    for (let i = 0; i < count; i++) {
      await page.locator(`.shot[data-i="${i}"] [data-sel]`).first().click();
      await page.waitForSelector(`.shot[data-i="${i}"] .pill.ok:has-text("approved")`);
    }
    await ss('approved');

    await page.click('#assemble');
    await page.waitForSelector('.final video', { timeout: 300000 });
    const meta = (await api(`/api/projects/${id}`)).meta;
    console.log('status', meta.status, 'validation ok', meta.validation?.ok, 'seconds', meta.edit?.seconds);
    await page.evaluate(() => { const v = document.querySelector('.final video'); v.currentTime = 4; });
    await page.waitForTimeout(1500);
    await ss('assembled');
    console.log(`final: reels/${id}/final.mp4`);
    if (!meta.validation?.ok) throw new Error('export failed validation');
  } else {
    throw new Error(`unknown command ${cmd} (flow | ss)`);
  }
  if (errors.length) console.log('page errors:\n  ' + errors.join('\n  '));
} catch (e) {
  console.error('FAILED:', e.message);
  await ss('failure').catch(() => {});
  if (errors.length) console.error('page errors:\n  ' + errors.join('\n  '));
  process.exitCode = 1;
} finally {
  await browser.close();
}
