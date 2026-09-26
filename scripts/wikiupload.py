#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一個文字檔的內容寫到 MediaWiki 的某個頁面

    python3 tools/wikiupload.py \\
        --site zh.wikisource.org \\
        --title 'User:PacmanD/sikuquanshu-catalog' \\
        --file out/books/catalog.wikitext \\
        --credentials ~/frank-stuffs/library_backup/state/commons-credentials.json \\
        --summary '書目名單（由 .xdf 書目索引生成）'

憑據檔是 `{"username": "帳號@BotName", "password": "..."}`，即 Special:BotPasswords
生成的那一組；pywikibot 的 password-file（`('帳號', BotPassword('BotName', '...'))`）也行。BotPassword 對整個 SUL 帳號有效，Commons 上建的同樣能編維基文庫。
密碼只在登入請求裡出現，不寫日誌、不回顯。

`--dry-run` 只比對，不提交。頁面已存在且內容相同時直接跳過，不留空編輯。

批次模式：`--dir out/wiki/0793` 把目錄裡 `0793-NNN.wikitext` 逐個對應到
`Page:文淵閣四庫全書 0793冊.djvu/NNN`。

    python3 tools/wikiupload.py --dir out/wiki/0793 --existing-only --skip 6,19,21 \
        --credentials ... --summary '...' [--dry-run]

`--existing-only` 只刷新線上已有的頁（不新建）；線上頁最後編輯者不是
機器人自己的，預設跳過並列出（`--overwrite-human` 才覆寫）；`--skip` 再指定
不碰的頁；`--pages 456-536` 只處理一段；`--no-plates` 跳過整頁圖版。線上頁面的 `<pagequality level= user= />` 標頭一律原樣保留，
機器人不會把別人標的校對等級退回去。
"""
import argparse, gzip, http.cookiejar, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

UA = ('sikuquanshu-catalog/0.1 (https://zh.wikisource.org/wiki/User:CollationBot; '
      'henryzhou2008@gmail.com) python-urllib')


class Wiki:
    def __init__(self, site, ua=UA, maxlag=5):
        self.endpoint = 'https://%s/w/api.php' % site
        self.maxlag = maxlag
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.ua = ua
        self._csrf = None
        self._cred = None      # (user, pw)，只留在記憶體裡，供 session 過期時重新登入

    def api(self, params, method='POST'):
        p = {'format': 'json', 'formatversion': '2', 'maxlag': self.maxlag, **params}
        body = urllib.parse.urlencode(p).encode('utf-8')
        backoff = 5
        for attempt in range(8):
            try:
                if method == 'GET':
                    req = urllib.request.Request(
                        self.endpoint + '?' + urllib.parse.urlencode(p),
                        headers={'User-Agent': self.ua, 'Accept-Encoding': 'gzip'})
                else:
                    req = urllib.request.Request(
                        self.endpoint, data=body, method='POST',
                        headers={'User-Agent': self.ua, 'Accept-Encoding': 'gzip',
                                 'Content-Type': 'application/x-www-form-urlencoded'})
                with self.opener.open(req, timeout=120) as r:
                    raw = r.read()
                    if r.headers.get('Content-Encoding') == 'gzip':
                        raw = gzip.decompress(raw)
                d = json.loads(raw.decode('utf-8'))
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                print('  重試 %d/8（%.0fs）：%r' % (attempt + 1, backoff, e), file=sys.stderr)
                time.sleep(backoff); backoff = min(backoff * 2, 300); continue
            err = d.get('error')
            if err and err.get('code') in ('assertuserfailed', 'badtoken') and self._cred:
                # 長批次跑到一半 session 過期（幾百次編輯後會發生），重新登入再來
                print('  %s，重新登入' % err['code'], file=sys.stderr)
                self.login(*self._cred)
                p['token'] = self.csrf() if 'token' in p else None
                if p.get('token') is None:
                    p.pop('token', None)
                body = urllib.parse.urlencode(p).encode('utf-8')
                continue
            if err and err.get('code') in ('maxlag', 'readonly', 'ratelimited',
                                           'internal_api_error'):
                print('  %s，等 %.0fs' % (err.get('code'), backoff), file=sys.stderr)
                time.sleep(backoff); backoff = min(backoff * 2, 300); continue
            if err:
                raise SystemExit('API 錯誤 %s: %s' % (err.get('code'), err.get('info')))
            return d
        raise SystemExit('重試耗盡')

    def login(self, user, pw):
        tok = self.api({'action': 'query', 'meta': 'tokens', 'type': 'login'},
                       'GET')['query']['tokens']['logintoken']
        j = self.api({'action': 'login', 'lgname': user, 'lgpassword': pw,
                      'lgtoken': tok})
        if j['login']['result'] != 'Success':
            raise SystemExit('登入失敗：%s' % j['login'].get('reason', j['login']))
        self._csrf = None
        self._cred = (user, pw)
        return j['login']['lgusername']

    def csrf(self):
        if not self._csrf:
            self._csrf = self.api({'action': 'query', 'meta': 'tokens'},
                                  'GET')['query']['tokens']['csrftoken']
        return self._csrf

    def content(self, title):
        j = self.api({'action': 'query', 'prop': 'revisions', 'rvprop': 'content',
                      'rvslots': 'main', 'titles': title}, 'GET')
        pg = j['query']['pages'][0]
        if pg.get('missing'):
            return None
        return pg['revisions'][0]['slots']['main']['content']

    def contents_by_prefix(self, ns, prefix):
        """→ {標題: (內容, 最後編輯者)}，一次 50 頁；批次比對用，免得逐頁 GET 幾百次。"""
        out, cont = {}, {}
        while True:
            j = self.api({'action': 'query', 'generator': 'allpages', 'gapnamespace': ns,
                          'gapprefix': prefix, 'gaplimit': 50, 'prop': 'revisions',
                          'rvprop': 'content|user', 'rvslots': 'main', **cont}, 'GET')
            for pg in j.get('query', {}).get('pages', []):
                r = pg['revisions'][0]
                out[pg['title']] = (r['slots']['main']['content'], r['user'])
            if 'continue' not in j:
                return out
            cont = j['continue']

    def edit(self, title, text, summary):
        return self.api({'action': 'edit', 'title': title, 'text': text,
                         'summary': summary, 'bot': 1, 'token': self.csrf(),
                         'assert': 'user'})['edit']


def load_credentials(path, account=None):
    """兩種格式：JSON `{"username": "帳號@BotName", "password": "..."}`，
    或 pywikibot 的 password-file `('帳號', BotPassword('BotName', '...'))`，
    後者可放多行；`account` 指定取哪個帳號（不給就取第一行）。"""
    raw = open(os.path.expanduser(path), encoding='utf-8').read()
    if raw.lstrip().startswith('{'):
        d = json.loads(raw)
        return {'username': d['username'], 'password': d['password']}
    found = re.findall(r"""\(\s*['"]([^'"]+)['"]\s*,\s*BotPassword\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""", raw)
    if not found:
        raise SystemExit('看不懂憑據檔格式：%s' % path)
    if account:
        found = [f for f in found if f[0] == account]
        if not found:
            raise SystemExit('憑據檔裡沒有帳號 %s' % account)
    user, bot, pw = found[0]
    return {'username': '%s@%s' % (user, bot), 'password': pw}


PQ = re.compile(r'<pagequality level="\d" user="[^"]*" />')
NS = 'Page:文淵閣四庫全書 %04d冊.djvu/%d'


def keep_quality(text, cur):
    """新內容套用線上頁面現有的 <pagequality> 標頭。"""
    m = PQ.search(cur or '')
    return PQ.sub(m.group(0), text, count=1) if m else text


def put(w, title, text, summary, dry_run, existing_only=False, cache=None,
        who=None, overwrite_human=False):
    """→ 'same' | 'new' | 'edit' | 'absent' | 'human'。cache 給了就不再逐頁 GET。
    線上頁最後編輯者不是自己（who）的，預設不碰：那是人工校對過的。"""
    if cache is not None:
        cur, last = cache.get(title, (None, None))
    else:
        cur, last = w.content(title), None
    if cur is None:
        if existing_only:
            return 'absent'
        kind = 'new'
    else:
        if last and who and last != who and not overwrite_human:
            return 'human'
        text = keep_quality(text, cur)
        # MediaWiki 存檔時會把結尾換行剝掉，本地檔案帶不帶都不算差異，
        # 否則每次重跑都會把整本書當成「全部改了」重新提交一輪空編輯。
        if cur.rstrip('\n') == text.rstrip('\n'):
            return 'same'
        kind = 'edit'
    if not dry_run:
        r = w.edit(title, text, summary)
        if r.get('result') != 'Success':
            raise SystemExit('編輯失敗 %s: %r' % (title, r))
    return kind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', default='zh.wikisource.org')
    ap.add_argument('--title', help='單頁模式：目標頁標題')
    ap.add_argument('--file', help='單頁模式：來源檔')
    ap.add_argument('--dir', help='批次模式：to_wiki.py 的輸出目錄（VVVV-NNN.wikitext）')
    ap.add_argument('--skip', default='', help='批次模式：跳過的 DjVu 頁碼，如 6,19,21')
    ap.add_argument('--existing-only', action='store_true',
                    help='批次模式：只刷新線上已有的頁，不新建')
    ap.add_argument('--pages', default=None,
                    help='批次模式：只處理這些 DjVu 頁碼，如 456-536 或 7,9,11')
    ap.add_argument('--no-plates', action='store_true',
                    help='批次模式：跳過整頁圖版（沒有 <section> 的檔）')
    ap.add_argument('--overwrite-human', action='store_true',
                    help='批次模式：連最後編輯者不是機器人的頁也覆寫（預設跳過並列出）')
    ap.add_argument('--credentials', required=True)
    ap.add_argument('--account', default=None, help='憑據檔有多個帳號時指定用哪個，如 PacmanD')
    ap.add_argument('--summary', default='',
                    help='編輯摘要；留空則由 MediaWiki 自動填（新建頁是「創建頁面，內容為…」）')
    ap.add_argument('--rate', type=float, default=85,
                    help='批次模式：每分鐘最多幾次編輯。沒有 bot 旗標的帳號伺服器限 90/分，'
                         '預設 85 留點餘量；有 noratelimit 的可以放到 120')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if not (a.dir or (a.title and a.file)):
        ap.error('要嘛 --title + --file，要嘛 --dir')

    w = Wiki(a.site)
    cred = load_credentials(a.credentials, a.account)
    who = w.login(cred['username'], cred['password'])
    del cred
    print('已登入 %s，身分 %s%s' % (a.site, who, '（--dry-run）' if a.dry_run else ''))

    if not a.dir:
        text = open(a.file, encoding='utf-8').read()
        print('來源 %s：%d 位元組，%d 行' % (a.file, len(text.encode()), text.count('\n') + 1))
        kind = put(w, a.title, text, a.summary, a.dry_run)
        print({'same': '內容與線上相同，不提交', 'new': '新建', 'edit': '覆寫'}[kind])
        print('https://%s/wiki/%s' % (a.site, urllib.parse.quote(a.title.replace(' ', '_'))))
        return

    skip = {int(x) for x in a.skip.split(',') if x}
    want = None
    if a.pages:
        want = set()
        for part in a.pages.split(','):
            lo, _, hi = part.partition('-')
            want |= set(range(int(lo), int(hi or lo) + 1))
    # 按 (冊, 頁) 數值排序：字串排序會把 1000–1134 插在 100 和 101 之間
    files = sorted((f for f in os.listdir(a.dir) if f.endswith('.wikitext')),
                   key=lambda f: tuple(int(x) for x in f[:-9].split('-')))
    vols = {int(f.split('-')[0]) for f in files}
    cache = {}
    for v in vols:
        cache.update(w.contents_by_prefix(104, (NS % (v, 0))[len('Page:'):-1]))
    print('線上已有 %d 頁' % len(cache))
    tally = {'same': 0, 'new': 0, 'edit': 0, 'absent': 0, 'skip': 0, 'plate': 0, 'human': 0}
    last_edit = 0.0
    for fn in files:
        vol, dj = (int(x) for x in fn[:-9].split('-'))
        if want is not None and dj not in want:
            continue
        if dj in skip:
            tally['skip'] += 1; continue
        title = NS % (vol, dj)
        text = open(os.path.join(a.dir, fn), encoding='utf-8').read()
        if a.no_plates and '<section' not in text:
            tally['plate'] += 1; continue
        # 節奏按時間戳控制，不是固定 sleep：比對、跳過都不算，只有真寫入才計
        cur, last = cache.get(title, (None, None))
        will_edit = cur is None or (cur.rstrip('\n') != keep_quality(text, cur).rstrip('\n'))
        if will_edit and not a.dry_run:
            wait = last_edit + 60.0 / a.rate - time.time()
            if wait > 0:
                time.sleep(wait)
            last_edit = time.time()
        kind = put(w, title, text, a.summary, a.dry_run, a.existing_only, cache,
                   who, a.overwrite_human)
        tally[kind] += 1
        if kind in ('new', 'edit', 'human'):
            print('%s　%s%s' % ({'new': '新建', 'edit': '覆寫', 'human': '人工改過，不碰'}[kind],
                               title, '（%s）' % cache[title][1] if kind == 'human' else ''),
                  flush=True)
    print('相同 %(same)d　覆寫 %(edit)d　新建 %(new)d　線上無此頁 %(absent)d　跳過 %(skip)d　圖版 %(plate)d　人工改過不碰 %(human)d' % tally)


if __name__ == '__main__':
    main()
