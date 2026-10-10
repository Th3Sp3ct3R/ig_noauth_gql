# Instagram profile scraping without login: what changed on 2 Sep 2026 and how we fixed it

## What Instagram changed
On 2 September Instagram shut every logged-out way of reading a profile:
- The JSON endpoint everyone used, `api/v1/users/web_profile_info/?username=...`, now answers
  401 or 429 with "Please wait a few minutes... require_login".
- The profile page `instagram.com/<user>/` still returns 200, but the body is a 620 KB empty
  JavaScript shell with no name, counts or bio in it. `/embed/` is the same shell.
- This is not a proxy or rate-limit problem: a fresh residential home connection that had never
  scraped anything gets exactly the same answers on its very first request.

## How the logged-out web app still shows profiles
When you open a profile without being logged in, the web app no longer gets data from the
server-rendered page. Its JavaScript calls `POST https://www.instagram.com/graphql/query` with
"persisted queries": numeric document ids that stand for a stored GraphQL query, plus a
`variables` JSON. Two of them give everything a profile scraper needs:

1. `PolarisProfilePostsQuery` takes a `username` and returns the account's posts. The owner block
   inside the first post carries the numeric account id (`pk`), the name and the private/verified
   flags. If the handle does not exist the server says "User lookup returned null".
2. `PolarisProfilePageContentQuery` takes that numeric `id` and returns the full profile: follower
   and following counts, biography, category, external URL, bio links, HD picture, business and
   professional flags, city. It works for private accounts too.

Three details make or break it:
- Every call needs tokens that the (empty) shell page still hands out: `lsd` (also as the
  `x-fb-lsd` header), `__spin_r/_b/_t`, `__hs`, `__rev`, `__hsi`, `__comet_req=7`, the csrf cookie,
  `x-ig-app-id 936619743392459`. Without them the endpoint answers "Your request couldn't be
  processed" (error 1357054).
- The variables must include five internal feature flags named
  `__relay_internal__pv__Polaris...relayprovider` set to false. Miss one and the server answers
  `missing_required_variable_value`.
- The document ids change whenever Instagram redeploys. They sit in the page's JavaScript bundles
  as `__d("<QueryName>_instagramRelayOperation",[],(function(...){a.exports="<id>"}))`, so the
  helper reads them from the live bundles and caches them for a day instead of hardcoding them.

## Why it is faster than before, not just working again
Measured with 8 parallel workers on one rotating mobile exit IP:
- 400 profile calls in 15 seconds and 160 username lookups in 21 seconds without a single block.
  The budget is roughly 500 calls per IP before 429s start. The old JSON route blocked after about
  60 requests per IP, so we now rotate about 8 times less often.
- A rotation plus re-warm now costs about 1 second (one small page load for tokens) instead of
  the ~19 seconds the old route needed to rebuild a browser-fingerprinted session.
- A profile answer is about 4 KB of JSON instead of a 700 KB page, so the proxy line is far less
  loaded and workers spend their time on requests, not downloads.
Net effect in production: about 280 handles per minute sustained for hours with zero errors
(two calls per handle), against 85 to 130 per minute on the old route.

## What is no longer available logged-out
- The id of a handle that shows no posts: private accounts, public accounts with zero posts, and
  deleted ones all come back as "no posts". Existence is still known, the profile is not.
- Post count, engagement rate and last-post date.
Everything else is there, and the picture URL comes with the profile, so avatars need no extra call.

## Files
- `ig_gql.py` - the helper: `IGWeb(proxy).warmup()`, `resolve(username)`, `profile(pk)`,
  `check(username)` (dead / alive / nopk), `clone()` for threads, `Blocked` exception on 401/429.
- `ig_noauth_scrape.py` - example bulk scraper: N workers, rotate every ~140 handles, retry a
  window once on a fresh IP, JSONL output, resumable.
