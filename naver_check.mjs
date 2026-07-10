// 네이버 부동산 수집 가능성 점검 (헤드리스 크롬 + CDP).
// 목적: GitHub Actions(Azure IP)에서도 in-page fetch가 200 나오나 확인.
// 성공 판정: 타깃 지역 중 하나라도 status 200 + 매물 반환 → exit 0, 아니면 exit 1.
// 크롬 실행파일 경로는 env CHROME_BIN (없으면 'chromium').
import { spawn } from 'node:child_process';
import http from 'node:http';

const CHROME = process.env.CHROME_BIN || 'chromium';
const PORT = 9400 + Math.floor((Date.now() % 500));
const URL0 = 'https://new.land.naver.com/offices?ms=37.4845,127.0341,16&a=OPST&b=B1';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36';

const child = spawn(CHROME, [
  '--headless=new','--disable-gpu','--no-sandbox','--disable-dev-shm-usage',
  `--remote-debugging-port=${PORT}`,'--remote-debugging-address=127.0.0.1','--lang=ko-KR',
  `--user-agent=${UA}`, 'about:blank',
], { stdio: ['ignore','pipe','pipe'] });
let clog = '';
child.stderr.on('data', d => clog += d);

const getJSON = p => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, r => {
    let b = ''; r.on('data', c => b += c); r.on('end', () => res(JSON.parse(b)));
  }).on('error', rej);
});
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitTarget() {
  for (let i = 0; i < 40; i++) {
    try { const t = await getJSON('/json'); const p = t.find(x => x.type === 'page' && x.webSocketDebuggerUrl); if (p) return p; } catch {}
    await sleep(500);
  }
  throw new Error('CDP not up. chromium stderr:\n' + clog.slice(0, 800));
}

const pend = new Map();
let idc = 0;
function cdp(ws, method, params = {}) {
  return new Promise((res, rej) => { const id = ++idc; pend.set(id, [res, rej]); ws.send(JSON.stringify({ id, method, params })); });
}

function fail(msg) { console.error('FAIL:', msg); try { child.kill('SIGKILL'); } catch {} process.exit(1); }

const tgt = await waitTarget();
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

let capToken = null;
ws.addEventListener('message', e => {
  const j = JSON.parse(e.data);
  if (j.id && pend.has(j.id)) { const [res, rej] = pend.get(j.id); pend.delete(j.id); j.error ? rej(new Error(JSON.stringify(j.error))) : res(j.result); return; }
  if (j.method === 'Network.requestWillBeSent') {
    const h = j.params.request.headers || {};
    const auth = h.Authorization || h.authorization;
    if (auth && /^Bearer /.test(auth) && j.params.request.url.includes('/api/') && !capToken) capToken = auth;
  }
});

await cdp(ws, 'Network.enable');
await cdp(ws, 'Runtime.enable');
await cdp(ws, 'Page.enable');

// 커맨드라인 URL 인자를 무시하는 크롬 빌드가 있어(CI) 명시적으로 이동시킨다
await cdp(ws, 'Page.navigate', { url: URL0 });

// 앱이 스스로 /api/* 를 쏘면서 실어보내는 Authorization 헤더를 가로챈다
for (let i = 0; i < 50 && !capToken; i++) await sleep(500);
console.log('token capture:', capToken ? 'OK (' + capToken.slice(0, 24) + '...)' : 'NONE');

// 진단: 페이지가 떴는지 + 토큰 없이 in-page fetch 하면 429(IP지문차단)냐 401(지문통과)이냐
const diag = await cdp(ws, 'Runtime.evaluate', { expression: `(async()=>{
  const o={ title:document.title, url:location.href, bodyLen:(document.body?document.body.innerText.length:0) };
  try{ const r=await fetch('/api/articles?cortarNo=1168010100&realEstateType=OPST&tradeType=B1&page=1',{headers:{'Referer':location.href}});
    o.noAuthStatus=r.status; o.noAuthBody=(await r.text()).slice(0,80);
  }catch(e){ o.noAuthErr=String(e); }
  return JSON.stringify(o);
})()`, awaitPromise: true, returnByValue: true });
console.log('diag:', diag.result.value);

if (!capToken) fail('토큰 미포착. 위 diag 판독 → noAuthStatus 429=IP째 지문차단(브라우저도 못뚫음) / 401=지문통과인데 SPA 렌더 지연으로 토큰만 못딴 것 / bodyLen 0=페이지 자체가 안 뜸');

// 가로챈 토큰으로 타깃 지역에 in-page fetch (핑거프린트는 브라우저 컨텍스트라 통과)
const expr = `(async()=>{
  const AUTH=${JSON.stringify(capToken)};
  const areas={'역삼동':'1168010100','양재동':'1165010900'};
  const out={};
  for(const [nm,a] of Object.entries(areas)){
    const u='/api/articles?cortarNo='+a+'&order=rank&realEstateType=OPST&tradeType=B1&tag=%3A%3A%3A%3A%3A%3A%3A%3A&rentPriceMin=0&rentPriceMax=900000000&priceMin=0&priceMax=900000000&areaMin=0&areaMax=900000000&showArticle=false&sameAddressGroup=false&priceType=RETAIL&page=1';
    try{ const r=await fetch(u,{headers:{'Authorization':AUTH,'Referer':location.href}}); let n=0,ex='';
      try{ const j=await r.json(); n=(j.articleList||[]).length; ex=(j.articleList&&j.articleList[0])?j.articleList[0].articleName:''; }catch(e){}
      out[nm]={status:r.status,n,ex};
    }catch(e){ out[nm]={err:String(e)}; }
  }
  return JSON.stringify(out);
})()`;
const r = await cdp(ws, 'Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
const res = JSON.parse(r.result.value || '{}');
console.log('fetch result:', JSON.stringify(res, null, 1));
child.kill('SIGKILL');

const ok = Object.values(res).some(v => v.status === 200 && v.n > 0);
if (ok) { console.log('\nPASS: Azure IP에서 헤드리스 크롬으로 네이버 매물 수집 가능'); process.exit(0); }
fail('200+매물 조합 없음 → CI에서 수집 불가 (아래 status 확인: 429=IP지문차단, 401=토큰거부)');
