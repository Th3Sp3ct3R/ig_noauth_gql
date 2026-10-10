#!/usr/bin/env python3
"""ig_noauth_scrape.py - bulk profile scraper on the logged-out Instagram GraphQL route, CSV output.

    py ig_noauth_scrape.py usernames.txt out.xlsx [--workers 4] [--per-ip 140]
    py ig_noauth_scrape.py usernames.txt out.xlsx --discover found.txt   # also harvest new usernames from posts
    py ig_noauth_scrape.py --raw someuser        # dump every raw profile field (debugging)

Env (all optional):
    IG_PROXY   e.g. socks5h://user:pass@host:port   (rotating residential/mobile proxy recommended)
    IG_ROTATE  URL that makes the proxy switch exit IP (called between windows)

Output columns: username, status, bio, email, phone, followers, following, posts,
    likes_post1..5, date_post1..5, full_name, verified, private, external_url

status: ok | not_found | no_posts_visible (private/zero posts: no id logged-out) | blocked | error
Output: a formatted .xlsx (if the name ends with .xlsx, needs openpyxl - installed automatically)
or a ;-separated CSV otherwise. Resumable: usernames already present in the output are skipped.
"""
import argparse, csv, datetime, json, os, re, sys, time, threading, queue
import requests
from ig_gql import IGWeb, Blocked, PV

NPOSTS = 5
COLUMNS = (['username', 'status', 'bio', 'email', 'phone', 'followers', 'following', 'posts']
           + [f'likes_post{i}' for i in range(1, NPOSTS + 1)] + [f'date_post{i}' for i in range(1, NPOSTS + 1)]
           + ['full_name', 'verified', 'private', 'external_url'])
MENTION_RE = re.compile(r'(?<![\w.])@([a-zA-Z0-9._]{2,30})(?![\w])')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
PHONE_RE = re.compile(r'(?<![\w/])\+?\d[\d\s().-]{7,}\d(?![\w/])')
EMAIL_FIELDS = ('public_email', 'business_email', 'email')
PHONE_FIELDS = ('contact_phone_number', 'public_phone_number', 'business_phone_number', 'phone_number')


def raw_profile(ig, pk):
    """Full user dict from PolarisProfilePageContentQuery (ig_gql.profile() keeps only a subset)."""
    v = {'id': str(pk), 'enable_integrity_filters': True}
    for k in ('CASB976ProfileEnabledrelayprovider', 'CannesGuardianExperienceEnabledrelayprovider',
              'RepostsConsumptionEnabledrelayprovider', 'ShortDramaEnabledrelayprovider', 'WebSchoolsEnabledrelayprovider'):
        v[PV + k] = False
    j = ig._gql('PolarisProfilePageContentQuery', v)
    return (j.get('data') or {}).get('user') or {}


def first(d, keys):
    for k in keys:
        if d.get(k):
            return str(d[k]).strip()
    return ''


def ts(node):
    t = node.get('taken_at') or (node.get('caption') or {}).get('created_at')
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d') if t else ''


def likes(node):
    n = node.get('like_count')
    if n is None:
        return 'hidden' if node.get('like_and_view_counts_disabled') else ''
    return n


def discover(edges):
    """Usernames visible in a list of post nodes: tagged users, co-authors, @mentions in captions."""
    found = set()
    for n in edges:
        for t in ((n.get('usertags') or {}).get('in') or []):
            h = (t.get('user') or {}).get('username')
            if h: found.add(h.lower())
        for c in (n.get('coauthor_producers') or []):
            h = c.get('username')
            if h: found.add(h.lower())
        cap = (n.get('caption') or {}).get('text') or ''
        found.update(m.lower().rstrip('.') for m in MENTION_RE.findall(cap))
    return found


def scrape_one(ig, username):
    r = ig.resolve(username, posts=NPOSTS)  # raises Blocked
    if r is None:
        return {'username': username, 'status': 'not_found'}
    if not r.get('pk'):
        return {'username': username, 'status': 'no_posts_visible'}
    u = raw_profile(ig, r['pk'])
    bio = (u.get('biography') or '').strip()
    email = first(u, EMAIL_FIELDS)
    if not email:
        m = EMAIL_RE.search(bio + ' ' + (u.get('external_url') or '')); email = m.group(0) if m else ''
    phone = first(u, PHONE_FIELDS)
    if not phone:
        m = PHONE_RE.search(bio); phone = m.group(0).strip() if m else ''
    edges = [e.get('node') or {} for e in r.get('edges') or []]
    posts = u.get('media_count')
    per_post = {}
    for i in range(NPOSTS):
        p = edges[i] if i < len(edges) else {}
        per_post[f'likes_post{i + 1}'] = likes(p)
        per_post[f'date_post{i + 1}'] = ts(p)
    return {
        '_found': discover(edges),
        'username': username, 'status': 'ok',
        'bio': bio.replace('\r', '').replace('\n', ' | '),
        'email': email, 'phone': phone,
        'followers': u.get('follower_count', ''), 'following': u.get('following_count', ''),
        'posts': '' if posts is None else posts,   # not exposed logged-out on this route
        **per_post,
        'full_name': u.get('full_name') or r.get('full_name') or '',
        'verified': bool(u.get('is_verified', r.get('is_verified'))),
        'private': bool(u.get('is_private')),
        'external_url': u.get('external_url') or '',
    }


HEADERS = {'username': 'Username', 'status': 'Status', 'bio': 'Bio', 'email': 'Email', 'phone': 'Phone',
           'followers': 'Followers', 'following': 'Following', 'posts': 'Posts',
           'full_name': 'Full name', 'verified': 'Verified', 'private': 'Private', 'external_url': 'Link'}
HEADERS.update({f'likes_post{i}': f'Likes post {i}' for i in range(1, NPOSTS + 1)})
HEADERS.update({f'date_post{i}': f'Date post {i}' for i in range(1, NPOSTS + 1)})
WIDTHS = {'username': 22, 'status': 12, 'bio': 60, 'email': 28, 'phone': 18, 'followers': 13, 'following': 12,
          'posts': 9,
          'full_name': 26, 'verified': 10, 'private': 10, 'external_url': 36}
WIDTHS.update({f'likes_post{i}': 13 for i in range(1, NPOSTS + 1)})
WIDTHS.update({f'date_post{i}': 12 for i in range(1, NPOSTS + 1)})
NUMERIC = ('followers', 'following', 'posts') + tuple(f'likes_post{i}' for i in range(1, NPOSTS + 1))


class CsvSink:
    """;-separated CSV (opens in columns in Excel with a Russian/European locale)."""
    def __init__(self, path):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.path = path
        if not new:
            with open(path, encoding='utf-8-sig', newline='') as f:
                rows = list(csv.reader(f, delimiter=';'))
            keep = [r for r in rows if not (len(r) > 1 and (r[1] == 'blocked' or r[1].startswith('error')))]
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                csv.writer(f, delimiter=';').writerows(keep)
        self.f = open(path, 'a', encoding='utf-8-sig', newline='')
        self.w = csv.DictWriter(self.f, fieldnames=COLUMNS, extrasaction='ignore', delimiter=';')
        if new:
            self.w.writerow(HEADERS); self.f.flush()

    def existing(self):
        if os.path.getsize(self.path) == 0:
            return set()
        with open(self.path, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f, delimiter=';'))
        return {r[0] for r in rows[1:] if r and len(r) > 1 and r[1] not in ('blocked',) and not r[1].startswith('error')}

    def write(self, row):
        self.w.writerow(row); self.f.flush()

    def close(self):
        self.f.close()


class XlsxSink:
    """Formatted Excel workbook: styled header, frozen top row, filters, thousands separators, sized columns."""
    def __init__(self, path):
        try:
            import openpyxl
        except ImportError:
            import subprocess
            print('installing openpyxl...', flush=True)
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'openpyxl'])
            import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        self.path = path
        self.Font, self.Alignment = Font, Alignment
        self.thin = Border(*[Side(style='thin', color='D9D9D9')] * 4)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self.wb = openpyxl.load_workbook(path)
            self.ws = self.wb.active
            for r in range(self.ws.max_row, 1, -1):
                st = str(self.ws.cell(row=r, column=2).value or '')
                if st == 'blocked' or st.startswith('error'):
                    self.ws.delete_rows(r)
        else:
            self.wb = openpyxl.Workbook(); self.ws = self.wb.active; self.ws.title = 'Profiles'
            self.ws.append([HEADERS[c] for c in COLUMNS])
            for i, c in enumerate(COLUMNS, 1):
                cell = self.ws.cell(row=1, column=i)
                cell.font = Font(name='Arial', bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='305496')
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                cell.border = self.thin
                self.ws.column_dimensions[cell.column_letter].width = WIDTHS[c]
            self.ws.row_dimensions[1].height = 30
            self.ws.freeze_panes = 'A2'
            self.wb.save(path)

    def existing(self):
        return {r[0] for r in self.ws.iter_rows(min_row=2, max_col=2, values_only=True)
                if r[0] and r[1] != 'blocked' and not str(r[1]).startswith('error')}

    def write(self, row):
        r = self.ws.max_row + 1
        for i, c in enumerate(COLUMNS, 1):
            v = row.get(c, '')
            if isinstance(v, bool):
                v = 'yes' if v else 'no'
            cell = self.ws.cell(row=r, column=i, value=v)
            cell.font = self.Font(name='Arial', size=10)
            cell.border = self.thin
            if c in NUMERIC and isinstance(v, (int, float)):
                cell.number_format = '#,##0'
                cell.alignment = self.Alignment(horizontal='right')
            elif c == 'bio':
                cell.alignment = self.Alignment(wrap_text=True, vertical='top')
            else:
                cell.alignment = self.Alignment(vertical='top')
        self.ws.auto_filter.ref = f'A1:{self.ws.cell(row=1, column=len(COLUMNS)).column_letter}{r}'
        self.wb.save(self.path)

    def close(self):
        self.wb.save(self.path)


def fmt_elapsed(sec):
    sec = int(round(sec)); h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f'{h} h {m} min {s} s' if h else (f'{m} min {s} s' if m else f'{s} s')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('infile', nargs='?'); ap.add_argument('outfile', nargs='?', default='out.xlsx')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--per-ip', type=int, default=140, help='handles per exit IP before rotating')
    ap.add_argument('--raw', metavar='USERNAME', help='print the raw profile dict for one user and exit')
    ap.add_argument('--discover', metavar='FILE', help='append usernames found in scraped posts (tags, co-authors, @mentions) to FILE')
    a = ap.parse_args()
    PROXY = os.environ.get('IG_PROXY'); ROTATE = os.environ.get('IG_ROTATE')

    if a.raw:
        ig = IGWeb(proxy=PROXY).warmup()
        r = ig.resolve(a.raw, posts=NPOSTS)
        print('resolve:', json.dumps({k: v for k, v in (r or {}).items() if k != 'edges'}, ensure_ascii=False))
        if r and r.get('pk'):
            print(json.dumps(raw_profile(ig, r['pk']), ensure_ascii=False, indent=1))
        return
    if not a.infile:
        ap.error('infile is required')

    names = [l.strip().lstrip('@') for l in open(a.infile, encoding='utf-8') if l.strip()]
    sink = XlsxSink(a.outfile) if a.outfile.lower().endswith('.xlsx') else CsvSink(a.outfile)
    done = sink.existing()
    todo = [n for n in names if n not in done]
    print(f'{len(todo)} to do ({len(done)} already in {a.outfile})', flush=True)
    out_lock = threading.Lock()
    counter = [len(done)]
    nonlocal_count = [0]
    total = len(names)
    known = set(names) | done
    disc_f = None
    if a.discover:
        if os.path.exists(a.discover):
            known |= {l.strip().lower() for l in open(a.discover, encoding='utf-8') if l.strip()}
        disc_f = open(a.discover, 'a', encoding='utf-8')
    disc_n = [0]

    def rotate():
        if ROTATE:
            try: requests.get(ROTATE, timeout=40)
            except Exception as e: print('rotate failed:', e, flush=True)
            time.sleep(3)

    def write(row):
        nonlocal_count[0] += 1
        with out_lock:
            sink.write(row)
            counter[0] += 1
            n = counter[0]
            if disc_f is not None:
                new = sorted(h for h in row.get('_found', ()) if h not in known)
                for h in new:
                    disc_f.write(h + '\n'); known.add(h)
                disc_n[0] += len(new); disc_f.flush()
        print(f'[{n}/{total}] {row["username"]}: {row["status"]}', flush=True)

    def run_window(base, window):
        q = queue.Queue(); [q.put(u) for u in window]
        retry, lock = [], threading.Lock()

        def worker():
            ig = base.clone()
            while True:
                try: u = q.get_nowait()
                except queue.Empty: return
                try:
                    rec = scrape_one(ig, u)
                except Blocked:
                    with lock: retry.append(u)
                    continue
                except Exception as e:
                    rec = {'username': u, 'status': 'error: ' + str(e)[:80]}
                write(rec)
        ths = [threading.Thread(target=worker) for _ in range(a.workers)]
        [t.start() for t in ths]; [t.join() for t in ths]
        return retry

    t0 = time.time(); i = 0; processed = 0
    while i < len(todo):
        window = todo[i:i + a.per_ip]; i += len(window)
        base = IGWeb(proxy=PROXY).warmup()
        retry = run_window(base, window)
        if retry and not ROTATE:
            left = len(retry) + len(todo) - i
            print(f'\nBLOCKED: Instagram rate-limited this IP. {left} usernames left unprocessed.\n'
                  f'Wait a few hours (or set IG_PROXY/IG_ROTATE) and run the same command again - it will resume.', flush=True)
            break
        if retry:
            rotate(); base = IGWeb(proxy=PROXY).warmup()
            for u in run_window(base, retry):
                write({'username': u, 'status': 'blocked'})
        rotate()
        rate = i / max(1e-9, (time.time() - t0) / 60)
        print(f'--- {rate:.0f} handles/min', flush=True)
    sink.close()
    if disc_f is not None:
        disc_f.close()
        print(f'Discovered {disc_n[0]} new usernames -> {a.discover} ({len(known)} known in total)')
    elapsed = time.time() - t0
    n = nonlocal_count[0]
    print(f'\nProcessed {n} of {len(todo)} usernames in {fmt_elapsed(elapsed)}'
          + (f' ({n / (elapsed / 60):.0f} per minute)' if n and elapsed > 1 else '')
          + f' -> {a.outfile} ({counter[0]}/{total} total)')


if __name__ == '__main__':
    main()
