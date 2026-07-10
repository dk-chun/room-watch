#!/usr/bin/env node
// 네이버 부동산 통근권 매물 수집 (로컬 온디맨드 전용).
// GitHub Actions(Azure IP)는 네이버가 ERR_CONNECTION_RESET으로 네트워크 차단 → CI 불가.
// 로컬(가정용 IP)에서 헤드리스 크롬 CDP로 앱 토큰 가로채 in-page fetch.
// 실행: node naver_fetch.mjs  →  바탕화면 방매물_네이버.html 생성
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const _HERE = path.dirname(fileURLToPath(import.meta.url));

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

// ===== 실거래 대조 (국토부 data.go.kr) — 직방과 동일 로직, 매칭키만 지번→건물명 =====
const MOLIT_KEY = (process.env.MOLIT_KEY || (fs.existsSync(_HERE+'/.molit_key') ? fs.readFileSync(_HERE+'/.molit_key','utf8') : '')).trim();
const RT_CACHE = _HERE+'/rt_cache';
const RT_MONTHS = 12, RT_CONV = 0.055;
const DONG_GU = { '역삼동':'11680','도곡동':'11680','논현동':'11680','대치동':'11680','삼성동':'11680',
                  '서초동':'11650','양재동':'11650','우면동':'11650','반포동':'11650' };
const num = v => { const n=parseInt(String(v==null?'':v).replace(/,/g,'').trim()); return isNaN(n)?0:n; };
const arOf = x => { const a=parseFloat(x.excluUseAr||x.totalFloorAr); return isNaN(a)?null:a; };
const isJeonse = x => num(x.monthlyRent)===0;
const valOf = (dep,rent,jeonse) => jeonse ? dep : Math.round(dep + rent*12/RT_CONV);
const norm = s => String(s||'').replace(/\s|·|\(.*?\)|오피스텔|빌라/g,'').toLowerCase();
const median = a => { const s=[...a].sort((x,y)=>x-y),n=s.length; return n?(n%2?s[(n-1)/2]:(s[n/2-1]+s[n/2])/2):0; };
function recentMonths(n){ const d=new Date(Date.now()+9*3600*1000); let y=d.getUTCFullYear(),m=d.getUTCMonth()+1,out=[];
  for(let i=0;i<n;i++){ out.push(`${y}${String(m).padStart(2,'0')}`); if(--m===0){m=12;y--;} } return out; }
async function molitLoad(api,short,gu,ym,fresh){
  fs.mkdirSync(RT_CACHE,{recursive:true});
  const p=`${RT_CACHE}/${short}_${gu}_${ym}.json`;
  if(!fresh.has(ym) && fs.existsSync(p)){ try{ return JSON.parse(fs.readFileSync(p,'utf8')); }catch{} }
  let its=[];
  try{
    const u=`https://apis.data.go.kr/1613000/${api}/get${api}?serviceKey=${encodeURIComponent(MOLIT_KEY)}&LAWD_CD=${gu}&DEAL_YMD=${ym}&numOfRows=900&_type=json`;
    const j=await (await fetch(u,{headers:{'User-Agent':'Mozilla/5.0'}})).json();
    let it=(((j.response||{}).body||{}).items||{}).item; it=Array.isArray(it)?it:(it?[it]:[]); its=it;
  }catch{}
  fs.writeFileSync(p,JSON.stringify(its));
  return its;
}
function stat(samples,mine,jeonse){
  let vals=samples.map(x=>valOf(num(x.deposit),num(x.monthlyRent),jeonse)).filter(v=>v>0).sort((a,b)=>a-b);
  if(!vals.length) return null;
  const k=vals.length>=10?Math.floor(vals.length/10):0;
  const tv=k?vals.slice(k,vals.length-k):vals;
  const med=median(tv);
  const pct=Math.round(vals.filter(v=>v<mine).length/vals.length*100);
  const hist=samples.map(x=>{ const dy=x.dealYear, dm=x.dealMonth;
      return { ym: dy?`${String(dy).slice(2)}.${String(dm).padStart(2,'0')}`:'?', k: dy?dy*100+Number(dm):0,
               v: valOf(num(x.deposit),num(x.monthlyRent),jeonse), d:num(x.deposit), r:num(x.monthlyRent) }; })
    .filter(h=>h.v>0).sort((a,b)=>b.k-a.k).slice(0,14);
  return { n:vals.length, med, lo:tv[0], hi:tv[tv.length-1], diff: med?Math.round((mine-med)/med*100):0, pct, hist };
}
async function attachRealprice(rows){
  const months=recentMonths(RT_MONTHS), fresh=new Set(months.slice(0,2));
  const gus=[...new Set(rows.map(r=>DONG_GU[r.dong]).filter(Boolean))];
  const offiDong={}, rhDong={}, shDong={};   // key: gu|dong → [records]
  const push=(m,k,x)=>{ (m[k]=m[k]||[]).push(x); };
  for(const gu of gus) for(const ym of months){
    for(const x of await molitLoad('RTMSDataSvcOffiRent','offi',gu,ym,fresh)) push(offiDong,gu+'|'+x.umdNm,x);
    for(const x of await molitLoad('RTMSDataSvcRHRent','rh',gu,ym,fresh)) push(rhDong,gu+'|'+x.umdNm,x);
    for(const x of await molitLoad('RTMSDataSvcSHRent','sh',gu,ym,fresh)) push(shDong,gu+'|'+x.umdNm,x);
  }
  for(const r of rows){
    r.rt=null;
    const gu=DONG_GU[r.dong]; if(!gu) continue;
    const jeonse=r.sales==='전세', mine=valOf(r.dep,r.rent,jeonse);
    const isOffi=r.type==='오피스텔';
    const dk=gu+'|'+r.dong;
    const dongArr=isOffi?(offiDong[dk]||[]):[...(rhDong[dk]||[]),...(shDong[dk]||[])];
    // 1) 건물명 매칭 (오피=offiNm, 빌라=mhouseNm)
    let comps=[], bldgName=null;
    if(r.bldgName){
      const nb=norm(r.bldgName);
      if(nb.length>=2){
        const nameArr=isOffi?(offiDong[dk]||[]):(rhDong[dk]||[]);
        comps=nameArr.filter(x=>{ const nm=norm(x.offiNm||x.mhouseNm); return nm && (nm===nb || nm.includes(nb) || nb.includes(nm)); });
        if(comps.length) bldgName=(comps[0].offiNm||comps[0].mhouseNm||'').trim();
      }
    }
    const ct=comps.filter(x=>isJeonse(x)===jeonse);
    const near=ct.filter(x=>arOf(x)!=null && Math.abs(arOf(x)-r.m2)<=3);
    let typed=null, tier=null;
    if(near.length){ typed=near; tier='building'; }
    else if(ct.length){ typed=ct; tier='building-any'; }
    else {
      const pool=dongArr.filter(x=>isJeonse(x)===jeonse);
      for(const [tol,tg] of [[5,'area'],[15,'area-wide']]){
        const cand=pool.filter(x=>arOf(x)!=null && Math.abs(arOf(x)-r.m2)<=tol);
        if(cand.length>=3){ typed=cand; tier=tg; break; }
      }
      if(!typed && pool.length){ typed=pool; tier='dong'; }
    }
    if(!typed) continue;
    const s=stat(typed,mine,jeonse);
    if(s){ s.tier=tier; s.bldg=bldgName; r.rt=s; }
  }
}

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

async function inpage(code, capMs=120000){
  const ev=cdp(ws,'Runtime.evaluate',{expression:code,awaitPromise:true,returnByValue:true});
  const to=new Promise((_,rej)=>setTimeout(()=>rej(new Error('evaluate timeout')),capMs));
  const r=await Promise.race([ev,to]); return r.result.value;
}
const AUTH = JSON.stringify(token);
// in-page fetch에 개별 타임아웃(요청 하나가 멈춰도 배치 전체가 안 죽게) — 각 evaluate 앞에 주입
const TF = `const tf=(u)=>{const c=new AbortController();const t=setTimeout(()=>c.abort(),12000);return fetch(u,{headers:{'Authorization':AUTH,'Referer':location.href},signal:c.signal}).finally(()=>clearTimeout(t));};`;

// ---------- 수집 (동별로 in-page 페이지네이션) ----------
const raw=[];
for(const [code,name] of Object.entries(DONGS)){
  let arr=[];
  try{
    const got = await inpage(`(async()=>{
      const AUTH=${AUTH}, out=[]; ${TF}
      const sleep=ms=>new Promise(r=>setTimeout(r,ms));
      for(const R of ${JSON.stringify(TYPES)}) for(const [T] of ${JSON.stringify(TRADES)}){
        for(let page=1; page<=15; page++){
          const u='/api/articles?cortarNo=${code}&order=rank&realEstateType='+R+'&tradeType='+T+'&tag=%3A%3A%3A%3A%3A%3A%3A%3A&rentPriceMin=0&rentPriceMax=900000000&priceMin=0&priceMax=900000000&areaMin=0&areaMax=900000000&showArticle=false&sameAddressGroup=false&priceType=RETAIL&page='+page;
          let j=null;
          for(let attempt=0; attempt<3 && !j; attempt++){         // 429 등 실패 시 페이지 유실 방지: 재시도
            try{ const r=await tf(u); if(r.status===429){ await sleep(600); continue; } j=await r.json(); }
            catch(e){ await sleep(400); }
          }
          if(!j) break;
          for(const a of (j.articleList||[])) out.push(a);
          if(!j.isMoreData) break;
        }
      }
      return JSON.stringify(out);
    })()`);
    arr = JSON.parse(got||'[]');
  }catch(e){ console.error(`  ${name}: 실패(${String(e.message||e).slice(0,40)}) — 스킵`); }
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

// ---------- 물리적 단위로 묶기 (중개사 중복 게시 합치기) ----------
// 같은 집(좌표+전용+층+전월세)을 여러 중개사가 각자 올림 → articleNo만 다름. 묶어서 한 카드로.
const units=new Map();
for(const a of items){
  const lat=Number(a.latitude), lng=Number(a.longitude);
  const dep=parseWon(a.dealOrWarrantPrc), rent=parseWon(a.rentPrc);
  const key=[lat.toFixed(4),lng.toFixed(4),a.area2,a.floorInfo,a.tradeTypeName].join('|');
  const m={ a, lat, lng, dep, rent, confirm:a.articleConfirmYmd||'' };
  if(!units.has(key)) units.set(key,[]);
  units.get(key).push(m);
}
console.error(`중복 병합: ${items.length} → 단위 ${units.size}`);

// 대표 매물(최신확인·사진있음)만 상세조회
const reps=[...units.values()].map(ms=>ms.slice().sort((x,y)=>
  (y.confirm.localeCompare(x.confirm)) || ((y.a.representativeImgUrl?1:0)-(x.a.representativeImgUrl?1:0)))[0]);
const roomOf={};
const repNos=reps.map(r=>r.a.articleNo);
for(let i=0;i<repNos.length;i+=30){
  const chunk=repNos.slice(i,i+30);
  try{
    const res=await inpage(`(async()=>{
      const AUTH=${AUTH}, out={}; ${TF}
      await Promise.all(${JSON.stringify(chunk)}.map(async no=>{
        try{ const r=await tf('/api/articles/'+no+'?complexNo=');
          const j=await r.json(); const d=j.articleDetail||{};
          out[no]={room:d.roomCount??'', bath:d.bathroomCount??'', move:(d.moveInTypeName||''), apt:(d.aptName||''), dbldg:(d.buildingName||'')};
        }catch(e){ out[no]={}; }
      }));
      return JSON.stringify(out);
    })()`);
    Object.assign(roomOf, JSON.parse(res||'{}'));
  }catch(e){ console.error(`  상세 청크 ${i} 실패 — 스킵`); }
  process.stderr.write(`\r  상세조회 ${Math.min(i+30,repNos.length)}/${repNos.length}`);
}
process.stderr.write('\n');
child.kill('SIGKILL');

// ---------- 단위별 정규화 ----------
const rows = [...units.values()].map(ms=>{
  const rep=ms.slice().sort((x,y)=>(y.confirm.localeCompare(x.confirm))||((y.a.representativeImgUrl?1:0)-(x.a.representativeImgUrl?1:0)))[0];
  const a=rep.a, [cd,cv]=commute(rep.lat,rep.lng);
  const depMin=Math.min(...ms.map(m=>m.dep)), depMax=Math.max(...ms.map(m=>m.dep));
  const rentMin=Math.min(...ms.map(m=>m.rent)), rentMax=Math.max(...ms.map(m=>m.rent));
  const det=roomOf[a.articleNo]||{};
  // 건물명: 오피=aptName, 빌라=상세 buildingName(실명일 때만). 실거래 매칭키.
  const generic=s=>!s||['빌라','단독/다가구','오피스텔','주택','원룸','투룸'].includes(s);
  const bldgName = det.apt || (!generic(det.dbldg)?det.dbldg:'') || '';
  return {
    no:a.articleNo, sales:a.tradeTypeName, type:a.realEstateTypeName,
    dep:depMax, rent:rentMax, depMin, depMax, rentMin, rentMax,   // 필터는 현실가(max) 기준
    m2:Number(a.area2)||0, floor:a.floorInfo||'', bldgName,
    room:det.room||'', bath:det.bath||'',
    dong:a._dong, cd, cv, confirm:rep.confirm, cnt:ms.length,
    tags:(a.tagList||[]).slice(0,5), desc:a.articleFeatureDesc||'',
    img:a.representativeImgUrl?IMG_HOST+a.representativeImgUrl:'',
    link:`https://new.land.naver.com/houses?articleNo=${a.articleNo}`
  };
}).sort((x,y)=>x.cd-y.cd);

// ---------- 실거래 대조 ----------
await attachRealprice(rows);

const nJ=rows.filter(r=>r.sales==='전세').length, nW=rows.filter(r=>r.sales==='월세').length;
const now=new Date(Date.now()+9*3600*1000).toISOString().replace('T',' ').slice(0,16);
console.error(`\n최종 ${rows.length}단위 · 전세 ${nJ} / 월세 ${nW} · 투룸+ ${rows.filter(r=>Number(r.room)>=2).length}`);
console.error(`실거래 매칭: ${rows.filter(r=>r.rt).length}/${rows.length} (건물 ${rows.filter(r=>r.rt&&r.rt.tier.startsWith('building')).length} · 동네 ${rows.filter(r=>r.rt&&!r.rt.tier.startsWith('building')).length})`);

// ---------- HTML ----------
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const py=m2=>(m2/3.3058).toFixed(1);
function rtLine(r){
  const s=r.rt; if(!s) return {html:'', rtdiff:9999};
  const rtdiff=s.pct-50;   // 낮을수록 저평가(싼 편)
  let cls='rt', txt;
  if(s.tier.startsWith('building')){
    const sign=s.diff>=0?'+':'';
    txt=`🏢 ${esc(s.bldg||r.bldgName||'같은건물')} 실거래 ${won(s.med)}(${s.n})${s.tier==='building-any'?' 타평형':''} <b>${sign}${s.diff}%</b>`;
    cls += s.diff<=-8?' cheap':(s.diff>=8?' pricey':'');
  } else {
    const lo=s.pct<=50, p=lo?s.pct:100-s.pct;
    txt=`📊 동네 ${r.m2}㎡ 실거래 ${lo?`하위 ${p}% (싼 편)`:`상위 ${p}% (비싼 편)`} (${s.n}건)·참고`;
    cls += s.pct<=30?' cheap':(s.pct>=70?' pricey':'');
  }
  return {html:`<div class="${cls}">${txt}</div>`, rtdiff};
}
const card=r=>{
  let price;
  if(r.sales==='전세') price = r.depMin===r.depMax ? '전세 '+won(r.depMax) : '전세 '+won(r.depMin)+'~'+won(r.depMax);
  else { const dp=won(r.depMax), rt = r.rentMin===r.rentMax?won(r.rentMax):won(r.rentMin)+'~'+won(r.rentMax); price='월세 '+dp+'/'+rt; }
  const roomBadge = r.room?`방${r.room}`:'';
  const isOneroom = r.room && Number(r.room)<2;
  const rl=rtLine(r);
  return `<a class="card${isOneroom?' oneroom':''}" data-sales="${r.sales}" data-cd="${r.cd}" data-m2="${r.m2}" data-room="${r.room||0}" data-dep="${r.depMax}" data-rent="${r.rentMax}" data-rtdiff="${rl.rtdiff}" href="${r.link}" target="_blank" rel="noopener">
   ${r.img?`<img loading="lazy" src="${esc(r.img)}">`:'<div class="noimg">사진없음</div>'}
   <div class="body">
    <div class="price">${esc(price)}</div>
    <div class="meta">${r.m2}㎡ (${py(r.m2)}평) · ${esc(r.floor)}층 ${roomBadge?'· '+roomBadge:''}${r.bath?'/욕'+r.bath:''}</div>
    <div class="cm">🚶 ${r.cd}m ${esc(r.cv)} · ${esc(r.dong)}</div>
    ${rl.html}
    <div class="tags">${r.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div>
    ${r.desc?`<div class="desc">${esc(r.desc.slice(0,60))}</div>`:''}
    <div class="foot">${esc(r.confirm.replace(/(\d{4})(\d{2})(\d{2})/,'$1.$2.$3'))} 확인${r.cnt>1?' · 중개 '+r.cnt+'곳':''}</div>
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
.sorts{font-size:12px;color:var(--sub);display:flex;gap:4px;align-items:center}
.sortb{padding:4px 10px;font-size:12px}
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
.rt{font-size:11.5px;color:var(--sub);margin-top:1px}.rt b{font-weight:700}
.rt.cheap{color:#12b886;font-weight:600}.rt.pricey{color:#fa5252}
.card.hidden{display:none}
input.cap{width:52px;border:1px solid var(--line);background:var(--card);color:var(--tx);border-radius:6px;padding:4px 6px;font-size:13px}
</style>
<header>
 <h1>🟢 네이버 통근권 매물 · ${now} · ${rows.length}단위<span style="font-weight:400;color:var(--sub);font-size:12px"> (중복 병합됨)</span></h1>
 <div class="ctl">
  <button id="bJ" class="on" onclick="setSales('전세',this)">전세 <span id="cJ"></span></button>
  <button id="bW" onclick="setSales('월세',this)">월세 <span id="cW"></span></button>
  <span id="capJ" class="capbox">전세 상한 <input class="cap" id="jMax" type="number" placeholder="3.5" oninput="render()">억</span>
  <span id="capW" class="capbox" style="display:none">보증금<input class="cap" id="wDep" type="number" placeholder="만" oninput="render()"> 월<input class="cap" id="wRent" type="number" placeholder="만" oninput="render()"></span>
  <span style="flex:1"></span>
  <label><input type="checkbox" id="chkOne" onchange="render()"> 원룸 포함</label>
  <span class="sorts">정렬
   <button class="sortb on" onclick="setSort('cd',this)">통근순</button>
   <button class="sortb" onclick="setSort('rtdiff',this)">저평가순</button>
   <button class="sortb" onclick="setSort('m2',this)">면적순</button>
  </span>
 </div>
</header>
<div class="grid" id="grid">${rows.map(card).join('\n')}</div>
<script>
let sales='전세', sortMode='cd';   // cd(통근) / rtdiff(저평가) / m2(면적)
function setSales(s,b){sales=s;document.getElementById('bJ').classList.toggle('on',s==='전세');document.getElementById('bW').classList.toggle('on',s==='월세');
 document.getElementById('capJ').style.display=s==='전세'?'':'none';document.getElementById('capW').style.display=s==='월세'?'':'none';render();}
function setSort(k,b){sortMode=k;document.querySelectorAll('.sortb').forEach(x=>x.classList.toggle('on',x===b));render();}
function render(){
 const showOne=document.getElementById('chkOne').checked;
 const jMax=parseFloat(document.getElementById('jMax').value)*10000||Infinity;
 const wDep=parseFloat(document.getElementById('wDep').value)||Infinity;
 const wRent=parseFloat(document.getElementById('wRent').value)||Infinity;
 let cj=0,cw=0;
 const cards=[...document.querySelectorAll('.card')];
 for(const c of cards){
  const isJ=c.dataset.sales==='전세';
  const oneroom=c.classList.contains('oneroom');
  const capOK=isJ?(+c.dataset.dep<=jMax):(+c.dataset.dep<=wDep&&+c.dataset.rent<=wRent);
  const base=(!oneroom||showOne);
  if(base&&capOK){ if(isJ)cj++; else cw++; }
  c.classList.toggle('hidden',!(isJ===(sales==='전세')&&base&&capOK));
 }
 document.getElementById('cJ').textContent=cj;document.getElementById('cW').textContent=cw;
 const vis=cards.filter(c=>!c.classList.contains('hidden'));
 const g=document.getElementById('grid');
 const key=c=>sortMode==='m2'?-(+c.dataset.m2):(+c.dataset[sortMode]);
 vis.sort((a,b)=>key(a)-key(b)).forEach(c=>g.appendChild(c));
}
document.addEventListener('DOMContentLoaded',render);
</script>`;
fs.writeFileSync(OUT, html);
console.error(`\n대시보드: ${OUT}`);
process.exit(0);
