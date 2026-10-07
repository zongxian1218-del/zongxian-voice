import { chromium } from 'playwright';

const ch = process.env.CH || 'msedge';
const b = await chromium.launch({ channel: ch, headless: true });
const p = await b.newPage();
await p.setContent('<h1>hello</h1>');
console.log('channel:', ch);
console.log('h1:', await p.evaluate(() => document.querySelector('h1').textContent));
console.log('RTCPeerConnection:', await p.evaluate(() => typeof RTCPeerConnection));
console.log('showDirectoryPicker:', await p.evaluate(() => typeof window.showDirectoryPicker));
console.log('indexedDB:', await p.evaluate(() => typeof indexedDB));
console.log('crypto.subtle:', await p.evaluate(() => typeof crypto.subtle));
await b.close();
console.log('SMOKE OK');
