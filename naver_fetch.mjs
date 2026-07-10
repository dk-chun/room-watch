#!/usr/bin/env node
// 네이버 부동산 통근권 매물 수집 (로컬 온디맨드 전용).
// GitHub Actions(Azure IP)는 네이버가 ERR_CONNECTION_RESET으로 네트워크 차단 → CI 불가.
// 로컬(가정용 IP)에서 헤드리스 크롬 CDP로 앱 토큰 가로채 in-page fetch.
// 실행: node naver_fetch.mjs  →  바탕화면 방매물_네이버.html 생성
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

// ===== CONFIG (직방 fetch_listings.py와 동일 기준) =====
const STATIONS = [[37.50466,127.02503],[37.49794,127.02762],[37.48453,127.03411],[37.47012,127.03856]]; // 신논현·강남·양재·양재숲
const SHUTTLE = { '강남셔틀':[37.496517,127.026931], '양재셔틀':[37.486832,127.032575], '양재숲셔틀':[37.470165,127.038620] };
const OFFICE = [37.4666247,127.0230600];   // 삼성 서울R&D캠퍼스 (성촌길 56)
const MIN_M2 = 29.75;                       // 전용면적 하한 = 9평
const MAX_DIST = 1200;                      // 최근접 역 거리 컷(m)
const NORTH = 37.500;                       // 이북 제외(신논현 위쪽)
// 통근권 관련 동만(멀면 어차피 거리컷에 걸림). 네이버 cortarNo.
const DONGS = { '1168010100':'역삼동','1168011800':'도곡동','1168010800':'논현동','1168010600':'대치동','1168010500':'삼성동',
                '1165010800':'서초동','1165010200':'양재동','1165010300':'우면동','1165010700':'반포동' };
const TYPES = ['VL','OPST','DDDGG','OR'];    // 빌라·오피스텔·단독다가구·원룸 (아파트 제외 = 직방과 동일)
const TRADES = [['B1','전세'],['B2','월세']];
const IMG_HOST = 'https://landthumb-phinf.pstatic.net';
const OUT = process.env.NAVER_DASHBOARD || '/mnt/c/Users/10x100/Desktop/방매물_네이버.html';
const CHROME = process.env.CHROME_BIN || 'chromium';
// =====================================================

const PORT = 9800 + Math.floor(Date.now() % 199);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36';

function hav(a,b,c,d){ const R=6371000,p=Math.PI/180;
  const x=Math.sin((c-a)*p/2)**2 + Math.cos(a*p)*Math.cos(c*p)*Math.sin((d-b)*p/2)**2;
  return 2*R*Math.asin(Math.sqrt(x)); }
function commute(lat,lng){
  let best=[Math.round(hav(lat,lng,OFFICE[0],OFFICE[1])),'회사도보'];
  for(const [n,s] of Object.entries(SHUTTLE)){ const d=Math.round(hav(lat,lng,s[0],s[1])); if(d<best[0]) best=[d,n]; }
  return best;
}
function nearestStation(lat,lng){ return Math.min(...STATIONS.map(s=>hav(lat,lng,s[0],s[1]))); }
function parseWon(s){ // "3억 1,200"→31200(만), "3,000"→3000, "145"→145, ""→0
  if(!s) return 0; s=String(s).replace(/,/g,'').trim(); let man=0;
  const m=s.match(/(\d+)\s*억/); if(m) man+=Number(m[1])*10000;
  const rest=s.replace(/\d+\s*억/,'').trim(); if(rest) man+=Number(rest)||0;
  return man;
}
const won = man => man>=10000 ? (man/10000).toFixed(man%10000?1:0).replace(/\.0$/,'')+'억' : man+'만';

// ---------- CDP 하네스 ----------
const child = spawn(CHROME, ['--headless=new','--disable-gpu','--no-sandbox','--disable-dev-shm-usage',
  `--remote-debugging-port=${PORT}`,'--remote-debugging-address=127.0.0.1','--lang=ko-KR',`--user-agent=${UA}`,'about:blank'],
  { stdio:['ignore','pipe','pipe'] });
let clog=''; child.stderr.on('data',d=>clog+=d);
const getJSON = p => new Promise((res,rej)=>{ http.get({host:'127.0.0.1',port:PORT,path:p},r=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>res(JSON.parse(b)));}).on('error',rej); });
const sleep = ms => new Promise(r=>setTimeout(r,ms));
async function waitTarget(){ for(let i=0;i<40;i++){ try{const t=await getJSON('/json');const p=t.find(x=>x.type==='page'&&x.webSocketDebuggerUrl);if(p)return p;}catch{} await sleep(500);} throw new Error('CDP down\n'+clog.slice(0,600)); }
const pend=new Map(); let idc=0;
const cdp=(ws,m,p={})=>new Promise((res,rej)=>{const id=++idc;pend.set(id,[res,rej]);ws.send(JSON.stringify({id,method:m,params:p}));});

const tgt = await waitTarget();
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((res,rej)=>{ws.addEventListener('open',res);ws.addEventListener('error',rej);});
let token=null;
ws.addEventListener('message',e=>{ const j=JSON.parse(e.data);
  if(j.id&&pend.has(j.id)){const[r,x]=pend.get(j.id);pend.delete(j.id);j.error?x(new Error(JSON.stringify(j.error))):r(j.result);return;}
  if(j.method==='Network.requestWillBeSent'){const h=j.params.request.headers||{};const a=h.Authorization||h.authorization;if(a&&/^Bearer/.test(a)&&j.params.request.url.includes('/api/')&&!token)token=a;}
});
await cdp(ws,'Network.enable'); await cdp(ws,'Runtime.enable'); await cdp(ws,'Page.enable');
await cdp(ws,'Page.navigate',{url:'https://new.land.naver.com/offices?ms=37.4845,127.0341,16&a=OPST&b=B1'});
for(let i=0;i<50&&!token;i++) await sleep(500);
if(!token){ console.error('토큰 미포착 (네트워크 차단?):\n'+clog.slice(0,400)); child.kill('SIGKILL'); process.exit(1); }
console.error('token OK');

async function inpage(code){ const r=await cdp(ws,'Runtime.evaluate',{expression:code,awaitPromise:true,returnByValue:true}); return r.result.value; }
const AUTH = JSON.stringify(token);

// ---------- 수집 (동별로 in-page 페이지네이션) ----------
const raw=[];
for(const [code,name] of Object.entries(DONGS)){
  const got = await inpage(`(async()=>{
    const AUTH=${AUTH}, out=[];
    for(const R of ${JSON.stringify(TYPES)}) for(const [T,TN] of ${JSON.stringify(TRADES)}){
      for(let page=1; page<=15; page++){
        const u='/api/articles?cortarNo=${code}&order=rank&realEstateType='+R+'&tradeType='+T+'&tag=%3A%3A%3A%3A%3A%3A%3A%3A&rentPriceMin=0&rentPriceMax=900000000&priceMin=0&priceMax=900000000&areaMin=0&areaMax=900000000&showArticle=false&sameAddressGroup=false&priceType=RETAIL&page='+page;
        let j; try{ const r=await fetch(u,{headers:{'Authorization':AUTH,'Referer':location.href}}); j=await r.json(); }catch(e){ break; }
        for(const a of (j.articleList||[])) out.push(a);
        if(!j.isMoreData) break;
      }
    }
    return JSON.stringify(out);
  })()`);
  const arr = JSON.parse(got||'[]');
  for(const a of arr){ a._dong=name; raw.push(a); }
  console.error(`  ${name}: ${arr.length}`);
}

// dedup by articleNo
const byNo=new Map(); for(const a of raw){ if(!byNo.has(a.articleNo)) byNo.set(a.articleNo,a); }
let items=[...byNo.values()];
console.error(`수집 ${raw.length} → dedup ${items.length}`);

// 필터: 전용면적 + 최근접역 + 이북제외
items = items.filter(a=>{
  const m2=Number(a.area2)||0, lat=Number(a.latitude), lng=Number(a.longitude);
  if(!(m2>=MIN_M2) || !lat) return false;
  if(lat>=NORTH) return false;
  if(nearestStation(lat,lng)>MAX_DIST) return false;
  return true;
});
console.error(`면적·거리 필터 후 ${items.length}`);

// ---------- 상세 조회로 방개수 (survivor만) ----------
const nos=items.map(a=>a.articleNo);
const roomOf={};
for(let i=0;i<nos.length;i+=30){
  const chunk=nos.slice(i,i+30);
  const res=await inpage(`(async()=>{
    const AUTH=${AUTH}, out={};
    for(const no of ${JSON.stringify(chunk)}){
      try{ const r=await fetch('/api/articles/'+no+'?complexNo=',{headers:{'Authorization':AUTH,'Referer':location.href}});
        const j=await r.json(); const d=j.articleDetail||{};
        out[no]={room:d.roomCount??'', bath:d.bathroomCount??'', move:(d.moveInTypeName||''), addr:(d.exposureAddress||d.divisionName||'')};
      }catch(e){ out[no]={}; }
    }
    return JSON.stringify(out);
  })()`);
  Object.assign(roomOf, JSON.parse(res||'{}'));
}
child.kill('SIGKILL');

// ---------- 정규화 ----------
const rows = items.map(a=>{
  const lat=Number(a.latitude), lng=Number(a.longitude);
  const [cd,cv]=commute(lat,lng);
  const dep=parseWon(a.dealOrWarrantPrc), rent=parseWon(a.rentPrc);
  const det=roomOf[a.articleNo]||{};
  return {
    no:a.articleNo, sales:a.tradeTypeName, type:a.realEstateTypeName,
    dep, rent, priceStr:a.dealOrWarrantPrc + (rent?(' / '+a.rentPrc):''),
    m2:Number(a.area2)||0, supply:Number(a.area1)||0, floor:a.floorInfo||'',
    bldg:(a.buildingName&&a.buildingName!=='빌라'&&a.buildingName!=='단독/다가구')?a.buildingName:'',
    room:det.room||'', bath:det.bath||'', move:det.move||'',
    dong:a._dong, cd, cv, confirm:a.articleConfirmYmd||'',
    tags:(a.tagList||[]).slice(0,5), desc:a.articleFeatureDesc||'',
    img:a.representativeImgUrl?IMG_HOST+a.representativeImgUrl:'',
    directTrade:a.isDirectTrade, sameCnt:a.sameAddrCnt||1,
    link:`https://new.land.naver.com/houses?articleNo=${a.articleNo}`
  };
}).sort((x,y)=>x.cd-y.cd);

const nJ=rows.filter(r=>r.sales==='전세').length, nW=rows.filter(r=>r.sales==='월세').length;
const now=new Date(Date.now()+9*3600*1000).toISOString().replace('T',' ').slice(0,16);
console.error(`\n최종 ${rows.length}건 · 전세 ${nJ} / 월세 ${nW} · 투룸+ ${rows.filter(r=>Number(r.room)>=2).length}`);

// ---------- HTML ----------
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const py=m2=>(m2/3.3058).toFixed(1);
const card=r=>{
  const price = r.sales==='전세' ? '전세 '+won(r.dep) : '월세 '+won(r.dep)+'/'+won(r.rent);
  const roomBadge = r.room?`방${r.room}`:'';
  const isOneroom = r.room && Number(r.room)<2;
  return `<a class="card${isOneroom?' oneroom':''}" data-sales="${r.sales}" data-cd="${r.cd}" data-m2="${r.m2}" data-room="${r.room||0}" href="${r.link}" target="_blank" rel="noopener">
   ${r.img?`<img loading="lazy" src="${esc(r.img)}">`:'<div class="noimg">사진없음</div>'}
   <div class="body">
    <div class="price">${esc(price)}</div>
    <div class="meta">${r.m2}㎡ (${py(r.m2)}평) · ${esc(r.floor)}층 ${roomBadge?'· '+roomBadge:''}${r.bath?'/욕'+r.bath:''}</div>
    <div class="cm">🚶 ${r.cd}m ${esc(r.cv)} · ${esc(r.dong)}</div>
    ${r.bldg?`<div class="bldg">${esc(r.bldg)}</div>`:''}
    <div class="tags">${r.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div>
    ${r.desc?`<div class="desc">${esc(r.desc.slice(0,60))}</div>`:''}
    <div class="foot">${esc(r.confirm.replace(/(\d{4})(\d{2})(\d{2})/,'$1.$2.$3'))} 확인${r.sameCnt>1?' · 동일주소 '+r.sameCnt:''}${r.directTrade?' · 직거래':''}</div>
   </div></a>`;
};
const html=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>네이버 방매물</title><style>
:root{--bg:#f5f6f8;--card:#fff;--tx:#1a1a1a;--sub:#666;--line:#e3e5e8;--accent:#00c73c}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--card:#22252b;--tx:#e8eaed;--sub:#9aa0a6;--line:#33373d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 -apple-system,'Malgun Gothic',sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px;z-index:5}
h1{font-size:16px;margin:0 0 6px}.ctl{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
button{border:1px solid var(--line);background:var(--card);color:var(--tx);border-radius:16px;padding:5px 12px;font-size:13px;cursor:pointer}
button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
label{font-size:13px;color:var(--sub);display:flex;gap:4px;align-items:center;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;padding:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;display:flex;flex-direction:column}
.card img,.noimg{width:100%;height:150px;object-fit:cover;background:#0002}
.noimg{display:flex;align-items:center;justify-content:center;color:var(--sub);font-size:12px}
.body{padding:8px 10px;display:flex;flex-direction:column;gap:3px}
.price{font-weight:700;font-size:15px}.meta{font-size:12.5px;color:var(--sub)}
.cm{font-size:12.5px;color:var(--accent);font-weight:600}.bldg{font-size:12px}
.tags{display:flex;gap:3px;flex-wrap:wrap;margin-top:2px}.tags span{font-size:10.5px;background:#0001;padding:1px 6px;border-radius:4px;color:var(--sub)}
@media(prefers-color-scheme:dark){.tags span{background:#fff1}}
.desc{font-size:11.5px;color:var(--sub)}.foot{font-size:10.5px;color:var(--sub);margin-top:2px}
.card.hidden{display:none}
</style>
<header>
 <h1>🟢 네이버 통근권 매물 · ${now} · ${rows.length}건</h1>
 <div class="ctl">
  <button id="bJ" class="on" onclick="setSales('전세',this)">전세 <span id="cJ"></span></button>
  <button id="bW" onclick="setSales('월세',this)">월세 <span id="cW"></span></button>
  <span style="flex:1"></span>
  <label><input type="checkbox" id="chkOne" onchange="render()"> 원룸(방1) 포함</label>
  <button id="sortBtn" onclick="toggleSort(this)">통근순</button>
 </div>
</header>
<div class="grid" id="grid">${rows.map(card).join('\n')}</div>
<script>
let sales='전세', sortM2=false;
function setSales(s,b){sales=s;document.getElementById('bJ').classList.toggle('on',s==='전세');document.getElementById('bW').classList.toggle('on',s==='월세');render();}
function toggleSort(b){sortM2=!sortM2;b.textContent=sortM2?'면적순':'통근순';render();}
function render(){
 const showOne=document.getElementById('chkOne').checked;
 let cj=0,cw=0;
 const cards=[...document.querySelectorAll('.card')];
 for(const c of cards){
  const oneroom=c.classList.contains('oneroom');
  const ok=(!oneroom||showOne);
  if(ok){ if(c.dataset.sales==='전세')cj++; else cw++; }
  c.classList.toggle('hidden',!(c.dataset.sales===sales&&ok));
 }
 document.getElementById('cJ').textContent=cj;document.getElementById('cW').textContent=cw;
 const vis=cards.filter(c=>!c.classList.contains('hidden'));
 const g=document.getElementById('grid');
 vis.sort((a,b)=>sortM2?(b.dataset.m2-a.dataset.m2):(a.dataset.cd-b.dataset.cd)).forEach(c=>g.appendChild(c));
}
document.addEventListener('DOMContentLoaded',render);
</script>`;
fs.writeFileSync(OUT, html);
console.error(`\n대시보드: ${OUT}`);
process.exit(0);
