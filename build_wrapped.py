#!/usr/bin/env python3
"""Build "Cubing Wrapped": a year-in-review for every WCA competitor, from the WCA results export.

Usage: python3 build_wrapped.py <WCA_export_tsv.zip | folder_with_tsvs> <output_dir>

Writes into <output_dir>:
  meta.json     years, event list, countries, per-year distributions (for "more than X% of cubers") and top lists
  y/<year>/<n>.gz  per-year gzipped person shards (id, name, country, json for that year); i/<n>.txt + i/s_<key>.txt  all-time people index (with a bitmask of the years each person competed in)
  p/s_<key>.txt name search buckets (only people who competed in one of the covered years)
"""
import bisect, datetime, json, os, re, shutil, sys, tempfile, unicodedata, zipfile
from array import array
from collections import defaultdict

src, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
if src.endswith('.zip'):
    D = tempfile.mkdtemp()
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        for name in ('persons', 'results', 'competitions'):
            z.extract(f'WCA_export_{name}.tsv', D)
        if 'metadata.json' in names:
            z.extract('metadata.json', D)
else:
    D = src

EV = ['333', '222', '444', '555', '666', '777', '333bf', '333fm', '333oh', 'clock', 'minx', 'pyram', 'skewb', 'sq1',
      '444bf', '555bf', '333mbf']
NAMES = ['3x3', '2x2', '4x4', '5x5', '6x6', '7x7', '3x3 Blindfolded', 'Fewest Moves', '3x3 One-Handed', 'Clock', 'Megaminx',
         'Pyraminx', 'Skewb', 'Square-1', '4x4 Blindfolded', '5x5 Blindfolded', 'Multi-Blind']
EIDX = {e: i for i, e in enumerate(EV)}
NE = len(EV)
HAS_AVG = [e != '333mbf' for e in EV]
FIRST_YEAR = int(os.environ.get('WRAPPED_FROM', '1982'))   # every year the WCA has results for (set WRAPPED_FROM to test with fewer)


def rows(name):
    with open(f'{D}/WCA_export_{name}.tsv', encoding='utf-8', errors='replace') as f:
        header = f.readline().rstrip('\n').split('\t')
        idx = {h: i for i, h in enumerate(header)}
        n = len(header)
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) == n:
                yield idx, p


try:
    stamp = json.load(open(f'{D}/metadata.json')).get('export_date', '')
except Exception:
    stamp = ''
today = datetime.date.fromisoformat(stamp[:10]) if stamp[:10] else datetime.date.today()
CUR = today.year
print('as of', today)

# ---------- competitions ----------
comp_info = {}
for idx, p in rows('competitions'):
    try:
        comp_info[p[idx['id']]] = (int(p[idx['year']]), int(p[idx['month']]), int(p[idx['day']]), p[idx['name']], p[idx['country_id']])
    except ValueError:
        pass
comp_ids = sorted(comp_info, key=lambda c: (comp_info[c][:3], c))
CRANK = {c: i for i, c in enumerate(comp_ids)}
COMP_Y = [comp_info[c][0] for c in comp_ids]

# ---------- results, kept as compact columns ----------
PIDX, PIDS = {}, []
countries, CIDX = [], {}
def tier(x):
    return 0 if x in ('', 'NULL') else (1 if x == 'WR' else (3 if x == 'NR' else 2))


rc, re_, rrt, rpos, rbest, ravg, rpid, rrs, rra = (array('i') for _ in range(9))
for idx, p in rows('results'):
    e = EIDX.get(p[idx['event_id']])
    c = CRANK.get(p[idx['competition_id']])
    if e is None or c is None:
        continue
    pid = p[idx['person_id']]
    i = PIDX.get(pid)
    if i is None:
        i = PIDX[pid] = len(PIDS); PIDS.append(pid)
    try:
        b, a, pos = int(p[idx['best']]), int(p[idx['average']]), int(p[idx['pos']])
    except ValueError:
        continue
    rc.append(c); re_.append(e); rpid.append(i); rbest.append(b); ravg.append(a); rpos.append(pos)
    rrt.append(1 if p[idx['round_type_id']] in ('f', 'c') else 0)
    rrs.append(tier(p[idx['regional_single_record']])); rra.append(tier(p[idx['regional_average_record']]))
NR_ = len(rc)
NPID = len(PIDS)
print('results kept:', NR_, 'people:', NPID)

pname, pctry = {}, {}
for idx, p in rows('persons'):
    w = p[idx['wca_id']]
    if w in PIDX and (p[idx['sub_id']] == '1' or w not in pname):
        pname[w] = p[idx['name']]
        pctry[w] = p[idx['country_id']]

# the order we replay history in: by competition date (stable, so rounds of a competition stay together)
order = sorted(range(NR_), key=rc.__getitem__)

# who competed in which year (years without any results, like 1983-2002, are left out)
active = defaultdict(set)
for i in range(NR_):
    y = COMP_Y[rc[i]]
    if FIRST_YEAR <= y <= CUR:
        active[y].add(rpid[i])
YEARS = sorted(active)
print('years:', YEARS[0], '-', YEARS[-1], f'({len(YEARS)} with results)')
print('active people per year:', {y: len(s) for y, s in active.items()})

# ---------- boundaries: ranks at the end of each year (needed for "start of year" and "end of year") ----------
need_at = {}                    # boundary year B (end of B) -> set of people we need ranks for
for y in YEARS:
    need_at.setdefault(y - 1, set()).update(active[y])
    need_at.setdefault(y, set()).update(active[y])
snaps = {}                      # B -> {person: (per-event rank list [s0,a0,s1,a1...], total, world rank of total)}
cumS, cumA = {}, {}             # (person*NE + event) -> best so far


def snapshot(B):
    want = need_at.get(B)
    if not want:
        return
    arrS = [[] for _ in range(NE)]
    arrA = [[] for _ in range(NE)]
    for key, v in cumS.items():
        arrS[key % NE].append(v)
    for key, v in cumA.items():
        arrA[key % NE].append(v)
    for e in range(NE):
        arrS[e].sort(); arrA[e].sort()
    nS = [len(a) for a in arrS]; nA = [len(a) for a in arrA]
    # everyone's Sum of Ranks (combined), to rank the totals
    totals = {}
    have = {key // NE for key in cumS} | {key // NE for key in cumA}
    for pid in have:
        t = 0
        for e in range(NE):
            v = cumS.get(pid * NE + e)
            t += (bisect.bisect_left(arrS[e], v) + 1) if v else nS[e] + 1
            if HAS_AVG[e]:
                v = cumA.get(pid * NE + e)
                t += (bisect.bisect_left(arrA[e], v) + 1) if v else nA[e] + 1
        totals[pid] = t
    sorted_t = sorted(totals.values())
    out = {}
    for pid in want:
        rk = []
        for e in range(NE):
            v = cumS.get(pid * NE + e)
            rk.append((bisect.bisect_left(arrS[e], v) + 1) if v else 0)
            v = cumA.get(pid * NE + e) if HAS_AVG[e] else None
            rk.append((bisect.bisect_left(arrA[e], v) + 1) if v else 0)
        t = totals.get(pid)
        out[pid] = (array('i', rk), t or 0, (bisect.bisect_left(sorted_t, t) + 1) if t else 0)
    snaps[B] = out
    print('snapshot', B, 'people ranked', len(sorted_t))


# ---------- replay history ----------
acc = {}                        # (person, year) -> accumulated stats
TIER = {1: 'WR', 2: 'CR', 3: 'NR'}


def A(pid, y):
    a = acc.get((pid, y))
    if a is None:
        a = acc[(pid, y)] = {'comps': [], 'rounds': 0, 'evr': defaultdict(int), 'ctry': set(), 'mon': defaultdict(int),
                             'pe': {}, 'deb': set(), 'rec': [], 'recseen': set(), 'wins': 0, 'pod': [], 'cdone': set()}
    return a


done_B = set()
boundaries = sorted({y for y in need_at if y < CUR})
pos_ = 0
while pos_ < NR_:
    c = rc[order[pos_]]
    end = pos_
    while end < NR_ and rc[order[end]] == c:
        end += 1
    y, m, d, cname, ccty = comp_info[comp_ids[c]]
    for B in boundaries:                    # take the snapshot just before the first competition after a year ends
        if B not in done_B and y > B:
            snapshot(B); done_B.add(B)
    cb = {}
    for j in range(pos_, end):
        i = order[j]
        pid, e = rpid[i], re_[i]
        key = pid * NE + e
        b, av = rbest[i], ravg[i]
        cur = cb.get(key)
        if cur is None:
            cur = cb[key] = [0, 0]
        if b > 0 and (cur[0] == 0 or b < cur[0]): cur[0] = b
        if av > 0 and (cur[1] == 0 or av < cur[1]): cur[1] = av
        if y in active:
            a = A(pid, y)
            a['rounds'] += 1
            a['evr'][e] += 1
            if c not in a['cdone']:
                a['cdone'].add(c)
                a['comps'].append([cname, f'{m:02d}-{d:02d}', ccty, comp_ids[c]])
                a['ctry'].add(ccty)
                a['mon'][m] += 1
            if rrt[i] and rpos[i] in (1, 2, 3) and b > 0:
                if rpos[i] == 1: a['wins'] += 1
                a['pod'].append([e, rpos[i], cname])
            for kind, t, val in (('s', rrs[i], b), ('a', rra[i], av)):
                if t and val > 0 and (pid, e, kind, t, c) not in a['recseen']:
                    a['recseen'].add((pid, e, kind, t, c))
                    a['rec'].append([TIER[t], kind, e, val, cname])
    for key, (b, av) in cb.items():
        pid, e = divmod(key, NE)
        ps, pa = cumS.get(key), cumA.get(key)
        if y in active and pid in active[y]:
            a = A(pid, y)
            pe = a['pe'].get(e)
            if pe is None:
                pe = a['pe'][e] = [0, 0, ps or 0, ps or 0, pa or 0, pa or 0]    # single PRs, average PRs, s0, s1, a0, a1
                if ps is None and pa is None:
                    a['deb'].add(e)
            if b > 0:
                if ps is not None and b < ps: pe[0] += 1
            if av > 0 and HAS_AVG[e]:
                if pa is not None and av < pa: pe[1] += 1
        if b > 0 and (ps is None or b < ps): cumS[key] = b
        if av > 0 and HAS_AVG[e] and (pa is None or av < pa): cumA[key] = av
        if y in active and pid in active[y]:
            pe = acc[(pid, y)]['pe'][e]
            pe[3] = cumS.get(key, 0); pe[5] = cumA.get(key, 0)
    pos_ = end

for B in boundaries:
    if B not in done_B:
        snapshot(B); done_B.add(B)
snapshot(CUR)

# ---------- write per-person data ----------
def shard_of(w):
    h = 0
    for ch in w:
        h = (h * 31 + ord(ch)) % 1000003
    return h


def fold(t):
    return ''.join(c for c in unicodedata.normalize('NFD', t) if not unicodedata.combining(c)).lower()


def bucket(tok):
    k = tok[:3]
    return k if re.fullmatch(r'[a-z0-9]{2,3}', k) else '_' + str(ord(tok[0]) % 64)


people_y = defaultdict(dict)
dist = {y: {'comps': defaultdict(int), 'rounds': defaultdict(int), 'prs': defaultdict(int), 'countries': defaultdict(int)} for y in YEARS}
tops = {y: {'comps': [], 'prs': [], 'pod': []} for y in YEARS}
for (pid, y), a in acc.items():
    ranks0 = snaps.get(y - 1, {}).get(pid)
    ranks1 = snaps.get(y if y < CUR else CUR, {}).get(pid)
    pr_rows = []
    total_prs = 0
    for e, pe in sorted(a['pe'].items()):
        total_prs += pe[0] + pe[1]
        pr_rows.append([e] + pe)
    ev_rank = []
    if ranks0 and ranks1:
        for e in range(NE):
            r0s, r0a, r1s, r1a = ranks0[0][2 * e], ranks0[0][2 * e + 1], ranks1[0][2 * e], ranks1[0][2 * e + 1]
            if (r0s or r1s or r0a or r1a) and (r0s != r1s or r0a != r1a):
                ev_rank.append([e, r0s, r1s, r0a, r1a])
    months = sorted(a['mon'].items(), key=lambda kv: (-kv[1], kv[0]))
    entry = {
        'k': len(a['comps']), 'r': a['rounds'],
        'cs': a['comps'][:40],
        'ct': len(a['ctry']), 'cl': sorted(a['ctry'])[:20],
        'ev': [[e, n] for e, n in sorted(a['evr'].items(), key=lambda kv: (-kv[1], kv[0]))],
        'db': sorted(a['deb']),
        'pr': pr_rows, 'pt': total_prs,
        'rec': a['rec'][:25], 'rc': [sum(1 for r in a['rec'] if r[0] == t) for t in ('WR', 'CR', 'NR')],
        'w': a['wins'], 'pd': len(a['pod']), 'pl': a['pod'][:12],
        'er': ev_rank,
        'sr': [ranks0[1], ranks0[2], ranks1[1], ranks1[2]] if ranks0 and ranks1 else None,   # Sum of Ranks (combined): total and world rank, start then end
        'mo': months[0] if months else None,
    }
    wid = PIDS[pid]
    people_y[wid][y] = entry
    dist[y]['comps'][len(a['comps'])] += 1
    dist[y]['rounds'][min(a['rounds'], 300)] += 1
    dist[y]['prs'][min(total_prs, 200)] += 1
    dist[y]['countries'][len(a['ctry'])] += 1
    tops[y]['comps'].append((len(a['comps']), wid))
    tops[y]['prs'].append((total_prs, wid))
    tops[y]['pod'].append((len(a['pod']), wid))


import gzip, math
shutil.rmtree(os.path.join(out_dir, 'y'), ignore_errors=True)
shutil.rmtree(os.path.join(out_dir, 'p'), ignore_errors=True)
shutil.rmtree(os.path.join(out_dir, 'i'), ignore_errors=True)
Y0 = YEARS[0]
NSH = {y: max(1, math.ceil(len(active[y]) / 800)) for y in YEARS}     # shards per year, about 800 people each
yshards = defaultdict(lambda: defaultdict(list))
ishards, buckets = defaultdict(list), defaultdict(list)
for pid, ys in people_y.items():
    nm = pname.get(pid, pid).replace('|', '/').replace('\t', ' ')
    ct = pctry.get(pid, '')
    h = shard_of(pid)
    mask = 0
    for y, entry in ys.items():
        mask |= 1 << (y - Y0)
        yshards[y][h % NSH[y]].append(pid + '\t' + nm + '\t' + ct + '\t' + json.dumps(entry, ensure_ascii=False, separators=(',', ':')))
    line = pid + '\t' + nm + '\t' + ct + '\t' + format(mask, 'x')
    ishards[h % 256].append(line)
    seen = set()
    for tok in re.split(r'[^\w]+', fold(nm)):
        if len(tok) < 2:
            continue
        k = bucket(tok)
        if k not in seen:
            seen.add(k); buckets[k].append(line)
nfiles = 0
for y, sh in yshards.items():
    d = os.path.join(out_dir, 'y', str(y))
    os.makedirs(d)
    for k, lines in sh.items():
        with gzip.GzipFile(os.path.join(d, f'{k}.gz'), 'wb', 9, mtime=0) as f:
            f.write('\n'.join(lines).encode('utf-8'))
        nfiles += 1
os.makedirs(os.path.join(out_dir, 'i'))
for k, lines in ishards.items():
    with open(os.path.join(out_dir, 'i', f'{k}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
for k, lines in buckets.items():
    with open(os.path.join(out_dir, 'i', f's_{k}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
shards = {0: [0] * nfiles}


def top_list(lst, n=25):
    lst.sort(key=lambda t: (-t[0], t[1]))
    return [[pid, pname.get(pid, pid), pctry.get(pid, ''), v] for v, pid in lst[:n]]


def dense(d, size):
    a = [0] * (size + 1)
    for k, v in d.items():
        a[min(k, size)] += v
    return a


meta = {
    'updated': stamp, 'asof': today.isoformat(), 'years': YEARS, 'cur': CUR, 'nsh': {str(y): NSH[y] for y in YEARS},
    'events': EV, 'names': NAMES, 'avg': HAS_AVG,
    'dist': {str(y): {'n': len(active[y]), 'comps': dense(dist[y]['comps'], 60), 'rounds': dense(dist[y]['rounds'], 300),
                      'prs': dense(dist[y]['prs'], 200), 'countries': dense(dist[y]['countries'], 30)} for y in YEARS},
    'top': {str(y): {k: top_list(v) for k, v in tops[y].items()} for y in YEARS},
}
with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
    json.dump(meta, f, ensure_ascii=False, separators=(',', ':'))
print('done:', len(people_y), 'people;', nfiles, 'year files; export', stamp)
