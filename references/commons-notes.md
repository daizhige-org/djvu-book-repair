# Commons 上傳與說明頁的慣例

## 帳號與工具

- 憑據檔是 pywikibot 密碼檔格式，`--credentials 路徑 --account 帳號`；不要印出檔案內容。
- `commonsupload.py`：分塊上傳（8 MB），`--overwrite` 放行 exists／duplicate-version／was-deleted，`--file` 指定本機檔，`--repaired-dir` 讀 `.repair.json` 自動附損壞說明，`--dry-run` 先看 sha1 與說明。從 repo 根目錄用絕對路徑執行。
- `wikiupload.py --site commons.wikimedia.org --title … --file …`：整頁覆寫，所以寫前一定先抓最新版當底稿。
- API 讀取要帶 User-Agent，否則 403。

## 檔案說明頁的結構

```
=={{int:filedesc}}==
{{Information
|description={{zh-hant|1=《書名》第N冊，內含：…}}{{zh-hans|1=…}}
{{zh-hant|1='''注意：…'''損壞或補頁的說明}}{{zh-hans|1=…}}
|date=出版年
|source= {{cite book|editor=…|date=…|title=…|publisher=…|isbn=整套的 13 位 ISBN}}
|author={{various}}
…}}

== 目錄 ==
頁碼為冊內頁碼（書眉所印）；本 DjVu 第 K 頁為冊內第 1 頁，DjVu 頁 = 冊內頁碼 + (K−1)。
* 書名　撰者 …起頁

=={{int:license-header}}==
{{PD-scan|PD-old-100-expired}}
[[Category:…]]
```

損壞段的兩種寫法：

- 有佔位頁：「原始電子檔第 A–B 頁（按 DjVu 頁序，共 N 頁）資料損壞或缺失，無法恢復，已在原位置以「頁面損壞」佔位頁代替，其餘頁面的頁序與原檔一致。」
- 已拍照補入：「原始電子檔第 A–B 頁（按 DjVu 頁序，冊內頁碼 a–b，共 N 頁）資料損壞或缺失，無法恢復，已於 日期 全部改以實體書拍照、校正變形並二值化的頁面補入，畫質不及原掃描。全冊頁序與原檔一致。」

部分補入時兩段合寫，分別列出補入範圍與仍為佔位的範圍。

## 頁碼換算的陷阱

「DjVu 頁 = 冊內頁碼 + K」只對前段成立：書中不計頁碼的書名頁（如《孫淵如先生全集》在 DjVu 387）會讓後段多 1。寫換算式前抽查後段一頁，必要時寫兩段式。冊末最後一葉可能只有一面，照片頁數會比壞頁數多 1。

## 使用者的上傳清單頁

`User:PacmanD/Uploads/四庫全書类`（由使用者頁模板引入，改清單要改子頁）有一張修復冊表：冊、總頁數、損壞頁數、損壞頁（DjVu 頁序）、替代品。補頁後把該列的損壞頁數改為 0（或剩餘數）、損壞頁欄寫「（無）」、備註寫補入範圍與日期。使用者常同時手改這頁，整頁覆寫前以最新版為底、錨點替換、錨點找不到就停。

## 上傳後的檢查

1. API `prop=imageinfo&iiprop=sha1|size|timestamp` 看最新版 sha1 與本機相同。
2. `thumb.php?f=檔名&width=300&page=N` 抽看補入的第一頁與最後一頁（`Special:FilePath` 的 page 參數無效）。
3. pagecount 延遲屬正常，不要重傳。
4. 傳錯版本（曾把有一頁全白的版本傳上去）只能再傳一版蓋過，舊版留在歷史裡。
