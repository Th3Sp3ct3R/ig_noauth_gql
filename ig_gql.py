#!/usr/bin/env python3
"""ig_gql.py - non-auth Instagram profile access via the web GraphQL endpoint (2026-09-02).

Instagram closed the un-authenticated web_profile_info JSON route and the profile HTML page on
2026-09-02, but the logged-out web app itself still fetches profiles through
POST https://www.instagram.com/graphql/query with persisted document ids. This module speaks
that protocol:

    IGWeb.resolve(username) -> {'pk','username','full_name','is_private','is_verified'} | None (not found) | raises Blocked
    IGWeb.profile(pk)       -> dict with follower_count/following_count/media_count/biography/category/
                               external_url/bio_links/profile_pic_url/hd_profile_pic_url/is_business/... | None | raises Blocked

Doc ids are read from the live JS bundles (cached 24h in /tmp/ig_gql_docids.json) so an Instagram
redeploy does not break callers. One IGWeb per exit IP: call warmup() after every proxy rotation.
Serialise access to a shared rotating proxy with your own lock.
CLI:  ig_gql.py resolve <username> | profile <pk> | both <username> | docids   (env S6_PROXY optional)
"""
import json, os, re, sys, time
import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
APP_ID = '936619743392459'
DOC_CACHE = '/tmp/ig_gql_docids.json'
DOC_TTL = 24 * 3600
OPS = ('PolarisProfilePageContentQuery', 'PolarisProfilePostsQuery')
PV = '__relay_internal__pv__Polaris'
PROFILE_FIELDS = ('pk', 'username', 'full_name', 'biography', 'follower_count', 'following_count', 'media_count', 'is_private',
                  'is_verified', 'is_business', 'is_professional_account', 'category', 'external_url', 'bio_links', 'profile_pic_url',
                  'city_name', 'address_street', 'pronouns', 'account_type', 'has_profile_pic', 'fbid_v2')


class Blocked(Exception):
    """IP/session is being throttled or walled: rotate the proxy, re-warm, retry."""


def _load_docids(session=None, force=False):
    try:
        if not force and os.path.exists(DOC_CACHE) and time.time() - os.path.getmtime(DOC_CACHE) < DOC_TTL:
            d = json.load(open(DOC_CACHE))
            if all(op in d for op in OPS):
                return d
    except Exception:
        pass
    s = session or requests.Session()
    html = s.get('https://www.instagram.com/instagram/', headers={'User-Agent': UA}, timeout=30).text
    srcs = sorted(set(re.findall(r'(https://static\.cdninstagram\.com/rsrc\.php/[^"\']+\.js[^"\']*)', html)))
    found = {}
    for src in srcs:
        try:
            js = s.get(src, headers={'User-Agent': UA}, timeout=40).text
        except Exception:
            continue
        for op in OPS:
            if op in found:
                continue
            m = re.search(r'__d\("%s_instagramRelayOperation",\[\],\(function\([^)]*\)\{[a-z]\.exports="(\d{15,17})"' % op, js)
            if m:
                found[op] = m.group(1)
        if len(found) == len(OPS):
            break
    if len(found) != len(OPS):
        raise RuntimeError('could not find doc ids in bundles: %s' % found)
    try:
        json.dump(found, open(DOC_CACHE, 'w'))
        os.chmod(DOC_CACHE, 0o664)
    except Exception:
        pass
    return found


class IGWeb:
    def __init__(self, proxy=None, timeout=30):
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {'http': proxy, 'https': proxy}
        self.s.headers.update({'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9',
                               'sec-ch-ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                               'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"'})
        self.timeout = timeout
        self.form = None
        self.docs = _load_docids(self.s)

    def warmup(self):
        """Fetch the logged-out shell once per IP: cookies + the form tokens every GraphQL call needs."""
        h = self.s.get('https://www.instagram.com/instagram/', timeout=self.timeout,
                       headers={'Accept': 'text/html,*/*;q=0.8', 'Upgrade-Insecure-Requests': '1'})
        if h.status_code != 200:
            raise Blocked('shell %s' % h.status_code)
        html = h.text

        def pick(pat):
            m = re.search(pat, html)
            return m.group(1) if m else ''
        lsd = pick(r'"LSD",\[\],\{"token":"([^"]+)"')
        if not lsd:
            raise Blocked('no lsd token in shell')
        self.form = {'av': '0', '__d': 'www', '__user': '0', '__a': '1', '__req': 'b', '__hs': pick(r'"haste_session":"([^"]+)"'),
                     'dpr': '1', '__ccg': 'EXCELLENT', '__rev': pick(r'"server_revision":(\d+)'), '__s': 'abcdef:ghijkl:mnopqr',
                     '__hsi': pick(r'"hsi":"(\d+)"'), '__dyn': '', '__csr': '', '__comet_req': '7', 'lsd': lsd,
                     'jazoest': '2' + str(sum(ord(c) for c in lsd)), '__spin_r': pick(r'"__spin_r":(\d+)'),
                     '__spin_b': pick(r'"__spin_b":"([^"]+)"'), '__spin_t': pick(r'"__spin_t":(\d+)'),
                     'fb_api_caller_class': 'RelayModern', 'server_timestamps': 'true'}
        self.hdr = {'x-fb-lsd': lsd, 'x-csrftoken': self.s.cookies.get('csrftoken') or '', 'x-ig-app-id': APP_ID, 'x-asbd-id': '129477',
                    'x-ig-www-claim': '0', 'Content-Type': 'application/x-www-form-urlencoded', 'Referer': 'https://www.instagram.com/',
                    'Origin': 'https://www.instagram.com', 'Accept': '*/*', 'sec-fetch-site': 'same-origin', 'sec-fetch-mode': 'cors',
                    'sec-fetch-dest': 'empty'}
        return self

    def _gql(self, op, variables):
        if self.form is None:
            self.warmup()
        data = dict(self.form, fb_api_req_friendly_name=op, variables=json.dumps(variables), doc_id=self.docs[op])
        r = self.s.post('https://www.instagram.com/graphql/query', headers=dict(self.hdr, **{'x-fb-friendly-name': op}),
                        data=data, timeout=self.timeout)
        if r.status_code in (401, 429):
            raise Blocked('http %s' % r.status_code)
        txt = r.text
        if txt.startswith('for (;;);'):
            txt = txt[9:]
        try:
            j = json.loads(txt)
        except ValueError:
            raise Blocked('non-json %s %s' % (r.status_code, txt[:80]))
        if 'require_login' in txt or 'Unauthorized logged out' in txt:
            raise Blocked('login wall')
        return j

    def clone(self):
        """A second IGWeb (own keep-alive socket) sharing this one's warmed tokens/cookies.
        Threaded scrapers keep one clone per worker; re-clone after every warmup()."""
        c = IGWeb.__new__(IGWeb)
        c.s = requests.Session()
        c.s.proxies = dict(self.s.proxies)
        c.s.headers.update(self.s.headers)
        c.s.cookies.update(self.s.cookies)
        c.timeout = self.timeout
        c.docs = self.docs
        c.form = dict(self.form) if self.form else None
        c.hdr = dict(self.hdr) if self.form else None
        return c

    def resolve(self, username, posts=1):
        """username -> owner dict or None when Instagram says the user does not exist.
        The dict carries the raw post edges under 'edges' (up to `posts`)."""
        v = {'username': username, 'data': {'count': posts, 'include_reel_media_seen_timestamp': True, 'include_relationship_info': True,
                                            'latest_besties_reel_media': True, 'latest_reel_media': True},
             'first': posts, 'after': None, 'before': None, 'last': None,
             PV + 'MultiCaptionCarouselEnabledrelayprovider': False, PV + 'ShortDramaEnabledrelayprovider': False,
             PV + 'ReelsRecoDebugOverlayEnabledrelayprovider': False}
        j = self._gql('PolarisProfilePostsQuery', v)
        errs = j.get('errors') or []
        if any('User lookup returned null' in (e.get('description') or e.get('message') or '') for e in errs):
            return None
        conn = (j.get('data') or {}).get('xdt_api__v1__feed__user_timeline_graphql_connection') or {}
        edges = conn.get('edges') or []
        if not edges:
            # private or zero-post account: existence is known, pk is not exposed on this route
            return {'pk': None, 'username': username, 'no_posts_visible': True, 'edges': []}
        u = edges[0]['node'].get('user') or edges[0]['node'].get('owner') or {}
        out = {k: u.get(k) for k in ('pk', 'username', 'full_name', 'is_private', 'is_verified', 'profile_pic_url')}
        out['edges'] = edges
        return out

    def check(self, username):
        """Existence only: 'dead' (Instagram says no such user) | 'alive' | 'nopk' (exists,
        private/zero-post). Raises Blocked. Used by the slave dead-checks."""
        r = self.resolve(username)
        if r is None:
            return 'dead'
        return 'alive' if r.get('pk') else 'nopk'

    @staticmethod
    def _legacy_node(n):
        cap = (n.get('caption') or {}).get('text') or ''
        return {'node': {'id': n.get('pk') or n.get('id'), 'shortcode': n.get('code'),
                         'taken_at_timestamp': n.get('taken_at') or (n.get('caption') or {}).get('created_at'),
                         'is_video': n.get('media_type') == 2, 'product_type': n.get('product_type') or '',
                         'edge_liked_by': {'count': n.get('like_count') or 0},
                         'edge_media_preview_like': {'count': n.get('like_count') or 0},
                         'edge_media_to_comment': {'count': n.get('comment_count') or 0},
                         'edge_media_to_caption': {'edges': [{'node': {'text': cap}}] if cap else []}}}

    def wpi_user(self, username, posts=1):
        """Legacy web_profile_info `user` dict rebuilt from GraphQL, so old extractors keep working.
        Returns None when the user does not exist, or {'_no_pk': True, 'username': u} when the
        handle exists but exposes no pk logged-out (private / zero posts). Raises Blocked."""
        r = self.resolve(username, posts=posts)
        if r is None:
            return None
        if not r.get('pk'):
            return {'_no_pk': True, 'username': username, 'id': None}
        p = self.profile(r['pk']) or {}
        edges = [self._legacy_node(e.get('node') or {}) for e in r.get('edges') or []]
        return {
            'id': str(r['pk']), 'pk': str(r['pk']), 'username': p.get('username') or r.get('username') or username,
            'full_name': p.get('full_name') or r.get('full_name') or '', 'biography': p.get('biography') or '',
            'profile_pic_url': p.get('profile_pic_url') or r.get('profile_pic_url') or '',
            'profile_pic_url_hd': p.get('hd_profile_pic_url') or p.get('profile_pic_url') or r.get('profile_pic_url') or '',
            'edge_followed_by': {'count': p.get('follower_count') or 0}, 'edge_follow': {'count': p.get('following_count') or 0},
            'edge_owner_to_timeline_media': {'count': p.get('media_count') or 0, 'edges': edges},
            'is_verified': bool(p.get('is_verified') if p.get('is_verified') is not None else r.get('is_verified')),
            'is_private': bool(p.get('is_private')), 'is_business_account': bool(p.get('is_business')),
            'is_professional_account': bool(p.get('is_professional_account')),
            'category_name': p.get('category') or '', 'business_category_name': p.get('category') or '',
            'external_url': p.get('external_url') or '', 'city_name': p.get('city_name') or '',
            'business_address_json': '', 'fbid': p.get('fbid_v2') or '', 'bio_links': p.get('bio_links') or [],
            '_source': 'gql',
        }

    def profile(self, pk):
        v = {'id': str(pk), 'enable_integrity_filters': True, PV + 'CASB976ProfileEnabledrelayprovider': False,
             PV + 'CannesGuardianExperienceEnabledrelayprovider': False, PV + 'RepostsConsumptionEnabledrelayprovider': False,
             PV + 'ShortDramaEnabledrelayprovider': False, PV + 'WebSchoolsEnabledrelayprovider': False}
        j = self._gql('PolarisProfilePageContentQuery', v)
        u = (j.get('data') or {}).get('user')
        if not u:
            if j.get('errors'):
                msg = json.dumps(j['errors'])[:200]
                if 'missing_required' in msg or 'execution error' in msg:
                    raise RuntimeError('profile query rejected (doc ids stale?): ' + msg)
            return None
        out = {k: u.get(k) for k in PROFILE_FIELDS}
        hd = u.get('hd_profile_pic_url_info') or {}
        out['hd_profile_pic_url'] = hd.get('url')
        return out


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'docids'
    ig = IGWeb(proxy=os.environ.get('S6_PROXY'))
    if cmd == 'docids':
        print(json.dumps(_load_docids(ig.s, force=True)))
        sys.exit(0)
    ig.warmup()
    if cmd == 'resolve':
        print(json.dumps(ig.resolve(sys.argv[2])))
    elif cmd == 'profile':
        print(json.dumps(ig.profile(sys.argv[2]), ensure_ascii=False)[:1500])
    elif cmd == 'both':
        r = ig.resolve(sys.argv[2]); print('resolve:', json.dumps(r))
        if r and r.get('pk'):
            print('profile:', json.dumps(ig.profile(r['pk']), ensure_ascii=False)[:1500])
