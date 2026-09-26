#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本機的 DjVu／PDF 分塊上傳到 Wikimedia Commons

    python3 tools/commonsupload.py --vols 1500,1586 \\
        --src '/mnt/d/四库全书/续修四库全书' \\
        --desc scratch/xuxiu-desc.json \\
        --credentials ~/wiki-bot-credentials/user-passwd.py --account PacmanD \\
        [--dry-run] [--ignore-warnings was-deleted]

本機檔案按 `DVD*/0NNNN*.djvu` 找；Commons 上的檔名固定為
`續修四庫全書第NNNN冊.djvu`（與 MidleadingBot 2018 年上傳的一致）。
--desc 是 {"1500": "說明文字", ...}，會套進 {{Information}}。

上傳前先查：同名檔是否已存在、同 SHA1 是否已在 Commons（避免重複）。
Commons 對「曾被刪除的檔名」會回 was-deleted 警告，要傳就得用
`--ignore-warnings was-deleted` 明確放行；別的警告一律停下來。
分塊 8 MB，每塊失敗重試；密碼只在登入請求出現。
"""
import argparse, glob, hashlib, json, os, sys, time
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wikiupload import load_credentials  # noqa: E402

API = 'https://commons.wikimedia.org/w/api.php'
UA = ('sikuquanshu-commonsupload/0.1 (https://commons.wikimedia.org/wiki/User:PacmanD) '
      'python-requests')
CHUNK = 8 * 1024 * 1024
TITLE = '續修四庫全書第%d冊.%s'

PAGE = '''=={{int:filedesc}}==
{{Information
|description=%s
|date=2002
|source= {{cite book|editor=續修四庫全書編纂委員會|date=2002 |title=續修四庫全書 |language=zh |publisher=上海古籍出版社 |isbn=9787532524600}}
|author={{various}}
|permission=
|other versions=
}}

=={{int:license-header}}==
{{PD-scan|PD-old-100-expired}}
[[Category:續修四庫全書%s]]
'''

BU = [(1, 260, '經部'), (261, 930, '史部'), (931, 1300, '子部'), (1301, 1800, '集部')]


def bu_of(vol):
    for lo, hi, name in BU:
        if lo <= vol <= hi:
            return name
    raise SystemExit('冊號 %d 超出範圍' % vol)


class Commons:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers['User-Agent'] = UA
        self._csrf = None

    def api(self, data=None, files=None, method='POST', retries=6):
        p = {'format': 'json', 'formatversion': '2', 'maxlag': '5', **(data or {})}
        backoff = 5
        for i in range(retries):
            try:
                if method == 'GET':
                    r = self.s.get(API, params=p, timeout=120)
                else:
                    r = self.s.post(API, data=p, files=files, timeout=600)
                r.raise_for_status()
                d = r.json()
            except (requests.RequestException, ValueError) as e:
                print('  重試 %d/%d（%ds）：%r' % (i + 1, retries, backoff, e), file=sys.stderr)
                time.sleep(backoff); backoff = min(backoff * 2, 120)
                if files:
                    for f in files.values():
                        f[1].seek(0)
                continue
            err = d.get('error')
            if err and err.get('code') in ('maxlag', 'readonly', 'ratelimited', 'internal_api_error',
                                           'stashfailed', 'backend-fail-internal'):
                print('  %s，等 %ds' % (err.get('code'), backoff), file=sys.stderr)
                time.sleep(backoff); backoff = min(backoff * 2, 120)
                if files:
                    for f in files.values():
                        f[1].seek(0)
                continue
            if err:
                raise SystemExit('API 錯誤 %s: %s' % (err.get('code'), err.get('info')))
            return d
        raise SystemExit('重試耗盡')

    def login(self, user, pw):
        tok = self.api({'action': 'query', 'meta': 'tokens', 'type': 'login'}, method='GET')
        tok = tok['query']['tokens']['logintoken']
        j = self.api({'action': 'login', 'lgname': user, 'lgpassword': pw, 'lgtoken': tok})
        if j['login']['result'] != 'Success':
            raise SystemExit('登入失敗：%s' % j['login'].get('reason', j['login']))
        return j['login']['lgusername']

    def csrf(self):
        if not self._csrf:
            self._csrf = self.api({'action': 'query', 'meta': 'tokens'},
                                  method='GET')['query']['tokens']['csrftoken']
        return self._csrf

    def exists(self, title):
        j = self.api({'action': 'query', 'titles': 'File:' + title}, method='GET')
        return not j['query']['pages'][0].get('missing')

    def by_sha1(self, sha1):
        j = self.api({'action': 'query', 'list': 'allimages', 'aisha1': sha1}, method='GET')
        return [x['name'] for x in j['query']['allimages']]

    def upload_chunked(self, path, title, text, comment, ignore):
        size = os.path.getsize(path)
        filekey, offset = None, 0
        with open(path, 'rb') as fh:
            while offset < size:
                fh.seek(offset)
                blob = fh.read(CHUNK)
                import io
                data = {'action': 'upload', 'stash': '1', 'filename': title,
                        'filesize': str(size), 'offset': str(offset), 'token': self.csrf(),
                        'ignorewarnings': '1'}  # 分塊階段的警告（如 exists）留到最後一步才處理
                if filekey:
                    data['filekey'] = filekey
                j = self.api(data, files={'chunk': ('chunk.bin', io.BytesIO(blob),
                                                     'application/octet-stream')})
                up = j['upload']
                if up.get('result') not in ('Continue', 'Success'):
                    raise SystemExit('分塊上傳失敗 offset=%d: %r' % (offset, up))
                filekey = up['filekey']
                offset = up.get('offset', offset + len(blob))
                print('\r  %5.1f%%  %d/%d' % (100.0 * offset / size, offset, size),
                      end='', file=sys.stderr, flush=True)
        print(file=sys.stderr)
        data = {'action': 'upload', 'filekey': filekey, 'filename': title, 'comment': comment,
                'text': text, 'token': self.csrf()}
        j = self.api(data)
        up = j['upload']
        if up.get('result') == 'Warning':
            warns = up.get('warnings', {})
            bad = [k for k in warns if k not in ignore]
            if bad:
                raise SystemExit('上傳 %s 停下：警告 %s（用 --ignore-warnings 放行）' %
                                 (title, json.dumps(warns, ensure_ascii=False)))
            print('  放行警告：%s' % ', '.join(warns), file=sys.stderr)
            data['ignorewarnings'] = '1'
            up = self.api(data)['upload']
        if up.get('result') != 'Success':
            raise SystemExit('上傳 %s 失敗：%r' % (title, up))
        return up


def ranges(pages):
    """[527,528,529,531] → '527–529、531'"""
    out, start, prev = [], None, None
    for p in sorted(pages) + [None]:
        if start is None:
            start = prev = p; continue
        if p is not None and p == prev + 1:
            prev = p; continue
        out.append('%d' % start if start == prev else '%d–%d' % (start, prev))
        start = prev = p
    return '、'.join(out)


def damage_note(repair_json):
    """djvurepair.py 的 .repair.json → 加在 description 後面的損壞說明（繁簡各一）"""
    j = json.load(open(repair_json, encoding='utf-8'))
    bad = [b['page'] for b in j['bad']]
    if not bad:
        return '', ''
    r = ranges(bad)
    hant = ("\n{{zh-hant|1='''注意：本冊掃描檔部分頁面損壞。'''原始電子檔第 %s 頁（按 DjVu 頁序，共 %d 頁）"
            "資料損壞或缺失，無法恢復，已在原位置以「頁面損壞」佔位頁代替，其餘頁面的頁序與原檔一致。}}" % (r, len(bad)))
    hans = ("{{zh-hans|1='''注意：本册扫描档部分页面损坏。'''原始电子档第 %s 页（按 DjVu 页序，共 %d 页）"
            "数据损坏或缺失，无法恢复，已在原位置以“页面损坏”占位页代替，其余页面的页序与原档一致。}}" % (r, len(bad)))
    summary = '第 %s 頁損壞，以佔位頁代替' % r if len(r) < 120 else '共 %d 頁損壞，以佔位頁代替' % len(bad)
    return hant + hans, summary


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def find_local(src, vol):
    hits = sorted(glob.glob(os.path.join(src, 'DVD*', '%05d*.djvu' % vol)) +
                  glob.glob(os.path.join(src, 'DVD*', '%05d*.pdf' % vol)))
    if len(hits) != 1:
        raise SystemExit('冊 %d 本機找到 %d 個檔：%r' % (vol, len(hits), hits))
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vols', required=True, help='冊號，如 1500,1586 或 1613-1620')
    ap.add_argument('--src', default='/mnt/d/四库全书/续修四库全书')
    ap.add_argument('--desc', required=True, help='JSON：{"冊號": "description 內容"}')
    ap.add_argument('--credentials', required=True)
    ap.add_argument('--account', default='PacmanD')
    ap.add_argument('--summary', default='上傳《續修四庫全書》（補 Commons 缺冊）')
    ap.add_argument('--ignore-warnings', default='', help='逗號分隔的警告名，如 was-deleted')
    ap.add_argument('--file', default=None,
                    help='不從 --src 找，直接用這個檔（只能配一個冊號）；配 --overwrite 用來換新版本')
    ap.add_argument('--overwrite', action='store_true',
                    help='線上已有同名檔時上傳為新版本（放行 exists／duplicate-version 警告）')
    ap.add_argument('--repaired-dir', default=None,
                    help='上傳 djvurepair.py 修好的檔：從這個目錄取 NNNN.djvu，'
                         '並按 NNNN.djvu.repair.json 在說明裡列出損壞頁')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    vols = []
    for part in a.vols.split(','):
        lo, _, hi = part.partition('-')
        vols += list(range(int(lo), int(hi or lo) + 1))
    desc = {int(k): v for k, v in json.load(open(a.desc, encoding='utf-8')).items()}
    ignore = {x for x in a.ignore_warnings.split(',') if x}
    if a.overwrite:
        ignore |= {'exists', 'duplicate-version', 'was-deleted'}
    if a.file and len(vols) != 1:
        ap.error('--file 只能配一個冊號')

    c = Commons()
    cred = load_credentials(a.credentials, a.account)
    who = c.login(cred['username'], cred['password'])
    del cred
    print('已登入 Commons，身分 %s%s' % (who, '（--dry-run）' if a.dry_run else ''))

    for vol in vols:
        if vol not in desc:
            raise SystemExit('冊 %d 沒有 description' % vol)
        note, summary = '', a.summary
        if a.repaired_dir:
            path = os.path.join(a.repaired_dir, '%d.djvu' % vol)
            note, dmg = damage_note(path + '.repair.json')
            summary = '%s（%s）' % (a.summary, dmg)
        else:
            path = a.file or find_local(a.src, vol)
        ext = path.rsplit('.', 1)[1].lower()
        title = TITLE % (vol, ext)
        size = os.path.getsize(path)
        print('冊 %d：%s（%.1f MB）→ File:%s' % (vol, os.path.basename(path), size / 1e6, title))
        if c.exists(title) and not a.overwrite:
            print('  線上已有同名檔，跳過'); continue
        sha = sha1_of(path)
        dup = c.by_sha1(sha)
        if dup:
            print('  同內容已在 Commons：%s，跳過' % dup); continue
        text = PAGE % (desc[vol] + note, bu_of(vol))
        if a.dry_run:
            print('  sha1 %s，可上傳。說明：' % sha)
            print('  ' + desc[vol] + note)
            print('  摘要：' + summary); continue
        t0 = time.time()
        up = c.upload_chunked(path, title, text, summary, ignore)
        print('  完成（%.0fs）：https://commons.wikimedia.org/wiki/File:%s' %
              (time.time() - t0, up.get('filename', title).replace(' ', '_')))


if __name__ == '__main__':
    main()
