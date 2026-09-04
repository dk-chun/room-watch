#!/usr/bin/env python3
"""직방 통근권 매물 재수집 + 직전 스냅샷 대비 diff.
실행: python3 fetch_listings.py
- stdout: diff 리포트(JSON)
- snapshots/<ts>.json 저장(비교 기준, 지우지 말 것)
"""
import urllib.request, urllib.parse, json, math, os, glob, statistics, time, gzip
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

KST = timezone(timedelta(hours=9))   # CI 러너는 UTC라 명시하지 않으면 9시간 어긋남

# ===== CONFIG (조건 바꾸려면 여기만 수정) =====
STATIONS = {'신논현': (37.50466, 127.02503), '강남': (37.49794, 127.02762),
            '양재': (37.48453, 127.03411), '양재숲': (37.47012, 127.03856),
            # 확장(2026-07-11): 셔틀 접근권 + 회사 서쪽(더 쌈). 통근시간은 ODsay로 매물별 실측
            '서초': (37.4917, 127.0078), '교대': (37.4935, 127.0143),
            '남부터미널': (37.4849, 127.0165), '매봉': (37.4870, 127.0466),
            '도곡': (37.4909, 127.0553), '방배': (37.4814, 126.9975),
            # 2차 확장(2026-07-11): 강남·서초 밖 가격계단. 셔틀 직결축만.
            # 신분당선 남쪽(양재 셔틀 직통) — 남쪽일수록 전세 쌈
            '판교': (37.3947, 127.1112), '정자': (37.3670, 127.1082),
            '수지구청': (37.3221, 127.0948), '광교중앙': (37.2872, 127.0543),
            # 2호선 서쪽(강남 서초사옥 셔틀 직통) — 관악
            '사당': (37.4765, 126.9816), '서울대입구': (37.4812, 126.9527), '신림': (37.4842, 126.9298)}
SHUTTLE = {'강남셔틀': (37.496517, 127.026931),    # 삼성 서초사옥 (서초대로74길 11)
           '양재셔틀': (37.486832, 127.032575),    # 양재역 2번출구 · 호텔 페이토 앞
           '양재숲셔틀': (37.470165, 127.038620)}  # 양재시민의숲역
OFFICE = (37.4666247, 127.0230600)        # 삼성 서울R&D캠퍼스 (서초구 성촌길 56, 우면동)
MIN_M2 = 29.75                            # 전용면적 하한(㎡) = 9평. 둘이 거주 기준
MAX_DIST = 1200                           # 역 최근접 거리 컷(m)
EXCLUDE_NORTH_LAT = 37.500                # 이 위도 이북(신논현 위쪽) 제외; None이면 미적용
# 빌라(투룸·쓰리룸 본진) 포함. 아파트는 단지 기반 별구조라 제외
# v2/items/* 는 2026-07-23 직방이 폐기(404) → house/property/v1 로 이전. 응답 키도 itemId→id
ENDPOINTS = ['house/property/v1/items/onerooms', 'house/property/v1/items/officetels',
             'house/property/v1/items/villas']
_HERE = os.path.dirname(os.path.abspath(__file__))
# 출력 경로: 로컬은 바탕화면, CI는 DASHBOARD_PATH 환경변수
DASHBOARD = os.environ.get('DASHBOARD_PATH', '/mnt/c/Users/10x100/Desktop/방매물.html')
# --- 실거래 대조 (국토부 data.go.kr + 직방 bls) ---
def _molit_key():   # CI는 env(Secrets), 로컬은 .molit_key 파일(gitignore)
    k = os.environ.get('MOLIT_KEY')
    if k: return k.strip()
    p = os.path.join(_HERE, '.molit_key')
    return open(p).read().strip() if os.path.exists(p) else ''
MOLIT_KEY = _molit_key()
RT_MONTHS = 12                            # 실거래 조회 개월수
RT_CONV = 0.055                           # 전월세 전환율(월세→전세환산)
BURDEN_RATE = 0.03                        # 실질부담 계산용 보증금 기회비용(연리): 월세+관리비+보증금×3%÷12
DONG_GU = {'역삼동': '11680', '논현동': '11680', '도곡동': '11680', '개포동': '11680',
           '대치동': '11680', '삼성동': '11680', '청담동': '11680', '신사동': '11680',
           '서초동': '11650', '양재동': '11650', '반포동': '11650', '우면동': '11650',
           '방배동': '11650', '잠원동': '11650', '내곡동': '11650'}
RT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rt_cache')
# --- 통근시간 (ODsay 대중교통 API) ---
def _odsay_key():   # CI는 env(Secrets), 로컬은 .odsay_key 파일(gitignore)
    k = os.environ.get('ODSAY_KEY')
    if k: return k.strip()
    p = os.path.join(_HERE, '.odsay_key')
    return open(p).read().strip() if os.path.exists(p) else ''
ODSAY_KEY = _odsay_key()
ODSAY_REF = 'https://dk-chun.github.io'    # 키가 이 도메인에 묶여 있어 Referer 필수
ODSAY_CACHE = os.path.join(_HERE, 'odsay_cache')
WALK_MPM = 70                              # 직선 m ÷ 70 ≈ 도보 분(우회 1.25배 감안)
# ==============================================

UA = {'User-Agent': 'Mozilla/5.0'}
BASE32 = '0123456789bcdefghjkmnpqrstuvwxyz'
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'snapshots')
SVC_PATH = {'원룸': 'oneroom', '오피스텔': 'officetel', '빌라': 'villa'}

def gh_encode(lat, lon, prec=6):
    lat_r, lon_r, gh, bit, ch, even = [-90., 90.], [-180., 180.], [], 0, 0, True
    while len(gh) < prec:
        if even:
            mid = (lon_r[0] + lon_r[1]) / 2
            if lon > mid: ch |= (1 << (4 - bit)); lon_r[0] = mid
            else: lon_r[1] = mid
        else:
            mid = (lat_r[0] + lat_r[1]) / 2
            if lat > mid: ch |= (1 << (4 - bit)); lat_r[0] = mid
            else: lat_r[1] = mid
        even = not even
        if bit < 4: bit += 1
        else: gh.append(BASE32[ch]); bit = 0; ch = 0
    return ''.join(gh)

def gh_bbox(gh):
    lat_r, lon_r, even = [-90., 90.], [-180., 180.], True
    for c in gh:
        cd = BASE32.index(c)
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_r[0] + lon_r[1]) / 2; lon_r[0 if cd & mask else 1] = mid
            else:
                mid = (lat_r[0] + lat_r[1]) / 2; lat_r[0 if cd & mask else 1] = mid
            even = not even
    return lat_r, lon_r

def neighbors(gh):
    (la0, la1), (lo0, lo1) = gh_bbox(gh)
    latc, lonc, dlat, dlon = (la0+la1)/2, (lo0+lo1)/2, la1-la0, lo1-lo0
    return [gh_encode(latc+i*dlat, lonc+j*dlon, len(gh)) for i in (-1,0,1) for j in (-1,0,1)]

def hav(a, b, c, d):
    R, p = 6371000, math.pi/180
    x = math.sin((c-a)*p/2)**2 + math.cos(a*p)*math.cos(c*p)*math.sin((d-b)*p/2)**2
    return 2*R*math.asin(math.sqrt(x))

def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))

def commute(lat, lng):
    d = [(round(hav(lat, lng, OFFICE[0], OFFICE[1])), '회사도보')]
    d += [(round(hav(lat, lng, s[0], s[1])), n) for n, s in SHUTTLE.items()]
    return min(d)

def odsay_raw(olat, olng, dlat, dlng):   # 출발→도착 대중교통 시간(분). 파일캐시(출발 3자리≈110m 격자)
    os.makedirs(ODSAY_CACHE, exist_ok=True)
    p = f'{ODSAY_CACHE}/{round(olat,3)}_{round(olng,3)}__{round(dlat,3)}_{round(dlng,3)}.json'
    if os.path.exists(p):
        try: return json.load(open(p))
        except Exception: pass
    out = {'min': None}
    if ODSAY_KEY:
        url = (f'https://api.odsay.com/v1/api/searchPubTransPathT?SX={olng}&SY={olat}&EX={dlng}&EY={dlat}'
               f'&apiKey={urllib.parse.quote(ODSAY_KEY)}')
        try:
            req = urllib.request.Request(url, headers={'Referer': ODSAY_REF, 'User-Agent': 'Mozilla/5.0'})
            d = json.load(urllib.request.urlopen(req, timeout=15))
            if d.get('result', {}).get('path'):
                i = d['result']['path'][0]['info']
                out = {'min': round(i['totalTime']), 'walk': round(i.get('totalWalk', 0))}
            elif (d.get('error') or {}).get('code') == '-98':   # 700m내 = 대중교통 대상아님(정상). 캐시해 재호출 방지
                out = {'min': None, 'near': 1}
        except Exception: pass
        if out.get('min') is not None or out.get('near'):       # 성공/700m만 캐시. 일시적 실패(429 등)는 캐시 안 함→다음 실행 재시도
            json.dump(out, open(p, 'w'))
        time.sleep(0.2)                  # 초당 호출 제한 회피
    return out

def transit(lat, lng):   # min(회사도보, 최근접셔틀 대중교통). 계산 실패 먼 매물은 미확정(None)
    cands = []
    od = hav(lat, lng, OFFICE[0], OFFICE[1])
    if od <= 2500:                       # 회사 도보권일 때만 후보(그 이상 도보통근은 비현실)
        cands.append((round(od / WALK_MPM), '회사도보', 'walk', round(od)))
    nn, s = min(SHUTTLE.items(), key=lambda kv: hav(lat, lng, kv[1][0], kv[1][1]))
    dm = hav(lat, lng, s[0], s[1])
    if dm <= 700:
        cands.append((round(dm / WALK_MPM), nn + '앞 도보', 'walk', round(dm)))
    else:
        r = odsay_raw(lat, lng, s[0], s[1])
        if r.get('min'):                 # ODsay 실패(한도초과 등)면 후보 안 넣음 → 미확정, 다음 실행 재시도
            cands.append((r['min'], nn, 'transit', round(dm)))
    if not cands:
        return {'tmin': None, 'tmode': None, 'ttype': None, 'tdist': None}
    b = min(cands)
    return {'tmin': b[0], 'tmode': b[1], 'ttype': b[2], 'tdist': b[3]}

def collect_ids():
    ghset = set()
    for la, lo in STATIONS.values():
        for g in neighbors(gh_encode(la, lo, 6)): ghset.add(g)
    seen = {}
    # salesTypes 필터는 새 API에서 매물 절반을 흘림(무필터 81 vs 전세∪월세 36 관측)
    # → 무필터로 긁고 전세/월세 구분은 상세(fetch_detail)의 salesType으로 거름
    for gh in ghset:
        for ep in ENDPOINTS:
            url = f'https://apis.zigbang.com/{ep}?geohash={gh}&depositMin=0&rentMin=0'
            try: d = get(url)
            except Exception: continue
            for it in d.get('items', []):
                iid = it.get('id', it.get('itemId'))
                if iid is None or iid in seen: continue
                seen[iid] = {'lat': it.get('lat'), 'lng': it.get('lng')}
    return seen

def fetch_detail(iid, retries=3):
    for attempt in range(retries):   # 간헐 네트워크 실패 시 재시도(누락 → 가짜 신규/빠짐 방지)
        try:
            d = get(f'https://apis.zigbang.com/v3/items/{iid}?domain=zigbang')
            it = d.get('item', d)
            p, ar, fl = it.get('price') or {}, (it.get('area') or {}).get('전용면적M2'), it.get('floor') or {}
            mc, ao = it.get('manageCost') or {}, it.get('addressOrigin') or {}
            la, lo = (it.get('randomLocation') or {}).get('lat'), (it.get('randomLocation') or {}).get('lng')
            return {'id': iid, 'sales': it.get('salesType'), 'deposit': p.get('deposit'), 'rent': p.get('rent'),
                    'm2': ar, 'floor': fl.get('floor'), 'floors': fl.get('allFloors'), 'manage': mc.get('amount'),
                    'svc': it.get('serviceType'), 'room': it.get('roomType'), 'addr': ao.get('localText') or it.get('jibunAddress'),
                    'approve': it.get('approveDate'), 'movein': it.get('moveinDate'), 'title': it.get('title'),
                    'img': it.get('imageThumbnail'), 'pnu': it.get('pnu'), 'lat': la, 'lng': lo}
        except Exception:
            if attempt == retries - 1:
                return None

def link(r):
    return f"https://www.zigbang.com/home/{SVC_PATH.get(r['svc'], 'oneroom')}/items/{r['id']}"

def recent_months(n):
    now = datetime.now(KST); y, m = now.year, now.month; out = []
    for _ in range(n):
        out.append(f'{y}{m:02d}'); m -= 1
        if m == 0: m = 12; y -= 1
    return out

def rt_load(api, short, gu, ym, fresh):   # 국토부 실거래 (과거월 파일캐시, 최근2개월만 갱신)
    os.makedirs(RT_CACHE, exist_ok=True)
    p = f'{RT_CACHE}/{short}_{gu}_{ym}.json'
    if ym not in fresh and os.path.exists(p):
        try: return json.load(open(p))
        except Exception: pass
    try:
        d = get(f'https://apis.data.go.kr/1613000/{api}/get{api}?serviceKey={MOLIT_KEY}&LAWD_CD={gu}&DEAL_YMD={ym}&numOfRows=900&_type=json')
        its = d['response']['body'].get('items', {}); its = its.get('item', []) if isinstance(its, dict) else []
        if isinstance(its, dict): its = [its]
    except Exception:
        its = []
    json.dump(its, open(p, 'w'), ensure_ascii=False)
    return its

def rt_bls(pnu):   # 직방 자체 실거래 (빌라/다세대, pnu 필지 단위)
    try: return get(f'https://apis.zigbang.com/v2/bls/{pnu}/rts').get('rts', [])
    except Exception: return []

def probe_status(iid):   # 빠진 매물이 진짜 나갔나: 직방 재조회 status(open=아직열림 / close=계약·내림 / 삭제)
    try:
        it = get(f'https://apis.zigbang.com/v3/items/{iid}?domain=zigbang')
        it = it.get('item', it)
        return it.get('status') or 'unknown'
    except urllib.error.HTTPError as e:
        return 'deleted' if e.code in (404, 410) else f'http{e.code}'
    except Exception:
        return 'unknown'

def _num(v):
    try: return int(str(v).replace(',', '').strip() or 0)
    except Exception: return 0

def attach_realprice(rows):
    months = recent_months(RT_MONTHS); fresh = set(months[:2])
    gus = {DONG_GU.get((r['addr'] or '').split()[-1] if r['addr'] else '') for r in rows}; gus.discard(None)
    offi_idx, rh_idx = defaultdict(list), defaultdict(list)
    offi_dong, rh_dong, sh_dong = defaultdict(list), defaultdict(list), defaultdict(list)
    for gu in gus:
        for ym in months:
            for x in rt_load('RTMSDataSvcOffiRent', 'offi', gu, ym, fresh):
                offi_idx[(gu, x.get('umdNm'), str(x.get('jibun')))].append(x); offi_dong[(gu, x.get('umdNm'))].append(x)
            for x in rt_load('RTMSDataSvcRHRent', 'rh', gu, ym, fresh):
                rh_idx[(gu, x.get('umdNm'), str(x.get('jibun')))].append(x); rh_dong[(gu, x.get('umdNm'))].append(x)
            for x in rt_load('RTMSDataSvcSHRent', 'sh', gu, ym, fresh):
                sh_dong[(gu, x.get('umdNm'))].append(x)
    orms = [r for r in rows if r['svc'] != '오피스텔' and r.get('pnu')]
    with ThreadPoolExecutor(max_workers=15) as ex:
        bls_map = dict(zip([r['id'] for r in orms], ex.map(lambda r: rt_bls(r['pnu']), orms)))

    def val(dep, rent, jeonse):
        return dep if jeonse else round(dep + rent * 12 / RT_CONV)   # 월세는 전세환산가로 통일
    def is_jeonse(x):
        st = x.get('sales_type')
        return st == '전세' if st else _num(x.get('monthlyRent')) == 0
    def stat(samples, mine, jeonse):
        vals = sorted(val(_num(x.get('deposit') or x.get('보증금액')), _num(x.get('monthlyRent') or x.get('월세금액')), jeonse) for x in samples)
        vals = [v for v in vals if v > 0]
        if not vals: return None
        k = len(vals) // 10 if len(vals) >= 10 else 0     # 상하위 10% 이상치 트림
        tv = vals[k:len(vals) - k] if k else vals
        med = statistics.median(tv)
        pct = round(sum(1 for v in vals if v < mine) / len(vals) * 100)   # 이 매물보다 싼 실거래 비율(하위 백분위)
        return {'n': len(vals), 'med': med, 'lo': tv[0], 'hi': tv[-1],
                'diff': round((mine - med) / med * 100) if med else 0, 'pct': pct}

    for r in rows:
        r['rt'] = None
        dong = (r['addr'] or '').split()[-1] if r['addr'] else ''; gu = DONG_GU.get(dong)
        if not gu: continue
        jeonse = r['sales'] == '전세'
        mine = val(r['deposit'] or 0, r['rent'] or 0, jeonse)
        pnu = r.get('pnu'); jibun = None
        if pnu and len(pnu) >= 19:
            bon = int(pnu[11:15]); bu = int(pnu[15:19]); jibun = f'{bon}-{bu}' if bu else str(bon)
        def _ar(x):
            a = x.get('excluUseAr') or x.get('전용면적') or x.get('totalFloorAr')
            try: return float(a) if a else None
            except Exception: return None
        bldg, tier, comps = None, None, []
        if jibun:                                          # 같은 건물(지번/bls) 실거래
            if r['svc'] == '오피스텔':
                comps = offi_idx.get((gu, dong, jibun), [])
                bldg = next((x.get('offiNm') for x in comps), None)
            else:
                comps = bls_map.get(r['id'], [])
                if not comps:
                    comps = rh_idx.get((gu, dong, jibun), [])
                    bldg = next((x.get('mhouseNm') for x in comps), None)
        ct = [x for x in comps if is_jeonse(x) == jeonse]
        near = [x for x in ct if _ar(x) and abs(_ar(x) - r['m2']) <= 3]
        if near:                                           # 1) 같은 건물 + 같은 평수 (최정밀)
            typed, tier = near, 'building'
        elif ct:                                           # 2) 같은 건물, 다른 평수
            typed, tier = ct, 'building-any'
        else:                                              # 3) 동 단위 폴백 (넉넉히 확대)
            pool = [x for x in (offi_dong.get((gu, dong), []) if r['svc'] == '오피스텔'
                    else rh_dong.get((gu, dong), []) + sh_dong.get((gu, dong), []))
                    if is_jeonse(x) == jeonse]
            typed = []
            for tol, tg in ((5, 'area'), (15, 'area-wide')):
                cand = [x for x in pool if _ar(x) and abs(_ar(x) - r['m2']) <= tol]
                if len(cand) >= 3:
                    typed, tier = cand, tg; break
            else:
                if pool:
                    typed, tier = pool, 'dong'          # 면적 무관 동 전체 (최후 폴백)
        if not typed:
            continue
        s = stat(typed, mine, jeonse)
        if s:
            s['tier'] = tier; s['bldg'] = bldg
            hs = []                                    # 스파크라인/hover용 거래 히스토리
            for x in typed:
                if x.get('dealYear'):
                    ym = f"{str(x['dealYear'])[2:]}.{x['dealMonth']:02d}"; k = x['dealYear'] * 100 + x['dealMonth']
                else:
                    dd = str(x.get('거래일', '')); ym = f"{dd[2:4]}.{dd[5:7]}" if len(dd) >= 7 else '?'
                    k = int(dd[:4] + dd[5:7]) if len(dd) >= 7 else 0
                dep = _num(x.get('deposit') or x.get('보증금액')); rent = _num(x.get('monthlyRent') or x.get('월세금액'))
                hs.append({'ym': ym, 'k': k, 'v': val(dep, rent, jeonse), 'd': dep, 'r': rent})
            hs.sort(key=lambda z: -z['k'])
            s['hist'] = hs[:14]
            r['rt'] = s

def fingerprint(r):   # 가격 제외한 매물 정체성 (재등록으로 itemId 바뀌어도 동일)
    return (round(r['m2'], 1), r.get('floor'), r.get('floors'), r['svc'], r['addr'], (r.get('approve') or '')[:4])

MIN_VALID = 600   # 이보다 작은 스냅샷은 부분 수집(7/23·8/1)이라 이력 계산에서 제외 — 가짜 재등록을 만들기 때문

def _load_snap(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt', encoding='utf-8') as f:
        d = json.load(f)
    return d['items'] if isinstance(d, dict) else d

def attach_history(rows, today, snap_dir=None):
    """각 매물에 체류 이력 부착: days(이 광고의 체류일) · rereg(지문 승계 재등록 횟수) · first(체인 첫 등장일) · lc(좌측절단).
    지문은 호실을 유일하게 특정하지 못하므로 이틀 이상 동시에 살아 있었던 선행 매물은 잇지 않고, 선행 하나는 후속 하나에만 배정.
    같은 id 는 가격이 바뀌지 않고 인하는 재등록으로만 일어나므로, 재등록 횟수가 곧 '몇 번 내렸다 다시 올렸나'."""
    snap_dir = snap_dir or SNAP_DIR
    first, last, fp, dates = {}, {}, {}, set()
    for path in sorted(glob.glob(snap_dir + '/*.json') + glob.glob(snap_dir + '/*.json.gz')):
        day = os.path.basename(path)[:10]
        try: items = _load_snap(path)
        except Exception: continue
        if len(items) < MIN_VALID: continue
        dates.add(day)
        for r in items:
            i = r['id']
            first.setdefault(i, day); last[i] = day; fp[i] = fingerprint(r)
    for r in rows:   # 오늘 수집분은 아직 스냅샷 파일에 없음
        i = r['id']
        first.setdefault(i, today); last[i] = today; fp[i] = fingerprint(r)
    dates.add(today)
    by_fp = defaultdict(list)
    for i in first: by_fp[fp[i]].append(i)
    pred, claimed = {}, set()
    for i in sorted(first, key=lambda k: (first[k], k)):   # 등장 순으로 선행자 배정 (날짜 문자열은 사전순 = 시간순)
        # 하루 겹침(last==first)까지 선행으로 인정: 새 광고 올린 뒤 옛 광고 내리는 사이 스냅샷이 한 번 지나간 경우(연속 지문쌍의 4.5%)
        # 선행자는 반드시 먼저 등장했어야 함(first 엄격 감소) — 같은 날 뜨고 진 둘이 서로를 잡아 순환하는 것 방지
        cands = [j for j in by_fp[fp[i]] if j != i and j not in claimed and first[j] < first[i] and last[j] <= first[i]]
        if cands:
            j = max(cands, key=lambda k: last[k]); pred[i] = j; claimed.add(j)
    d0 = min(dates)
    def _days(a, b): return (datetime.strptime(b, '%Y-%m-%d') - datetime.strptime(a, '%Y-%m-%d')).days
    for r in rows:
        i = head = r['id']; n = 0; seen = {i}
        while head in pred and pred[head] not in seen: head = pred[head]; seen.add(head); n += 1
        r['days'] = _days(first[i], today); r['rereg'] = n; r['first'] = first[head]; r['lc'] = first[head] == d0

def brief(r):
    cd, cv = (r.get('cd'), r.get('cv'))
    tm = f"{r.get('tmin')}분 {r.get('tmode')}" if r.get('tmin') is not None else (f"{cd}m {cv}" if cd else None)
    return {'id': r['id'], 'sales': r['sales'], 'deposit': r['deposit'], 'rent': r['rent'],
            'm2': r['m2'], 'floor': f"{r.get('floor')}/{r.get('floors')}", 'svc': r['svc'],
            'addr': r['addr'], 'approve': (r.get('approve') or '')[:4], 'commute': tm,
            'link': link(r)}

CSS = """
:root{--bg:#f4f5f7;--fg:#1a1a1a;--card:#fff;--muted:#666;--sub:#888;--border:#e2e4e8;--accent:#3a7afe;--imgbg:#e8eaed;--shadow:rgba(0,0,0,.12);--up:#c0392b;--down:#12905a}
body.dark{--bg:#16181c;--fg:#e6e8eb;--card:#1e2126;--muted:#9aa0a6;--sub:#7a7f87;--border:#2c2f36;--accent:#5b8cff;--imgbg:#2c2f36;--shadow:rgba(0,0,0,.55);--up:#e0685f;--down:#49c98c}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,'Malgun Gothic',sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;background:var(--card);padding:14px 20px;border-bottom:1px solid var(--border);z-index:10}
h1{margin:0 0 6px;font-size:18px}.sum{font-size:13px;color:var(--muted)}.sum b{color:var(--fg)}
.controls{padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:57px;z-index:9;font-size:13px}
.controls button{border:1px solid var(--border);background:var(--card);color:var(--fg);padding:6px 12px;border-radius:16px;cursor:pointer;font-size:13px}
.controls button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.controls input{width:64px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--card);color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;padding:18px 20px}
.card{display:block;background:var(--card);border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;border:1px solid var(--border);transition:.1s}
.card:hover{box-shadow:0 4px 14px var(--shadow);transform:translateY(-2px)}
.card.new{border:2px solid #22b573}.card.changed{border:2px solid #f5a623}
.img{height:150px;background:var(--imgbg) center/cover no-repeat}
.body{padding:10px 12px}.b{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:10px;margin-bottom:5px}
.b.new{background:#e3f9ee;color:#12905a}.b.chg{background:#fef3e0;color:#c47d10}
.price{font-size:16px;font-weight:700}.meta{font-size:12px;color:var(--muted);margin-top:3px}
.commute{font-size:12px;color:var(--accent);margin-top:4px;font-weight:600}.commute .dim{color:var(--sub);font-weight:400}.addr{font-size:12px;color:var(--sub);margin-top:3px}
.ml{margin-left:auto}.hidden{display:none}
.stay{font-size:11px;color:var(--sub);margin-top:3px}
#bldg{padding:14px 20px;overflow-x:auto}.bcap{font-size:12px;color:var(--sub);margin-bottom:8px}
.bt{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border:1px solid var(--border);border-radius:10px}
.bt th,.bt td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
.bt th{cursor:pointer;color:var(--muted);font-weight:600;font-size:12px;user-select:none}.bt th.on{color:var(--accent)}
.bt th:first-child,.bt td:first-child{text-align:left}.br{cursor:pointer}.br:hover{background:var(--bg)}.br td:first-child{font-weight:700}
.br .n{color:var(--sub);font-weight:400;font-size:12px}
.bd>td{padding:0;background:var(--bg)}.ut{width:100%;border-collapse:collapse;font-size:12.5px}
.ut td{padding:5px 10px 5px 24px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
.ut td:first-child{text-align:left}.ut a{color:var(--accent);text-decoration:none}.ut .kw{color:var(--muted);font-size:11.5px}
.ut .thumb{width:40px;height:30px;border-radius:4px;background:var(--imgbg) center/cover no-repeat;display:inline-block;vertical-align:middle;margin-right:6px}
.bt .lo{color:var(--down);font-weight:700}.bt .hi{color:var(--up)}
.sep-line{display:inline-block;width:1px;height:20px;background:var(--border);margin:0 3px}
.regions{padding:7px 20px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;background:var(--card);border-bottom:1px solid var(--border);font-size:12px;position:sticky;z-index:8}
.regions{top:105px}.regions.stns{top:146px}
.rlabel{color:var(--sub);font-weight:600;margin-right:4px}
.regions button{border:1px solid var(--border);background:var(--card);color:var(--fg);padding:4px 11px;border-radius:14px;cursor:pointer;font-size:12px}
.guchip{font-weight:700}
.regions button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.regions.stns button{padding:3px 9px;font-size:11.5px}
.regions.stns button.on{background:var(--down);border-color:var(--down)}
.stn-sep{display:inline-block;width:1px;height:14px;background:var(--border);margin:0 2px}
.rt{font-size:11px;margin-top:5px;padding:3px 6px;border-radius:5px;font-weight:600;display:inline-block}
.rt.hi{background:#fdecea;color:#c0392b}.rt.lo{background:#e6f8ef;color:#12905a}.rt.mid{background:#eef0f3;color:#666}
body.dark .rt.mid{background:#2c2f36;color:#aaa}
.spark{position:relative;margin-top:6px}
.spark svg{display:block;width:100%;height:auto;cursor:crosshair}
.sparktip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:2px 7px;font-size:11px;font-weight:700;white-space:nowrap;transform:translate(-50%,-135%);opacity:0;transition:opacity .08s;z-index:5;box-shadow:0 3px 12px var(--shadow)}
"""
JS = """
let sales='전세',sortKey='commute',sepOnly=true,showOffi=true,showHouse=true,bsmtShow=false;   // 기본: 방 분리만 + 반지하 숨김 + 용도 둘 다
let view='cards',bSort='lo',bAsc=true,bOpen={};
const BLDG_MIN=3;   // 같은 필지(pnu)에 이 건수 이상일 때만 건물 표에 올림 (현재 필터 통과분 기준)
function setView(v,e){view=v;document.querySelectorAll('.viewb').forEach(b=>b.classList.remove('on'));e.classList.add('on');
 document.querySelector('.grid').classList.toggle('hidden',v!=='cards');document.getElementById('bldg').classList.toggle('hidden',v!=='bldg');render()}
function med(a){const s=a.slice().sort((x,y)=>x-y),n=s.length;return n?(n%2?s[(n-1)/2]:(s[n/2-1]+s[n/2])/2):0}
function bSortBy(k){if(bSort===k)bAsc=!bAsc;else{bSort=k;bAsc=(k==='name'||k==='lo'||k==='md'||k==='per'||k==='cd'||k==='tmin'||k==='rt'||k==='days')}renderBldg()}
function bToggle(p){bOpen[p]=!bOpen[p];renderBldg()}
function renderBldg(){
 const box=document.getElementById('bldg'); if(view!=='bldg')return;
 const J=sales==='전세';
 const v=c=>J?+c.dataset.deposit/10000:+c.dataset.burden;                 // 전세=보증금(억) / 월세=실질부담(만)
 const fm=x=>J?x.toFixed(2).replace(/\\.?0+$/,'')+'억':Math.round(x)+'만';
 const fper=x=>J?Math.round(x*10000)+'만':x.toFixed(1)+'만';               // ㎡당
 const g={};
 document.querySelectorAll('.card:not(.hidden)').forEach(c=>{const p=c.dataset.pnu||('id'+c.dataset.id);(g[p]=g[p]||[]).push(c)});
 const rows=[];
 for(const p in g){const cs=g[p]; if(cs.length<BLDG_MIN)continue;
  const vals=cs.map(v),m2s=cs.map(c=>+c.dataset.m2),ds=cs.map(c=>+c.dataset.days).filter(x=>x>=0);
  const bt=cs.filter(c=>c.dataset.rttier==='building').map(c=>+c.dataset.rtdiff);
  const c0=cs[0];
  rows.push({p,cs,n:cs.length,lo:Math.min(...vals),md:med(vals),per:med(cs.map(c=>v(c)/+c.dataset.m2)),
   m2lo:Math.min(...m2s),m2hi:Math.max(...m2s),yr:+c0.dataset.yr||0,cv:c0.dataset.cv,cd:+c0.dataset.cd,
   tmin:Math.min(...cs.map(c=>+c.dataset.commute)),rt:bt.length?med(bt):null,days:ds.length?med(ds):-1,
   rereg:cs.reduce((a,c)=>a+(+c.dataset.rereg),0),nnew:cs.filter(c=>c.classList.contains('new')).length,
   name:c0.dataset.bldg||(c0.dataset.addr+' '+(+c0.dataset.yr?c0.dataset.yr:'?')+'년 '+c0.dataset.floors+'층')});
 }
 const key={name:r=>r.name,n:r=>r.n,lo:r=>r.lo,md:r=>r.md,per:r=>r.per,m2:r=>r.m2lo,yr:r=>r.yr,cd:r=>r.cd,tmin:r=>r.tmin,rt:r=>r.rt===null?9999:r.rt,days:r=>r.days,rereg:r=>r.rereg}[bSort];
 rows.sort((a,b)=>{const x=key(a),y=key(b);const c=typeof x==='string'?x.localeCompare(y):x-y;return bAsc?c:-c});
 const th=(k,t)=>'<th class="'+(bSort===k?'on':'')+'" onclick="bSortBy(\\''+k+'\\')">'+t+(bSort===k?(bAsc?' ↑':' ↓'):'')+'</th>';
 const cap=J?'보증금':'부담';
 let h='<div class="bcap">같은 필지(pnu)에 <b>'+BLDG_MIN+'건 이상</b>인 건물만 · 현재 탭·필터 적용 · '+(J?'값은 보증금(억)':'부담 = 월세+관리비+보증금×3%÷12')+' · 실거래 대비는 같은 건물 실거래 있을 때만 · 행 클릭 → 호실</div>';
 h+='<table class="bt"><thead><tr>'+th('name','건물')+th('n','건수')+th('lo',cap+' 최저')+th('md','중앙')+th('per','㎡당 중앙')+th('m2','면적')+th('yr','준공')+th('cd','셔틀')+th('tmin','통근')+th('rt','실거래 대비')+th('days','체류 중앙')+th('rereg','재등록')+'</tr></thead><tbody>';
 if(!rows.length)h+='<tr><td colspan="12" style="text-align:center;color:var(--sub)">현재 필터에서 '+BLDG_MIN+'건 이상 건물 없음</td></tr>';
 for(const r of rows){
  const rtc=r.rt===null?'—':'<span class="'+(r.rt>8?'hi':(r.rt<-8?'lo':''))+'">'+(r.rt>0?'+':'')+r.rt+'%</span>';
  h+='<tr class="br" onclick="bToggle(\\''+r.p+'\\')"><td>'+(bOpen[r.p]?'▾ ':'▸ ')+r.name+(r.nnew?' <span class="n">🆕'+r.nnew+'</span>':'')+'</td><td>'+r.n+'</td><td>'+fm(r.lo)+'</td><td>'+fm(r.md)+'</td><td>'+fper(r.per)+'</td><td>'+r.m2lo+'~'+r.m2hi+'㎡</td><td>'+(r.yr||'?')+'</td><td>'+(r.cd<99999?r.cv+' '+r.cd+'m':'—')+'</td><td>'+(r.tmin<999?r.tmin+'분':'—')+'</td><td>'+rtc+'</td><td>'+(r.days>=0?r.days+'일':'—')+'</td><td>'+(r.rereg?'♻️'+r.rereg:'—')+'</td></tr>';
  if(bOpen[r.p]){
   h+='<tr class="bd"><td colspan="12"><table class="ut">';
   for(const c of r.cs.slice().sort((a,b)=>v(a)-v(b))){
    const d=c.dataset, mg=+d.manage?'+관'+d.manage:'';
    const price=J?fm(+d.deposit/10000):'보'+(+d.deposit>=10000?(+d.deposit/10000).toFixed(2).replace(/\\.?0+$/,'')+'억':d.deposit+'만')+'/'+d.rent+mg;
    const lc=d.lc==='1', rr=+d.rereg;
    const stay=(+d.days>=0?'⏱'+(lc&&!rr?'≥':'')+d.days+'일':'')+(rr?' ♻️'+rr+'회('+(lc?'≥':'')+d.first.slice(5).replace('-','/')+'~)':'');
    h+='<tr><td>'+(d.img?'<span class="thumb" style="background-image:url(\\''+d.img+'?w=80&h=60&q=60\\')"></span>':'')+(c.classList.contains('new')?'🆕 ':'')+'<a href="'+c.href+'" target="_blank" rel="noopener">'+d.m2+'㎡ · '+d.floor+'/'+d.floors+'층 · '+d.room+'</a></td>'
      +'<td><b>'+fm(v(c))+'</b></td><td>'+fper(v(c)/+d.m2)+'/㎡</td><td>'+price+'</td><td class="kw">'+(d.kw||'').split(',').filter(Boolean).join(' · ')+'</td><td>'+stay+'</td></tr>';
   }
   h+='</table></td></tr>';
  }
 }
 box.innerHTML=h+'</tbody></table>';
}
function render(){
 const dc=document.getElementById('dcap').value,rc=document.getElementById('rcap').value,tc=document.getElementById('tcap').value,bc=document.getElementById('bcap').value,yc=document.getElementById('ycap').value,mc=document.getElementById('mcap').value;
 const stnOn={}; document.querySelectorAll('.stnchip').forEach(b=>stnOn[b.dataset.stn]=b.classList.contains('on'));
 const stnCount={};
 let cj=0,cw=0,co=0,ch=0;
 document.querySelectorAll('.card').forEach(c=>{
  const isJ=c.dataset.sales==='전세', isO=c.dataset.svcg==='offi';
  const sepOK=!(sepOnly&&c.dataset.room==='오픈형원룸') && !(!bsmtShow&&c.dataset.bsmt==='1');
  const capOK=(!dc||+c.dataset.deposit<=+dc*10000) && (isJ||!rc||+c.dataset.rent<=+rc) && (!tc||+c.dataset.commute<=+tc)
   && (!bc||+c.dataset.burden<=+bc) && (!yc||+c.dataset.yr>=+yc) && (!mc||+c.dataset.m2>=+mc);   // 보증금 상한은 전·월세 공통, 준공미상(yr=0)은 준공필터에서 제외
  const svcOK=isO?showOffi:showHouse;
  const stnOK=stnOn[c.dataset.stn]!==false;
  if(sepOK&&capOK&&svcOK&&stnOK){if(isJ)cj++;else cw++;}                // 탭 숫자
  if(c.dataset.sales===sales&&sepOK&&capOK&&stnOK){if(isO)co++;else ch++;} // 용도칩(현재 탭)
  if(c.dataset.sales===sales&&sepOK&&capOK&&svcOK)                      // 역칩 숫자(현재 탭, 지역필터 무시)
    stnCount[c.dataset.stn]=(stnCount[c.dataset.stn]||0)+1;
  c.classList.toggle('hidden',!(c.dataset.sales===sales&&sepOK&&capOK&&svcOK&&stnOK));
 });
 document.querySelectorAll('.stnchip').forEach(b=>{                     // 현재 탭 0인 역은 숨김
  const n=stnCount[b.dataset.stn]||0; b.querySelector('span').textContent=n; b.style.display=n?'':'none';
 });
 document.querySelectorAll('.guchip').forEach(b=>{
  const ss=GU2STN[b.dataset.gu]||[]; const n=ss.reduce((a,s)=>a+(stnCount[s]||0),0);
  b.querySelector('span').textContent=n; b.style.display=n?'':'none';
  b.classList.toggle('on',ss.some(s=>stnOn[s]!==false));
 });
 const tabs=document.querySelectorAll('.tab');
 tabs[0].textContent='전세 '+cj; tabs[1].textContent='월세 '+cw;
 document.getElementById('chipOffi').textContent='🏢 오피스텔 '+co;
 document.getElementById('chipHouse').textContent='🏠 빌라·원룸 '+ch;
 const g=document.querySelector('.grid');
 [...g.children].filter(c=>!c.classList.contains('hidden'))
  .sort((a,b)=>{                                                       // 1차=선택 정렬키, 2차=통근분(동점 다수라 명시 필요)
   const p=sortKey==='m2'?(+b.dataset.m2)-(+a.dataset.m2):(+a.dataset[sortKey])-(+b.dataset[sortKey]);
   return p||(+a.dataset.commute)-(+b.dataset.commute);
  }).forEach(c=>g.appendChild(c));
 renderBldg();
}
function setSales(s,e){sales=s;document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));e.classList.add('on');render()}
function setSort(k,e){sortKey=k;document.querySelectorAll('.sortb').forEach(t=>t.classList.remove('on'));e.classList.add('on');render()}
function toggleSep(e){sepOnly=!sepOnly;e.classList.toggle('on',!sepOnly);render()}   // 버튼 ON = 오픈형 포함 중
function toggleBsmt(e){bsmtShow=!bsmtShow;e.classList.toggle('on',bsmtShow);render()}  // 버튼 ON = 반지하·옥탑 포함 중
function toggleSvc(e,w){   // 마지막 하나는 못 끔(둘 다 끄면 빈 화면)
 if(w==='offi'){ if(showOffi&&!showHouse)return; showOffi=!showOffi; e.classList.toggle('on',showOffi); }
 else { if(showHouse&&!showOffi)return; showHouse=!showHouse; e.classList.toggle('on',showHouse); }
 render();
}
function toggleStn(name,el){el.classList.toggle('on');render()}          // 역 단위 다중토글
function toggleGu(gu,el){                                                 // 구 = 마스터(현재 보이는 역 전부 켜기/끄기)
 const ss=GU2STN[gu]||[];
 const chips=[...document.querySelectorAll('.stnchip')].filter(b=>ss.includes(b.dataset.stn)&&b.style.display!=='none');
 const anyOn=chips.some(b=>b.classList.contains('on'));
 chips.forEach(b=>b.classList.toggle('on',!anyOn));
 render();
}
function toggleDark(e){const d=document.body.classList.toggle('dark');localStorage.setItem('rw_dark',d?'1':'0');e.textContent=d?'☀️ 라이트':'🌙 다크'}
(function(){const s=localStorage.getItem('rw_dark');const dark=s===null?matchMedia('(prefers-color-scheme:dark)').matches:s==='1';if(dark)document.body.classList.add('dark')})();
function _eok(v){return v>=10000?(v/10000).toFixed(2).replace(/\\.?0+$/,'')+'억':v+'만'}
function drawSpark(el){
 const d=RT_DATA[el.dataset.id]; if(!d)return;
 const H0=d.h.slice().reverse(), vals=H0.map(x=>x[1]);
 const lo=Math.min(...vals,d.m),hi=Math.max(...vals,d.m),n=H0.length;
 const W=250,H=50,px=5,py=7;
 const X=i=>px+(n<2?0.5*(W-2*px):i*(W-2*px)/(n-1)), Y=v=>H-py-(hi===lo?.5:(v-lo)/(hi-lo))*(H-2*py);
 const col=d.diff>8?'var(--up)':(d.diff<-8?'var(--down)':'var(--accent)');
 const L=H0.map((x,i)=>(i?'L':'M')+X(i).toFixed(1)+','+Y(x[1]).toFixed(1)).join(' ');
 const A='M'+X(0).toFixed(1)+','+(H-py)+' '+H0.map((x,i)=>'L'+X(i).toFixed(1)+','+Y(x[1]).toFixed(1)).join(' ')+' L'+X(n-1).toFixed(1)+','+(H-py)+' Z';
 const my=Y(d.m).toFixed(1);
 const pts=H0.map((x,i)=>'<circle cx="'+X(i).toFixed(1)+'" cy="'+Y(x[1]).toFixed(1)+'" r="'+(i===n-1?3:1.8)+'" style="fill:'+(i===n-1?col:'var(--sub)')+'"/>').join('');
 el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
  +'<path d="'+A+'" style="fill:'+col+';fill-opacity:.13"/>'
  +'<path d="'+L+'" style="fill:none;stroke:'+col+';stroke-width:1.5"/>'
  +'<line x1="'+px+'" x2="'+(W-px)+'" y1="'+my+'" y2="'+my+'" style="stroke:var(--fg);stroke-width:1;stroke-dasharray:3 2;opacity:.45"/>'
  +pts+'</svg><div class="sparktip"></div>';
 const svg=el.querySelector('svg'), tip=el.querySelector('.sparktip');
 svg.addEventListener('mousemove',function(e){
  const rc=svg.getBoundingClientRect(), rx=(e.clientX-rc.left)/rc.width*W;
  let bi=0,bd=1e9; for(let i=0;i<n;i++){const q=Math.abs(X(i)-rx);if(q<bd){bd=q;bi=i}}
  const x=H0[bi];
  tip.textContent=x[0]+' · '+x[2]+'/'+x[3]+' · '+_eok(x[1]);
  tip.style.left=(X(bi)/W*100)+'%'; tip.style.top=(Y(x[1])/H*rc.height)+'px'; tip.style.opacity=1;
 });
 svg.addEventListener('mouseleave',function(){tip.style.opacity=0});
 svg.addEventListener('click',function(e){e.preventDefault();e.stopPropagation()});
}
// 사진 로드를 기다리는 onload 대신 DOMContentLoaded → 필터가 첫 페인트 직후 적용됨
document.addEventListener('DOMContentLoaded',function(){
 render();
 if(document.body.classList.contains('dark'))document.getElementById('darkbtn').textContent='☀️ 라이트';
 document.querySelectorAll('.spark').forEach(drawSpark);
});
"""

# 건물 내 비교용 제목 키워드 — 같은 건물 호가 편차의 설명변수(리모델링·옵션·단기·용도)
KEYWORDS = ['리모델링', '올수리', '풀옵션', '첫입주', '단기', '근생', '전입불가', '복층', '테라스']
# 지역 필터: 최근접 역 → 그 역이 속한 구로 묶음(계층형). 역단위 다중토글, 구는 마스터.
GU_GROUPS = [('강남구', ['강남', '도곡', '매봉', '신논현']),
             ('서초구', ['양재', '양재숲', '서초', '교대', '남부터미널', '방배']),
             ('관악구', ['서울대입구', '신림']),
             ('동작구', ['사당']),
             ('경기', ['정자', '판교', '수지구청', '광교중앙'])]
# 수집은 계속하되 대시보드 기본 표시에서만 빼는 역. 칩을 누르면 언제든 켜짐.
# 신분당선 남쪽(정자·판교·수지구청·광교중앙): 통근은 짧지만 남북 단일축이라 서울 생활권 접근이 불편해 제외.
DEFAULT_OFF_STN = {'정자', '판교', '수지구청', '광교중앙'}
def nearest_stn(lat, lng):
    return min(STATIONS, key=lambda k: hav(lat, lng, STATIONS[k][0], STATIONS[k][1]))

def build_html(rows, report, ts):
    def esc(s): return (str(s) if s is not None else '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    def won(v):
        if not v: return '0'
        return (f"{v/10000:.2f}억").replace('.00억', '억') if v >= 10000 else f"{v}만"
    present_stn = set()
    cards = []
    for r in sorted(rows, key=lambda x: x.get('tmin') if x.get('tmin') is not None else 999):
        st = r.get('_status', '')
        badge = '<span class="b new">🆕 신규</span>' if st == 'new' else \
                (f'<span class="b chg">💰 {esc(r.get("_before"))} → 변동</span>' if st == 'changed' else '')
        price = won(r['deposit']) if r['sales'] == '전세' else f"{won(r['deposit'])} / 월 {r['rent']}만"
        pg = round(r['m2'] / 3.305785, 1)
        yr = (r.get('approve') or '')[:4] or '?'
        imgstyle = f"background-image:url('{r['img']}?w=280&h=210&q=70')" if r.get('img') else ''
        rt = r.get('rt'); rtline = ''; rtdiff = 99999
        if rt and rt.get('tier'):
            tier = rt['tier']; dd = rt['diff']
            if tier in ('building', 'building-any'):        # 같은 건물 → diff% (직접 비교)
                rtdiff = dd
                cls = 'hi' if dd > 8 else ('lo' if dd < -8 else 'mid')
                kind = '전세' if r['sales'] == '전세' else '환산'
                same = '' if tier == 'building' else ' 타평형'
                note = '' if (tier == 'building' and rt['n'] >= 2) else ' ·참고'
                rtline = f'<div class="rt {cls}">🏢 {esc(rt.get("bldg") or "건물 실거래")} · 실거래{kind} {won(rt["med"])}({rt["n"]}){same} {dd:+d}%{note}</div>'
            else:                                           # 동네 → 백분위 (극단값 왜곡 방지)
                pct = rt['pct']; rtdiff = pct - 50   # 싼(하위)=음수 → 저평가순 위로
                cls = 'lo' if pct <= 25 else ('hi' if pct >= 75 else 'mid')
                pos = f'하위 {pct}% (싼 편)' if pct <= 50 else f'상위 {100 - pct}% (비싼 편)'
                rtline = f'<div class="rt {cls}">📊 동네 {r["m2"]:.0f}㎡ 실거래 {pos} ({rt["n"]}건) ·참고</div>'
        sparkdiv = f'<div class="spark" data-id="{r["id"]}"></div>' if (rt and rt.get('hist')) else ''
        rttier = (rt or {}).get('tier') or ''
        bldg = (rt or {}).get('bldg') or ''
        kw = ','.join(k for k in KEYWORDS if k in (r.get('title') or ''))
        # 체류 배지: 이 광고의 체류일 + 지문 승계 재등록 횟수(있으면 체인 첫 등장일). 좌측절단은 ≥
        days = r.get('days'); stay = ''
        if days is not None:
            lc = '≥' if r.get('lc') else ''   # 좌측절단: 재등록 없으면 광고 체류일에, 있으면 체인 시작일에 붙임
            rr = f" · ♻️ 재등록 {r['rereg']}회 ({lc}{r['first'][5:].replace('-', '/')}~)" if r.get('rereg') else ''
            stay = f'<div class="stay">⏱ 체류 {lc if not r.get('rereg') else ''}{days}일{rr}</div>'
        if r.get('tmin') is not None:
            dist = f' <span class="dim">({r.get("tdist")}m)</span>' if r.get('ttype') == 'walk' else ''
            cmline = f'🚆 약 {r.get("tmin")}분 · {esc(r.get("tmode"))}{dist}'
        else:
            cmline = '🚆 <span class="dim">통근 미확정(다음 갱신)</span>'
        stn = nearest_stn(r['lat'], r['lng']) if r.get('lat') else '기타'
        present_stn.add(stn)
        stntag = f' ({stn}역)' if stn != '기타' else ''
        mgtag = f' · 관리비 {r["manage"]:g}만' if r.get('manage') else ''
        bsmt = str(r.get('floor')) in ('반지하', '옥탑방')   # 비선호 층(반지하·옥탑) — 기본 숨김
        # 초기 화면(전세탭 + 오픈형·반지하 제외)에 안 보일 카드는 미리 hidden → FOUC(깜빡임) 방지
        init_hide = ' hidden' if (r['sales'] != '전세' or r.get('room') == '오픈형원룸' or bsmt) else ''
        cards.append(f'''<a class="card {st}{init_hide}" href="{link(r)}" target="_blank" rel="noopener"
 data-id="{r['id']}" data-sales="{r['sales']}" data-commute="{r.get('tmin') if r.get('tmin') is not None else 999}" data-deposit="{r['deposit'] or 0}" data-rent="{r['rent'] or 0}" data-m2="{r['m2']}" data-room="{esc(r.get('room'))}" data-rtdiff="{rtdiff}" data-svcg="{'offi' if r['svc'] == '오피스텔' else 'house'}" data-stn="{stn}" data-bsmt="{1 if bsmt else 0}" data-yr="{yr if yr.isdigit() else 0}" data-burden="{round((r['rent'] or 0) + (r.get('manage') or 0) + (r['deposit'] or 0) * BURDEN_RATE / 12)}"
 data-pnu="{esc(r.get('pnu'))}" data-bldg="{esc(bldg)}" data-addr="{esc(r['addr'])}" data-cd="{r.get('cd') or 99999}" data-cv="{esc(r.get('cv'))}" data-floor="{esc(r.get('floor'))}" data-floors="{esc(r.get('floors'))}" data-manage="{r.get('manage') or 0}" data-rttier="{rttier}" data-kw="{kw}" data-days="{days if days is not None else -1}" data-rereg="{r.get('rereg', 0)}" data-first="{r.get('first', '')}" data-lc="{1 if r.get('lc') else 0}" data-img="{r.get('img') or ''}">
 <div class="img" style="{imgstyle}"></div><div class="body">{badge}
  <div class="price">{r['sales']} {price}</div>
  <div class="meta">{r['m2']}㎡ ({pg}평) · {r.get('floor')}/{r.get('floors')}층 · {yr}준공 · {esc(r['svc'])}{mgtag}</div>
  <div class="commute">{cmline}</div>
  <div class="addr">{esc(r['addr'])}{stntag}</div>{stay}{rtline}{sparkdiv}</div></a>''')
    if report.get('baseline'):
        change = '기준점 설정 — 다음 실행부터 신규·빠짐·가격변동 표시'
    else:
        change = (f"🆕 신규 {len(report['new'])} · ❌ 빠짐 {len(report['removed'])} · "
                  f"💰 가격변동 {len(report['price_changed'])} (직전 {report['prev_snapshot']} 대비)")
    tj, tw = report['total_by_sales']['전세'], report['total_by_sales']['월세']
    # 탭 초기 숫자 = 기본 필터(오픈형·반지하 제외) 반영 → JS 돌기 전에도 숫자가 맞음
    def _std(r): return r.get('room') != '오픈형원룸' and str(r.get('floor')) not in ('반지하', '옥탑방')
    vj = sum(1 for r in rows if r['sales'] == '전세' and _std(r))
    vw = sum(1 for r in rows if r['sales'] == '월세' and _std(r))
    # 용도 칩 초기 숫자 (기본 화면 = 전세탭 + 오픈형·반지하 제외)
    co = sum(1 for r in rows if r['sales'] == '전세' and _std(r) and r['svc'] == '오피스텔')
    ch = vj - co
    rt_data = {}
    for r in rows:
        rt = r.get('rt')
        if rt and rt.get('tier') and rt.get('hist'):
            mine = r['deposit'] if r['sales'] == '전세' else round((r['deposit'] or 0) + (r['rent'] or 0) * 12 / RT_CONV)
            rt_data[r['id']] = {'h': [[x['ym'], x['v'], x['d'], x['r']] for x in rt['hist']], 'm': mine, 'diff': rt['diff']}
    rt_json = json.dumps(rt_data, ensure_ascii=False)
    # 지역 칩 (계층: 구 마스터 + 역 다중토글). present_stn만.
    gu_row, stn_row, gu2stn = [], [], {}
    for gu, stns in GU_GROUPS:
        ss = [s for s in stns if s in present_stn]
        if not ss: continue
        gu2stn[gu] = ss
        gu_on = ' on' if any(x not in DEFAULT_OFF_STN for x in ss) else ''
        gu_row.append(f'<button class="guchip{gu_on}" data-gu="{gu}" onclick="toggleGu(\'{gu}\',this)">{gu} <span></span></button>')
        for s in ss:
            son = '' if s in DEFAULT_OFF_STN else ' on'
            stn_row.append(f'<button class="stnchip{son}" data-stn="{s}" data-gu="{gu}" onclick="toggleStn(\'{s}\',this)">{s} <span></span></button>')
        stn_row.append('<span class="stn-sep"></span>')
    region_html = (f'<div class="regions"><span class="rlabel">지역</span>{"".join(gu_row)}</div>'
                   f'<div class="regions stns">{"".join(stn_row)}</div>') if gu2stn else ''
    gu2stn_json = json.dumps(gu2stn, ensure_ascii=False)
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>방 매물 {ts}</title>
<style>{CSS}</style></head><body>
<header><h1>🏠 방 매물 <span style="font-size:13px;color:#888">({ts} 갱신)</span></h1>
<div class="sum">현재 <b>{len(rows)}건</b> · 전세 {tj} / 월세 {tw} &nbsp;|&nbsp; {change}</div></header>
<div class="controls">
 <button class="tab on" onclick="setSales('전세',this)">전세 {vj}</button>
 <button class="tab" onclick="setSales('월세',this)">월세 {vw}</button>
 <span class="sep-line"></span>
 <button class="viewb on" onclick="setView('cards',this)">🃏 카드</button>
 <button class="viewb" onclick="setView('bldg',this)" title="같은 건물 3건 이상만 · 행 클릭하면 호실 비교">🏢 건물</button>
 <span style="width:10px"></span>
 <button class="sortb on" onclick="setSort('commute',this)">통근순</button>
 <button class="sortb" onclick="setSort('deposit',this)">보증금순</button>
 <button class="sortb" onclick="setSort('rent',this)">월세순</button>
 <button class="sortb" onclick="setSort('m2',this)">면적순</button>
 <button class="sortb" onclick="setSort('rtdiff',this)">실거래저평가순</button>
 <button class="sortb" onclick="setSort('burden',this)">부담순</button>
 <span class="sep-line"></span>
 <button id="chipOffi" class="on" onclick="toggleSvc(this,'offi')">🏢 오피스텔 {co}</button>
 <button id="chipHouse" class="on" onclick="toggleSvc(this,'house')">🏠 빌라·원룸 {ch}</button>
 <span class="sep-line"></span>
 <button onclick="toggleSep(this)">🏠 오픈형 원룸 포함</button>
 <button onclick="toggleBsmt(this)">🕳 반지하·옥탑 포함</button>
 <span style="width:10px"></span>
 보증금<input id="dcap" type="number" placeholder="상한" oninput="render()">억
 월세<input id="rcap" type="number" placeholder="상한" oninput="render()">만
 통근<input id="tcap" type="number" placeholder="상한" oninput="render()">분↓
 부담<input id="bcap" type="number" placeholder="상한" oninput="render()" title="월세+관리비+보증금×3%÷12 (만원)">만↓
 준공<input id="ycap" type="number" placeholder="하한" oninput="render()">년↑
 면적<input id="mcap" type="number" placeholder="하한" oninput="render()">㎡↑
 <button id="darkbtn" class="ml" onclick="toggleDark(this)">🌙 다크</button>
</div>{region_html}<div class="grid">{''.join(cards)}</div><div id="bldg" class="hidden"></div>
<script>const RT_DATA={rt_json};const GU2STN={gu2stn_json};</script>
<script>{JS}</script></body></html>'''

def main():
    os.makedirs(SNAP_DIR, exist_ok=True)
    seen = collect_ids()
    if not seen:   # 검색 API 붕괴 시 빈 스냅샷을 커밋하면 diff가 통째로 오염됨 → 실패로 중단
        raise SystemExit('collect_ids() returned 0 items: zigbang search API broken? snapshot not written')
    targets = [iid for iid, v in seen.items()
               if v['lat'] and min(hav(v['lat'], v['lng'], s[0], s[1]) for s in STATIONS.values()) <= MAX_DIST
               and (EXCLUDE_NORTH_LAT is None or v['lat'] < EXCLUDE_NORTH_LAT)]
    rows = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        for r in ex.map(fetch_detail, targets):
            if r and r['sales'] not in ('전세', '월세'): continue   # 무필터 수집이라 매매 등 유입 차단
            if r and r['m2'] and r['m2'] >= MIN_M2 and r['lat']:
                if EXCLUDE_NORTH_LAT and r['lat'] >= EXCLUDE_NORTH_LAT: continue
                cd, cv = commute(r['lat'], r['lng']); r['cd'], r['cv'] = cd, cv
                rows.append(r)
    cur = {r['id']: r for r in rows}
    attach_realprice(rows)   # 각 매물에 실거래 시세(r['rt']) 부착
    for i, r in enumerate(rows):   # 각 매물에 실제 통근시간(분) — ODsay, 캐시. 순차(초당제한)
        r.update(transit(r['lat'], r['lng']))
        if ODSAY_KEY and (i + 1) % 25 == 0:
            print(f'  통근시간 {i+1}/{len(rows)}', file=__import__('sys').stderr)

    prev_files = sorted(glob.glob(SNAP_DIR + '/*.json'))
    ts = datetime.now(KST).strftime('%Y-%m-%d-%H%M')
    try: attach_history(rows, ts[:10])   # 체류일·재등록 이력 (전체 스냅샷 기준, 오늘분 포함)
    except Exception as e: history_error = repr(e)   # 이력은 부가정보 — 실패해도 스냅샷·diff 는 살려야 함
    else: history_error = None
    report = {'snapshot': ts, 'total': len(cur),
              'total_by_sales': {s: sum(1 for r in rows if r['sales'] == s) for s in ('전세', '월세')}}
    if history_error: report['history_error'] = history_error

    if not prev_files:
        report['baseline'] = True
        report['message'] = '첫 실행 — 기준 스냅샷 저장. 다음 실행부터 변동 비교.'
        for r in rows: r['_status'] = ''
    else:
        prev = json.load(open(prev_files[-1]))
        # itemId는 매물 수정 시 새로 발급되므로, 매물 지문(면적+층+종류+주소+준공)으로 매칭.
        # 지문 같고 가격 다르면 = 재등록(가격변동), itemId 바뀌어도 신규로 오인하지 않음.
        prev_by, cur_by = defaultdict(list), defaultdict(list)
        for r in prev['items']: prev_by[fingerprint(r)].append(r)
        for r in rows: cur_by[fingerprint(r)].append(r)
        new, removed, changed = [], [], []
        for f in set(prev_by) | set(cur_by):
            ps, cs = list(prev_by.get(f, [])), list(cur_by.get(f, []))
            cs_by_price = defaultdict(list)
            for r in cs: cs_by_price[(r.get('deposit'), r.get('rent'))].append(r)
            ps_left = []
            for r in ps:
                k = (r.get('deposit'), r.get('rent'))
                if cs_by_price[k]: cs_by_price[k].pop()          # 가격까지 동일 = 유지
                else: ps_left.append(r)
            cs_left = [r for lst in cs_by_price.values() for r in lst]
            n = min(len(ps_left), len(cs_left))
            for a, b in zip(ps_left[:n], cs_left[:n]):           # 지문 같고 가격 다름 = 가격변동
                changed.append({**brief(b),
                                'before': {'deposit': a.get('deposit'), 'rent': a.get('rent')},
                                'after': {'deposit': b.get('deposit'), 'rent': b.get('rent')}})
            new += [brief(r) for r in cs_left[n:]]               # 현재만 = 진짜 신규
            removed += [brief(r) for r in ps_left[n:]]           # 이전만 = 진짜 빠짐
        if removed:   # 빠진 매물 진짜 나갔나 재조회 (close=계약·내림 / open=아직열림·수집누락 / deleted)
            with ThreadPoolExecutor(max_workers=15) as ex:
                sts = dict(zip([x['id'] for x in removed], ex.map(probe_status, [x['id'] for x in removed])))
            for x in removed: x['status'] = sts.get(x['id'])
        report.update({'baseline': False, 'prev_snapshot': prev['snapshot'],
                       'new': new, 'removed': removed, 'price_changed': changed,
                       'unchanged': len(cur) - len(new) - len(changed)})
        newset = {c['id'] for c in new}
        chgmap = {c['id']: c for c in changed}
        for r in rows:
            if r['id'] in newset:
                r['_status'] = 'new'
            elif r['id'] in chgmap:
                bf = chgmap[r['id']]['before']
                r['_status'] = 'changed'
                r['_before'] = won_short(bf, r['sales'])
            else:
                r['_status'] = ''

    json.dump({'snapshot': ts, 'items': rows}, open(f'{SNAP_DIR}/{ts}.json', 'w'), ensure_ascii=False)
    try:
        os.makedirs(os.path.dirname(DASHBOARD), exist_ok=True)
        open(DASHBOARD, 'w', encoding='utf-8').write(build_html(rows, report, ts))
        report['dashboard'] = DASHBOARD
    except Exception as e:
        report['dashboard_error'] = repr(e)
    print(json.dumps(report, ensure_ascii=False, indent=2))

def won_short(p, sales):
    d, rt = p.get('deposit'), p.get('rent')
    ds = (f"{d/10000:.2f}억").replace('.00억', '억') if d and d >= 10000 else f"{d}만"
    return ds if sales == '전세' else f"{ds}/{rt}"

if __name__ == '__main__':
    main()
