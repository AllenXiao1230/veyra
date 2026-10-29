# Ubuntu Camera Monitor

> 專案文件版本：2026-10-29 hybrid1（動作觸發、AI 標註、本機備份管理）\
> 文件語言：繁體中文\
> 目前選定架構：**方案 B --- 由 Ubuntu
> 伺服器獨立執行移動偵測；瀏覽器只負責觀看影像與讀取事件。**

## 1. 專案目標

### hybrid1 操作與維護

- 錄影容量上限與備份容量預算集中於設定，預設 0 不限。錄影含回收桶計算，每 5 秒檢查；上限達到後停止新增並保留現有檔案。備份預算使用既有實際占用加完整副本估算（含回收桶、快照及 256 MiB 設定預留），因此可能比增量備份所需空間更保守。
- `.media-index.sqlite3` 為可重建索引，目錄變更立即對帳、持續寫入大小最長約 2 秒更新。影片 `.metadata` 依 stat 保存檢查結果，`.thumbnails` 為縮圖快取；32 件縮圖佇列以 2 個工作者處理並合併同一檔案的要求。快照備份略過可重建索引。

- 即時監看維持音量條與手動錄影。設定 → 錄影可保存動態觸發、動作停止延遲（預設 30 秒）與磁碟保留空間（預設 1024 MB）。手動停止後經完整安靜期才恢復自動觸發；手動開始的錄影由使用者停止。
- 有動作即記錄；AI 僅分類是否有人。人形模型停用或失效不影響動作觸發。快照與錄影共用 8092 的偵測狀態。快照冷卻 15 秒、初始暖機約 30 秒、連續 3 個有效動作影格後觸發。
- 設定 → 備份管理（`/backups.html`）提供建立、驗證、回收桶／復原與隔離還原；工作可關閉頁面後繼續，重新開啟會顯示進度。API 位於 loopback 8095，由 nginx 的 `/backups-api/` 代理。
- 備份根目錄 `/var/backups/camera-monitor/snapshots`；操作與工作紀錄 `/var/lib/camera-maintenance`；隔離還原 `/var/lib/camera-maintenance/recoveries`。隔離還原會複製檔案，不套用設定或覆寫正式資料；設定 tar 僅保留，不自動解開。回收桶仍占空間，沒有永久清除功能。
- 同磁碟備份無法防護磁碟故障。依使用者決定，先完成本機管理，外部目的地稍後設定。建立前需至少 20 GiB 可用；隔離還原需資料量加 1 GiB。
- 備份短暫鎖定媒體變動與資料庫備份以固定一致版本，複製與 SHA-256 校驗於鎖外進行；錄製中的片段會略過並列出。舊備份首次驗證建立雜湊基準，無法證明該基準建立前的完整性。
- 錄影設定 `/var/lib/camera-recorder/settings.json`；每次錄影的觸發來源與事件 ID 位於 `sessions/`。檔名增加 12 位識別碼，舊檔仍支援。
- 模型來源與雜湊見 `models/person-yolox.json`，授權 `models/LICENSE-YOLOX`。權重不放 Git；部署前下載至 staging 的 `models/person-yolox.onnx` 並驗證固定雜湊。推論在本機 CPU，未上傳畫面。
- `camera-recorder-api` 使用專用 `camera-recorder` 帳號及 render/video 群組；`camera-backup-api` 需要 root 讀取服務設定與執行備份，只接受本機連線及固定管理動作。
- 部署：`sudo python3 deploy_release.py --stage <staging> --manifest <staging>/camera-monitor-hybrid1-manifest.json`。腳本驗證所有雜湊、拒絕錄影中部署，保存本版程式／設定、建立共用鎖、啟動服務並檢查健康；失敗自動回滾。
- 手動回滾：`sudo python3 /opt/camera-monitor/deploy_release.py --rollback /var/backups/camera-monitor/releases/<本版備份名稱>`。先停止錄影；只還原程式與部署設定，保留媒體、SQLite 與使用者錄影設定。回滾時請用 staging 的腳本副本，避免腳本本身被移除後無法再次執行。
- 測試：Ubuntu 安裝 `python3-opencv python3-numpy ffmpeg rsync`，執行 `/usr/bin/python3 -m unittest discover -v`；JavaScript 使用 `node --check`。硬體錄影驗證使用 `verify_meter_timestamp.py`，輸出至隔離目錄，不操作正式錄影 API。完整異動見 `CHANGELOG.md`。

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

**文件狀態：** 維護修正版。前述部分清單保留歷史驗收背景；目前部署以第 15、16 節
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

## 16. 2026-10-29 音訊來源選擇與自動復原（audio1）

修復前，音訊 FFmpeg 在發布連線逾時後仍存活，MediaMTX 的 `mic` 不再可讀，造成監聽失敗及
動態錄影的 RTSP 404。現已改用 `/opt/camera-common/camera_audio.py` 管理發布程序，
由 xuan 的 `camera-audio.service` 執行，API 只監聽 `127.0.0.1:8094`。

首頁新增輸入來源選單、0–100% 音量、靜音、套用與重新啟動音訊按鈕。設定保存在
`/home/xuan/.config/camera-monitor/audio.json`。目前選擇已實測可持續擷取的內建
Digital Microphone，音量 80%、未靜音。USB 輸入曾停滯，仍可選，但尚未宣稱其硬體問題已修復。

持續健康檢查包含 FFmpeg 輸出進度、RTSP 實際音訊封包及所選來源的實際路由；發布停滯時自動
重建，失敗以 3–60 秒退避重試。來源離線時保留使用者選擇，等待重新連接。每分鐘維護檢查也已
納入音訊健康；音訊 API 本身停止時可重啟使用者 unit，正常執行中的 supervisor 自行負責串流重連。
手動切換／重啟在錄影中會被拒絕，請先停止錄影。音訊自動恢復仍可能使進行中的錄影中斷或分段，
不保證中斷區間有聲音；沒有將錄影悄悄降級為無聲。

音量表改為分析 WebRTC MediaStream，不再重新導向播放器聲音。音訊恢復或發布程序更換後，
前端會重新連接播放器；「開啟監聽聲音」按鈕由使用者點按解除播放器預設靜音並播放，
iPhone Safari 重連後可能需要再按一次。狀態 API 的短暫網路錯誤不會單獨觸發播放器重載。

部署前備份：`/var/backups/camera-monitor/audio-fix-20261029/`。
其中 configuration.tar.gz 保存原設定；nginx-enabled-before 是實際啟用中的 nginx 設定備份；
舊音訊 readiness drop-in 已移到此目錄，新的使用者 unit 不再套用它。

已通過 20 項測試，以及實際來源切換、手動重啟、SIGSTOP 停滯復原、MediaMTX 重啟復原、
音訊 unit 重啟後設定保存。正式錄影 API 已成功產生含 H.264/AAC 的 MP4，解碼確認音軌有非零訊號；
已清除本次錄影失敗狀態並恢復動態觸發錄影。桌面網頁可見來源選單、音量及自動恢復狀態。
Safari 實機聽感、USB 拔插／硬體復原、整機重啟仍未驗收。

## 17. 2026-10-29 媒體資料庫與統一設定（library1）

首頁只保留即時影像、錄影操作與音訊監聽。導覽列的「快照」收錄全部現存快照，依臺北日期
分組並可篩選日期；「載入更多」逐頁讀取完整歷史，不受事件清單的筆數上限限制。
首次驗證共有 584 張快照、76 個錄影檔。數量會隨背景偵測及使用者操作變動。
舊 `/motion.html` 網址轉向 `/snapshots.html`。

快照和錄影都可單筆或選取多筆移到回收桶。操作後可立即按「復原」，也可在該頁的
「回收桶」還原；本版沒有清空回收桶的網頁操作。回收桶不會自動到期刪除。
快照位於 `/var/lib/camera-ai/trash/`，錄影位於 `/var/lib/camera-recorder/.trash/`，每筆含原檔
及復原收據。移動快照時保留事件，復原時重接原事件；同名原檔已存在時會拒絕覆寫。
正在寫入的錄影禁止移除。兩種回收桶都已加入每日增量備份。

錄影頁以實際影片影格產生縮圖，點選開啟播放器，也提供原檔下載。縮圖工作最多同時兩項，
快取依檔案大小及修改時間失效。歷史損壞影片可能沒有縮圖或無法播放，仍可下載或移到回收桶；
本版未修復第 15.4 節的兩個歷史 MP4。

所有設定由右上角「設定」開啟同一視窗：影像顯示比例、自動重連、動態觸發錄影、音訊來源、
音量、靜音、更新來源及重啟音訊。音訊設定仍由 audio1 supervisor 保存與自動復原。
首頁「開啟監聽」控制本機聆聽；設定內的「靜音」控制伺服器輸入。錄影期間切換或重啟音訊
仍會被拒絕。顯示比例和影像自動重連偏好保存在目前瀏覽器。

新增 `/ai-api/library`、`/ai-api/trash`、`/ai-api/restore`，以及錄影對應的
`/recordings-api/library`、`/recordings-api/trash`、`/recordings-api/restore` 與縮圖 API。
清單使用游標分頁；媒體路徑拒絕目錄穿越及符號連結，寫入操作檢查 Origin。
共同邏輯部署在 `/opt/camera-common/camera_media.py`。

部署前設定及資料庫備份：`/var/backups/camera-monitor/library-ui-20261029/`。
31 項隔離測試通過；另以專用測試檔驗證網頁刪除、立即復原、回收桶還原及原檔雜湊。
正式媒體未用於刪除測試。已檢查桌面、390px 手機及 614px 視窗的版面。
影片縮圖與 HTTP Range 206 已驗證；iPhone Safari 實際播放與長時間監聽仍待實機驗收。

## 18. 2026-10-29 即時音量條與錄影時間戳（meter1）

即時監看頁的音量條在桌面及手機皆顯示，並提供 dBFS 數字。量測取自伺服器已選定來源的
實際 FFmpeg 輸入，不依賴瀏覽器是否開啟監聽；不會要求本機麥克風權限。
−60 dBFS 以下為空條，−3 dBFS 以上轉紅；完全無聲顯示 −∞ dBFS，失聯顯示「暫無訊號」。
資料兩秒未更新、來源不符或發布程序已變更時，舊數值不再視為有效。
網頁約每 150ms 讀取 `/audio-api/level`，隱藏頁面停止請求，錯誤時退避重試。
量測使用 FFmpeg astats/RMS，不另啟擷取程序，也不保存音訊樣本。

新錄影使用時間戳服務 8093 的影像，左上角時間直接寫入 H.264 影格；手動及動態觸發
共用相同錄影管線。背景動作判斷仍使用 8080 原始影像，避免每秒跳動的時間文字誤觸錄影。
MJPEG 解碼器預設的 25fps 時間基準會造成低幀率影像加速，因此在編碼前以實際經過時間
重建影格時間，再輸出 15fps。時間戳串流不可用時明確失敗，不會改錄沒有時間戳的影像。
既有歷史影片不會被重新編碼或補上時間戳。

隔離短片驗證：影像 8.555 秒、音訊 8.522 秒，H.264/AAC 完整解碼成功；第 1 與第 2 秒
影格左上角分別為 12:59:27 和 12:59:28，確認時間戳已寫入且逐秒前進。
部署前備份：`/var/backups/camera-monitor/meter-timestamp-20261029/`。
