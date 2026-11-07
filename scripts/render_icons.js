// Render brand/icon.svg to icon.png (256) and icon@2x.png (512) with transparency.
// Usage: node scripts/render_icons.js custom_components/ping_monitor/brand/icon.svg custom_components/ping_monitor/brand
// Requires Playwright with Chromium (npm i -g playwright && npx playwright install chromium).
const { chromium } = require('playwright');
const fs = require('fs');
const [svgPath, outDir] = process.argv.slice(2);
const svg = fs.readFileSync(svgPath, 'utf8');
(async () => {
  const browser = await chromium.launch();
  for (const [name, size] of [['icon.png', 256], ['icon@2x.png', 512]]) {
    const page = await browser.newPage({ viewport: { width: size, height: size } });
    await page.setContent(`<html><body style="margin:0;background:transparent">${svg.replace(/width="256" height="256"/, `width="${size}" height="${size}"`)}</body></html>`);
    await page.screenshot({ path: `${outDir}/${name}`, omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
    await page.close();
  }
  await browser.close();
})();
