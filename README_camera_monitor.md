# Ubuntu Camera Monitor

> 專案文件版本：2026-10-28 維護修正版（fix1）\
> 文件語言：繁體中文\
> 目前選定架構：**方案 B --- 由 Ubuntu
> 伺服器獨立執行移動偵測；瀏覽器只負責觀看影像與讀取事件。**

## 1. 專案目標

在 Ubuntu 24.04 LTS 主機上建置可長時間執行的攝影機監控服務，讓使用者透過
iPhone Safari，在 Tailscale 私有網路內遠端觀看即時影像並查閱事件紀錄。

主要目標：

-   由伺服器背景服務持續執行移動偵測，不依賴 iPhone 網頁是否開啟。
-   透過瀏覽器觀看即時影像。
-   將事件寫入 SQLite，並在 Web App 顯示事件時間軸。
-   使用 Tailscale 提供遠端私有存取。
-   保持服務可診斷、可備份、可復原。
-   避免瀏覽器與伺服器重複產生正式事件。

## 2. 架構決策

### 2.1 選定方案 B：伺服器端唯一正式偵測來源

``` text
USB Webcam
    │
    ▼
uStreamer ───────────────► Web 直播畫面
    │
    ▼
camera-ai.service
    │
    ├── 伺服器端移動偵測
    ├── 事件判定與冷卻時間
    ├── SQLite 事件資料庫
    └── 事件快照（若有產生）
                 │
                 ▼
        nginx / Web App
                 │
                 ▼
       Tailscale 私有網路
                 │
                 ▼
          iPhone Safari
```

瀏覽器端的責任： - 顯示即時影像。 - 查詢伺服器狀態及事件清單。 -
顯示事件快照與事件資訊。 - 不自行執行移動偵測。 - 不向
`/ai-api/events/browser-motion` 回報移動事件。

伺服器端的責任： - 在背景執行移動偵測。 - 將正式事件寫入資料庫。 -
提供狀態、事件清單與快照 API。 - 不應因 iPhone 關閉網頁或 Safari
暫停而停止偵測。

**重要：** 方案 B
的核心是「正式偵測與事件紀錄在伺服器端」。前端是否已完全移除舊的瀏覽器偵測程式，必須以主機上的實際檔案與驗證結果為準；不能只依架構圖推定已完成。

## 3. 已知主機與執行環境

  -------------------------------------------------------------------------
  項目                                已知資訊
  ----------------------------------- -------------------------------------
  作業系統                            Ubuntu 24.04.5 LTS

  主機名稱                            `sever`（依目前 shell prompt）

  Linux 使用者                        `xuan`

  Web Server                          nginx 1.24.0

  影像串流                            uStreamer

  AI／事件 API                        Python 服務 `camera-ai.service`

  API 監聽位址                        `127.0.0.1:8092`

  Web 入口                            nginx，先前使用 `127.0.0.1:8090`

  遠端存取                            Tailscale Serve，先前使用
                                      `https://server.nase-truck.ts.net`

  事件資料庫                          `/var/lib/camera-ai/events.sqlite3`

  快照目錄                            `/var/lib/camera-ai/snapshots`

  AI 服務程式                         `/opt/camera-ai/camera_ai.py`

  前端目錄                            `/var/www/camera-monitor/`
  -------------------------------------------------------------------------

以上是過往排查時記錄的設定。正式部署前，請以主機目前的實際設定為準；IP、Tailscale
網域、連接埠、服務名稱及路徑若有變更，應同步更新本文件。

## 4. 主要服務與檔案

### 4.1 systemd 服務

-   `camera-ai.service`：伺服器端移動偵測與事件 API。
-   uStreamer 服務：提供攝影機影像串流，實際 unit 名稱需以主機查詢為準。
-   nginx：提供 Web App、反向代理 API 及影像相關路徑。
-   Tailscale Serve：將本機 Web 入口提供給 tailnet 內的裝置。
-   `camera-mediamtx.service`、`camera-audio.service`：先前曾存在於音訊／串流整合工作中；若目前不需要音訊功能，應先確認相依性，不要直接刪除或停用。

### 4.2 AI 後端

-   `/opt/camera-ai/camera_ai.py`
-   `/var/lib/camera-ai/events.sqlite3`
-   `/var/lib/camera-ai/snapshots/`

已知 API：

  ---------------------------------------------------------------------------------
  方法                    路徑                       用途
  ----------------------- -------------------------- ------------------------------
  GET                     `/status`                  取得服務狀態

  GET                     `/events?limit=30`         取得事件清單

  GET                     `/events/<id>/snapshot`    取得事件快照

  POST                    `/events/browser-motion`   已停用，固定回應 HTTP 410；
                                                     不再寫入瀏覽器事件
  ---------------------------------------------------------------------------------

nginx 先前設定 `/ai-api/` 代理到 `127.0.0.1:8092/`。因此公開於 Web App
的 API 路徑例如 `/ai-api/status`，會由 nginx 代理至後端
`/status`。請確認目前 nginx 設定仍符合此規則。

### 4.3 前端

前端曾使用下列檔案：

-   `/var/www/camera-monitor/motion.html`
-   `/var/www/camera-monitor/motion-detection.js`
-   `/var/www/camera-monitor/camera-ai-events.js`
-   `/var/www/camera-monitor/camera-ai-events.css`

`motion-detection.js` 是舊版瀏覽器端移動偵測程式。2026-10-28 修正後，
目前頁面已不再載入它，nginx 對該路徑回應 HTTP 410。原檔保留於主機與備份，
後端也會拒絕舊頁面送出的 browser-motion 請求。直播與伺服器事件 UI 已保留。

`camera-ai-events.js` 應只從伺服器 API 讀取狀態與事件，並依事件是否有
`snapshot_url` 決定顯示圖片或無快照佔位畫面。

## 5. 事件資料模型

已知 `events` 資料表欄位：

-   `id`
-   `created_at`
-   `event_type`
-   `motion_area`
-   `person_detected`
-   `confidence`
-   `snapshot_name`
-   `metadata_json`

事件 API 曾回傳下列欄位：

-   `id`
-   `created_at`
-   `event_type`
-   `motion_area`
-   `person_detected`
-   `confidence`
-   `snapshot_url`
-   `metadata`
-   `source`

事件來源應以伺服器端事件為正式依據。方案 B 不應繼續產生新的
`source: "browser"` 或 `event_type: "browser_motion"`
事件。歷史資料可保留，不必因切換架構而刪除。

### 5.1 快照注意事項

不是每筆事件都一定有快照。當 `snapshot_url` 為空或 `null`
時，前端應顯示文字佔位內容，而不是嘗試載入 `/ai-api` 根路徑或產生錯誤
URL。

若未來要求每次事件都保留影像，需另外設計快照擷取、檔案命名、儲存期限及磁碟空間管理；不能假設目前事件
API 已自動建立快照。

## 6. 安裝與運作原則

本文件不假設主機可從零直接依照此 README
完整安裝，因為目前專案包含既有的服務、反向代理與串流設定。應先盤點實際環境，再修改。

基本原則：

1.  不在未備份的情況下覆寫設定檔。
2.  修改前保存原始檔案及目前服務狀態。
3.  每次只修改一個層面，例如前端、nginx 或後端。
4.  先執行語法檢查，再重啟服務。
5.  驗證成功後才進行下一階段。
6.  不因單一功能異常就重設整個資料庫或刪除快照。
7.  不將管理 API 直接暴露到公網；API 應維持 loopback 監聽並經
    nginx／Tailscale 存取。
8.  不將 Tailscale 認證資訊、API 金鑰或其他秘密寫入 README 或版本控制。

## 7. 日常檢查命令

### 7.1 檢查 AI 服務

``` bash
systemctl status camera-ai.service --no-pager -l
systemctl is-active camera-ai.service
```

### 7.2 檢查服務日誌

``` bash
sudo journalctl -u camera-ai.service -n 100 --no-pager
sudo journalctl -u camera-ai.service -f
```

### 7.3 檢查本機 API

``` bash
curl -fsS http://127.0.0.1:8092/status
echo
curl -fsS 'http://127.0.0.1:8092/events?limit=10'
echo
```

若服務未監聽
`127.0.0.1:8092`，請先查看服務設定，不要直接假設是程式故障。

### 7.4 檢查 nginx

``` bash
sudo nginx -t
systemctl status nginx --no-pager -l
sudo journalctl -u nginx -n 100 --no-pager
```

檢查本機 Web 入口（先前為 8090，若已變更請替換）：

``` bash
curl -sS -o /dev/null -w 'Web HTTP %{http_code}\n' http://127.0.0.1:8090/
curl -sS -o /dev/null -w 'Events API HTTP %{http_code}\n' http://127.0.0.1:8090/ai-api/events?limit=5
```

### 7.5 檢查前端資產是否可存取

``` bash
curl -sS -o /dev/null -w 'motion page HTTP %{http_code}\n' http://127.0.0.1:8090/motion.html
curl -sS -o /dev/null -w 'events JS HTTP %{http_code}\n' http://127.0.0.1:8090/camera-ai-events.js
```

HTTP 200 只代表資源可取得，不代表 JavaScript 功能已正確執行；仍須在
Safari 實際驗證。

## 8. 方案 B 的驗收清單

### 8.1 伺服器端持續偵測

-   [ ] `camera-ai.service` 為 active。
-   [ ] `/status` 顯示服務正在執行。
-   [ ] 攝影機串流連線正常。
-   [ ] `last_error` 為空或沒有持續性錯誤。
-   [ ] 在鏡頭前移動時，伺服器事件清單出現新事件。
-   [ ] 關閉 iPhone Safari 後，伺服器仍能記錄事件。
-   [ ] 重新開啟 Safari 後，可以查閱先前事件。

### 8.2 瀏覽器不再偵測或回報移動

-   [ ] 前端不再載入舊的瀏覽器移動偵測流程。
-   [ ] `motion-detection.js` 不再對 `/ai-api/events/browser-motion`
    發出 POST。
-   [ ] 不再產生新的 `browser_motion` 事件。
-   [ ] 直播畫面與事件時間軸仍可正常使用。
-   [ ] 無快照事件顯示佔位內容，不產生破圖或錯誤請求。

### 8.3 事件資料完整性

-   [ ] 舊事件仍可讀取。
-   [ ] 伺服器端事件的時間、類型與快照 URL 正確。
-   [ ] 重複事件頻率符合預期。
-   [ ] SQLite 資料庫與快照目錄可正常寫入。
-   [ ] 事件 API 不向非授權的公網提供服務。

## 9. 方案切換的安全流程

建議依下列順序完成方案 B：

1.  **備份** 前端、後端與 nginx 設定。
2.  **確認後端** 移動偵測確實啟用，且串流連線正常。
3.  **移除前端偵測呼叫**，但保留直播及事件顯示。
4.  **更新事件 UI**，支援有快照及無快照的事件。
5.  **執行語法與 HTTP 檢查**。
6.  **重新載入或重啟必要服務**，避免不必要地重啟所有元件。
7.  **實測** 關閉 Safari 後的背景偵測。
8.  **觀察日誌與資料庫**，確認沒有持續性錯誤。
9.  驗收完成後再考慮推播、事件保留期限或人物辨識等功能。

### 9.1 備份範例

以下範例會備份前端目錄與後端程式；請確認路徑存在且磁碟空間足夠。

``` bash
BACKUP="$HOME/camera-monitor-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
sudo cp -a /var/www/camera-monitor "$BACKUP/"
sudo cp -a /opt/camera-ai/camera_ai.py "$BACKUP/"
echo "Backup: $BACKUP"
```

若要備份 nginx 設定，可另外執行：

``` bash
sudo cp -a /etc/nginx "$BACKUP/nginx"
```

備份資料可能包含個人化設定，應保留適當的檔案權限。

## 10. 故障排查

### 10.1 服務無法啟動

``` bash
sudo systemctl status camera-ai.service --no-pager -l
sudo journalctl -u camera-ai.service -n 200 --no-pager
```

先檢查錯誤訊息、設定檔路徑、權限及 Python traceback，再決定是否修改。

### 10.2 API 連線失敗

``` bash
sudo ss -ltnp | grep -E '8090|8092'
curl -v http://127.0.0.1:8092/status
```

確認後端監聽位址、nginx upstream 與代理路徑是否一致。

### 10.3 事件沒有新增

依序確認：

1.  攝影機串流是否連線。
2.  `camera-ai.service` 是否 active。
3.  `/status` 中的串流狀態與錯誤欄位。
4.  伺服器端移動偵測設定是否啟用。
5.  偵測閾值、最小變化量與冷卻時間是否過於嚴格。
6.  SQLite 是否可寫入及磁碟空間是否足夠。

不要只因事件清單沒有新增，就立刻重新安裝或刪除資料庫。

### 10.4 iPhone Safari 畫面重載或記憶體不足

過往曾觀察到 Safari 顯示即時畫面後出現記憶體使用警告。若問題仍存在：

-   避免在前端重複解碼或複製大型影像。
-   確認頁面沒有多次建立重複的串流或計時器。
-   限制事件清單的載入筆數及快照圖片數量。
-   檢查是否有不必要的輪詢或未釋放的資源。
-   將長時間偵測留給伺服器，不依賴 Safari 背景執行。

### 10.5 nginx 404／500

``` bash
sudo nginx -t
sudo tail -n 100 /var/log/nginx/error.log
```

檢查 `root`、`alias`、`try_files`、檔案權限與 proxy
路徑。不要直接將整個網站根目錄改成其他位置，除非已確認設定目的。

## 11. 安全與隱私

-   優先讓 API 只監聽 `127.0.0.1`。
-   透過 Tailscale tailnet 存取，不要無必要地將監視器開放至公網。
-   保護快照及事件資料庫的檔案權限。
-   若鏡頭拍攝到他人，應遵守所在地的隱私及告知規範。
-   設計事件保存期限，避免快照無限制累積。
-   README 不應包含 Tailscale 金鑰、密碼、私密 token 或其他憑證。

## 12. 已確認與待驗證事項

### 已確認（根據先前排查紀錄）

-   Ubuntu 24.04.5 LTS。
-   nginx 與 uStreamer 曾正常運作。
-   `camera-ai.service` 曾處於 active 狀態。
-   AI API 曾在 `127.0.0.1:8092` 回應。
-   SQLite 事件資料庫曾成功寫入伺服器端事件。
-   舊版 `/events/browser-motion` 曾成功寫入 `browser_motion` 事件。
-   事件清單曾包含 `source`、`metadata` 與 `snapshot_url` 欄位。

### 待驗證（不能僅依舊紀錄判定完成）

-   伺服器目前的移動偵測設定仍為啟用。
-   前端已完全停止移動偵測及 POST `browser-motion`。
-   Safari 關閉後伺服器仍能持續記錄新事件。
-   nginx 目前的實際 Web 連接埠及 Tailscale Serve 目標。
-   無快照事件在目前前端能正常呈現。
-   事件保存期限、磁碟容量告警及自動清理策略。
-   目前人物偵測功能仍是停用或尚未配置狀態；如需人物辨識，應獨立規劃模型與資源用量。

## 13. 後續建議

優先順序建議：

1.  完成方案 B 切換與關閉 Safari 的背景偵測驗收。
2.  加入事件快照擷取與保存期限管理。
3.  加入服務健康檢查、磁碟空間監控與自動恢復。
4.  改善 Safari 長時間觀看的記憶體與重連行為。
5.  若需求確實需要，再加入人物偵測與推播通知。
6.  加入事件篩選、匯出及備份功能。

## 14. 維護紀錄

  -----------------------------------------------------------------------
  日期                                變更
  ----------------------------------- -----------------------------------
  2026-10-28                          整理目前 Ubuntu
                                      遠端攝影機監控架構、AI API、SQLite
                                      事件紀錄與方案 B 切換要求。

  -----------------------------------------------------------------------

------------------------------------------------------------------------

**文件狀態：** 維護修正版。前述部分清單保留歷史驗收背景；目前部署以第 15 節
的實際修正結果為準。Safari 實機、鏡頭前實際移動與整機重啟驗收仍待完成。

## 15. 2026-10-28 維護修正結果

本節記錄在 `100.93.76.48` 實際部署的 fix1，時區為 UTC+8。

### 15.1 已部署的修正

- AI 偵測增加 supervisor：worker 例外停止後，以 1–30 秒退避自動重新建立。
- AI 與錄影動態分析改用 `/opt/camera-common/camera_runtime.py` 的有界 MJPEG reader。
  它保留最新完整影格並設讀取逾時；已實測 1280×720 解碼正常，原本大量 APP metadata 訊息不再出現。
- `GET /ai-api/health` 檢查執行狀態與影格新鮮度；`/status` 仍保留既有介面。
- 錄影等候 FFmpeg 實際輸出進度後才回報開始成功；異常退出會保留 failed 狀態與錯誤。
- 新錄影改用 fragmented MP4，減少程序非預期結束時整段缺少 moov 的風險。
  仍應以實際斷電驗收確認完整恢復能力。
- 時間戳預覽改用有監督的 JPEG 解碼／加字管線；預設 15 fps、1280×720，
  可用 `CAMERA_PREVIEW_FPS` 在 1–30 fps 內調整。原始 uStreamer 與錄影解析度沒有因此降低。
- `GET /camera/health` 現在回傳 JSON；檢查 worker 存活及影格年齡，過期回 HTTP 503。
- 錄影頁標示不可解析的檔案、停用其回放按鈕，並保留下載原檔；舊 MKV 也會列出供下載。
- 首頁「動態觸發錄影」只控制錄影服務；伺服器事件偵測持續由 camera-ai 執行。
- 舊 `/motion-detection.js` 與 `POST /ai-api/events/browser-motion` 皆回 HTTP 410。
- 錄影寫入 API 增加 JSON 物件、Content-Length、Content-Type 與同源 Origin 驗證。
- 事件輪詢新增逾時、防重疊與背景暫停；保留已顯示事件，避免短暫網路中斷清空時間軸。
- 預覽背景時停止讀流，恢復前景時重新連線；凍結檢查改取樣會變動的時間戳區域。
- MediaMTX 啟動前檢查 Tailscale IP；音訊服務先等候 RTSP listener，再確認音訊 track
  真正可讀才標示 active，取代固定等待 2 秒。
- HTML 頁面加上 no-store 與版本化內部連結；部署後手機請重新整理一次以取得新版。

### 15.2 健康檢查、備份與日誌

| 項目 | 設定 |
| --- | --- |
| 維護程式 | `/usr/local/sbin/camera-maintenance` |
| 狀態檔 | `/var/lib/camera-maintenance/status.json`，root 讀取 |
| 健康檢查 | `camera-health.timer`，每分鐘一次 |
| 自動恢復 | 連續兩次失敗才重啟，單一服務至少間隔 5 分鐘 |
| 錄影 failed 狀態 | 保留供診斷；不會只為清除 failed 而重啟健康的影像分析 worker |
| 容量告警 | 剩餘少於 10 GiB 或使用率超過 85% |
| 完整修正前備份 | `/var/backups/camera-monitor/pre-fix-20261028-2314/` |
| 日常備份 | `/var/backups/camera-monitor/snapshots/`；`latest` 指向最近完成版本 |
| 備份排程 | `camera-backup.timer`，每日 02:30，另有最多 2 分鐘錯開 |
| 備份內容 | 程式、設定、systemd、前端、SQLite 線上備份、快照、已完成錄影 |
| 備份方式 | rsync link-dest；未變更資料只在備份版本之間共用 hard link，不連結到 live 檔案 |
| 備份驗證 | SQLite quick_check、快照引用完整性、設定封存可讀性 |
| 備份保護 | root 私有；少於 20 GiB 可用空間時停止備份並報錯，不會刪除資料騰空間 |
| FFmpeg 日誌 | `/etc/logrotate.d/camera-recorder`；每日／10 MiB，保留 7 份壓縮輪替 |

正式錄影尚未封裝的活動檔案會保守略過，下次備份再收錄。備份保存獨立於 live 資料到期政策；
目前不會自動刪除舊備份，應持續留意容量。

常用維護命令：

```bash
sudo camera-maintenance check
sudo camera-maintenance backup --dry-run
sudo systemctl start camera-backup.service
sudo camera-maintenance cleanup
systemctl list-timers 'camera-*' --no-pager
```

`check --repair` 允許依上述條件重啟服務。`cleanup` 預設只列出計畫；只有 `--apply`
會執行已設定政策，且會先建立完整備份。

### 15.3 保存期限

設定檔為 `/etc/camera-monitor-maintenance.json`。目前 `retention_days: 0`，代表保留全部資料。
每日 03:10 的 cleanup timer 在此設定下只做無刪除的檢查。

只有明確選定保存天數後才啟用到期清理；本次修正沒有刪除任何正式事件、快照或錄影。

### 15.4 已確認的歷史資料問題

以下兩個原始 MP4 缺少 moov 索引，已標示無法回放，原件仍保留：

- `recording-20261027-025353.mp4`
- `recording-20261027-063107.mp4`

不能單憑缺少 moov 就保證可修復內容；本次未宣稱復原這兩段歷史影片。
約 11.81 GB 的 `recording-20261027-015401.mkv` 可解析，現已在錄影頁列出供下載。

### 15.5 已測試與仍待實機驗收

已通過隔離故障注入測試、API 輸入驗證、影格新鮮度檢查，以及實際影像／音訊的
H.264/AAC 錄影、正常封裝與失敗狀態測試。備份測試確認後續版本會共用未變更資料，
live 檔案修改不會改寫歷史備份。前端語法與桌面瀏覽器直播／事件／檔案清單亦已檢查。
隔離測試另強制結束測試編碼程序，產生的 fragmented MP4 仍可解析出影像、音訊與已寫入時長；
這是子程序故障測試，並非整機斷電測試。

仍待驗收：iPhone Safari 長時間記憶體與音訊、關閉 Safari 後實際走入鏡頭觸發事件、
真實斷電／整機重新開機、實際長片逐影格解碼與完整整機還原演練。
