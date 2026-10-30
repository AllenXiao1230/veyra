# Camera Monitor

正式目錄已統一為 `/srv/camera-monitor/`；結構、系統入口與相容路徑見 [LAYOUT.md](LAYOUT.md)。來源位於 `source/`，執行程式、設定與資料分開管理。下列歷史版本紀錄中的舊路徑由相容連結保留。

Ubuntu 攝影機監控專案的維護來源，包含 `hybrid1`、`stability1`、`mobile1` 與 `reliability1` 擴充：背景移動偵測、事件快照、時間戳直播、
動態錄影、事件時間軸、健康監控與本機增量備份。網頁透過 nginx 和 Tailscale 提供私有存取。

目前版本為 `features1`，加入站內故障／恢復通知、工作與下載中心、進階事件查找及整合音畫串流，詳見 [FEATURES_20261031.md](FEATURES_20261031.md)。下方具版本名稱的章節保留各版沿革；現役部署清單為 `camera-monitor-features1-manifest.json`。

這份 repository 是既有服務的維護來源，不是從零安裝套件；各版本的部署／驗收結果須以對應交付紀錄為準。完整操作說明與驗收限制見
[README_camera_monitor.md](README_camera_monitor.md)。

## 新功能入口（features1）

- 共用頁首的「通知」顯示六項服務、儲存、備份新鮮度、入口及服務警告的確認異常／恢復；連續兩次檢查異常才通知，設有去重與冷卻。未讀及已讀狀態保存於伺服器；設定視窗可調整通知。只做站內通知，不會即時通知每個工作失敗。
- 頁首「工作與下載」整合 API 備份／驗證／隔離還原工作、事件匯出及原檔下載捷徑，可篩選、分頁及重試；取消僅適用事件匯出。它不包含作業系統排程工作的完整歷史。「已準備好」只代表伺服器可供下載，瀏覽器是否已存檔由使用者確認。
- 「快照 → 事件時間軸 → 進階查找」可組合台北起訖時間、關鍵字、標籤、AI 最低信心、媒體狀態及排序，網址可重開條件；事件預覽內可保存標籤與備註，納入匯出與備份。
- 「設定 → 影像 → 即時串流」可選「音畫整合（低延遲）」或「音畫整合（穩定優先）」；單一 H.264 編碼分送 WebRTC／Opus 與 HLS／AAC，必要時回原影格。手機自動模式仍採低頻寬，播放聲音需點按，背景會卸載播放器。有動作即記錄、AI 僅標註及原有 30 秒停止延遲不變。

現役六項健康 API 為 loopback 8091–8096，經既有私有 Nginx／Tailscale 入口；路由與資料位置見 [LAYOUT.md](LAYOUT.md)。新增套件需求為 Ubuntu `python3-yaml`。HLS 使用本站按需載入的完整 hls.js 1.7.2（MIT，[LICENSE-hls.txt](LICENSE-hls.txt)），不在播放時向 CDN 取程式。Range 續傳、私有檔案 404 與既有正式設定保留規則不變。

本輪 Linux 136 項單元測試及本機 4 組 Node 測試通過；伺服器未安裝 Node。六項服務健康與本機入口檢查不能代替實體 iPhone 的可聽聲音、觸控、長時間記憶體或穩定外網整檔下載驗收，這些仍未驗證。完整證據界線見功能紀錄。

## 錄影與資料可靠性（reliability1）

錄影持續檢查 FFmpeg 輸出時間與檔案增長，停滯時先封裝再退避重試。音訊不可用時改錄純影像並顯示警告；恢復後接續含音訊的新片段，切換可能有短暫間隙。每個片段獨立標示活動／完成／損壞／未知；重啟對帳保留原檔，不宣稱修復損壞影片。

AI 改用有界背景佇列，動作先記錄，時間軸明示「AI 分析中」或「AI 未完成」；可用「AI 未完成」篩選。保存期限仍預設 0；明確設為正數後，`cleanup --apply` 先備份、將到期媒體移到可復原回收桶，保護收藏事件的快照與可關聯錄影，不刪除事件紀錄。

設定 → 備份管理 →「儲存與服務狀態」可看分類占用、檢查時間、必要異常及條件成立時的可錄天數估計。備份與匯出共用空間預留；快照低於 2GiB 保留空間時暫停新增圖片，仍保存事件。匯出壞工作紀錄隔離，背景每分鐘清理到期衍生檔，不需等待下次匯出。

健康維護加入本機 Nginx 內容／Range 驗證與 36 小時內成功備份檢查；狀態變更記錄在伺服器，未設定外部通知。這些不能證明遠端大檔或實體 iPhone 正常。本版最終測試／部署另見交付紀錄；完整來源清單、預檢與人工重建／回滾流程見 [RECOVERY.md](RECOVERY.md)。

## 手機預覽、事件與回收桶（mobile1）

右上角「設定」→「預覽頻寬」提供自動／標準／低頻寬。自動模式在手機、瀏覽器省流量或可辨識的弱網環境選擇低頻寬：最高 640px、約 2fps 的單張 JPEG；標準模式保留最高 960px、8fps MJPEG。錄影仍使用原解析度時間戳管線，預覽設定不改變影片畫質。頁面隱藏時取消前景 GET、回收影格 blob、卸載音訊 iframe 與開啟的播放器；返回後恢復預覽，監聽需重新點按。

快照、錄影與事件清單每頁在 ≤600px 視窗取 12 項，其餘取 24 項，以「上一頁／下一頁」替換本頁內容，不累積無限 DOM。媒體回收桶目前每頁 24 項。日期與時間統一顯示 Asia/Taipei、24 小時制。

在「快照」切換「事件時間軸」（`/snapshots.html?view=events`），可依日期、AI 標註有人／其他動作與收藏篩選，查看快照及已完成的相關錄影。收藏保存在伺服器，可單筆切換或選取本頁批量收藏／匯出。

匯出預設 ZIP，包含事件 JSON／CSV 紀錄、現存快照與 SHA-256 清單；已完成的相關影片須另勾選。亦可只匯出 CSV 或 JSON。每次最多 100 事件、2GiB，伺服器產檔後直接下載，不在手機組裝 ZIP；下載資格 24 小時失效，匯出暫存共用 4GiB 預算。缺失的快照及未完成影片不納入封存，事件紀錄保留。

快照、影片及備份先移到回收桶；只有在回收桶內輸入完整「永久刪除」後才能永久清除，無法復原。媒體支援批量移除／還原／永久刪除及部分成功回報；備份為逐筆操作。錄影中檔案及最近成功備份仍受保護。永久刪除不會連帶刪除先前備份或匯出副本。

## 程式與部署對照

| 來源 | 部署位置／用途 |
| --- | --- |
| `camera_ai.py` | `/opt/camera-ai/camera_ai.py`，背景偵測與事件 API |
| `recorder.py` | `/opt/camera-recorder/server.py`，錄影控制與動態觸發錄影 |
| `timestamp_v2.py` | `/opt/camera-timestamp/server.py`，時間戳直播 |
| `camera_runtime.py`、`camera_catalog.py`、`wait_ready.py` | `/opt/camera-common/` |
| `camera_maintenance.py` | `/usr/local/sbin/camera-maintenance` |
| `camera_audio.py` | `/opt/camera-common/camera_audio.py`，來源選擇、音訊 API 與發布 supervisor |
| `camera_media.py` | `/opt/camera-common/camera_media.py`，完整媒體清單、日期分類、縮圖與回收桶 |
| `camera_timeline.py` | `/opt/camera-common/camera_timeline.py`，事件分頁、收藏與伺服器匯出 |
| `camera_notifications.py`、`camera_work.py` | `/srv/camera-monitor/app/common/`，站內通知與跨 API 工作目錄 |
| `camera_stream.py`、`camera_stream_config.py`、`camera-stream.service` | 整合音畫發布、保留既有 MediaMTX 設定的合併工具及 systemd 服務 |
| `camera_annotations.py`、`camera_sessions.py`、`camera_storage.py`、`camera_diagnostics.py` | 有界 AI 分類、逐檔對帳、空間預留與本機入口檢查 |
| `release_manifest.py`、`preflight.py`、`camera_deployment.py` | 完整來源清單、唯讀主機預檢與部署期間工作閘門 |
| `camera-ui.css`、`camera-ui.js`、`camera-live.js`、`camera-library.js` | 共用介面、設定、監看與媒體互動 |
| `camera-mobile.js`、`camera-timeline.js`、`camera-backups.js` | 單張 JPEG 生命週期、事件時間軸與備份管理 |
| `camera-center.js`、`camera-stream.js`、`hls.min.js`、`LICENSE-hls.txt` | 頁首工具、音畫播放器、本站 HLS 完整 build 與授權 |
| `camera-audio.service` | 使用者 unit，`~/.config/systemd/user/camera-audio.service` |
| `camera-audio-controls.js` | 網頁音訊控制；API 經 `/audio-api/` 代理至 loopback 8094 |
| `live.html`、`snapshots.html`、`recordings.html`、`backups.html` | `/var/www/camera-monitor/`；live.html 部署為 index.html，舊 motion.html 網址轉向快照 |
| `camera-*.service`、`camera-*.timer` | systemd 健康檢查、備份與保存期限排程 |
| `nginx-camera-monitor` | loopback Web 入口與反向代理設定 |
| `camera-monitor-fix1-manifest.json` | 24 個部署檔案的路徑與 SHA-256 |
| `camera-monitor-*-manifest.json` | 各版部署檔案的路徑與 SHA-256；依本次交付紀錄選用對應版本 |
| `camera-monitor-features1-manifest.json` | 目前完整清單；84 個部署檔案，另加 current-release 共核對 85 個檔案 |

檔案保持平面配置，便於核對部署內容與在隔離目錄執行既有測試。Repository 不包含
錄影、快照、事件資料庫、密碼、SSH 連線工具或正式環境的憑證。

## 測試

在具有 Python 3、OpenCV、NumPy、PyYAML（Ubuntu `python3-yaml`）、FFmpeg 及 rsync 的 Ubuntu 主機上執行：

```bash
python3 test_camera_fixes.py
python3 test_camera_audio.py
python3 test_camera_media.py
sudo python3 test_camera_maintenance.py
python3 test_camera_events.py
python3 test_camera_reliability.py
python3 test_camera_features.py
```

完整 Python 回歸使用 `python3 -m unittest discover -v`；Node 可用的本機環境另執行 `test_camera_mobile.cjs`、`test_camera_pages.cjs`、`test_camera_preview.cjs`、`test_camera_stream.cjs`。Node 不是正式服務執行依賴。

維護測試會在 temporary directory 建立測試資料。它在 Ubuntu 上以管理權限驗證
設定封存與增量備份，並模擬服務重啟呼叫，不會重啟正式服務。

下列整合測試需要實際本機攝影機串流、RTSP 音訊與 VAAPI 裝置；測試影片只寫入
獨立的測試目錄：

```bash
sudo python3 verify_pipeline.py
sudo python3 verify_fragment_recovery.py
```

`verify_fragment_recovery.py` 只強制結束自己建立的測試編碼程序。

音訊現場整合測試：先停止錄影並關閉動態觸發錄影，再以 `xuan` 執行
`python3 verify_audio_recovery.py`。它會短暫切換內建輸入、暫停自己的音訊發布程序以模擬停滯、
重啟 MediaMTX／音訊服務，最後恢復原先的來源、音量及靜音設定；完成後再恢復動態觸發錄影。

## 音訊控制（audio1）

首頁「音訊監聽」可選擇伺服器的麥克風輸入、設定 0–100% 音量與靜音，並手動重啟音訊。
「開啟監聽聲音」按鈕可由使用者點按解除播放器預設靜音並播放。
設定保存在執行服務使用者的 `~/.config/camera-monitor/audio.json`，不納入 Git。
初次啟動優先選內建 Digital Microphone，預設 80%；目前部署已驗證此輸入正常。
USB2.0 Device 曾發生取樣停滯，列在選單不代表硬體已修復。

自動恢復檢查 FFmpeg 輸出時間是否持續前進、RTSP 是否真的收到音訊封包、實際 Pulse 路由
是否符合所選裝置。停滯或發布中斷會重建發布程序，以 3–60 秒退避重試；來源消失時保留選擇，
等待該來源回來，不會偷偷切換另一支麥克風。串流正常不代表環境一定有聲音；靜音或音量 0 仍會有封包。
套用來源與手動重啟會在錄影進行中回 HTTP 409，請先停止錄影。

`camera-audio.service` 必須安裝為使用者 unit。它取代原先直接啟動 FFmpeg 的 unit，
舊 `camera-audio.service.d/readiness.conf` 必須備份後移出 drop-in 目錄，避免舊的 ExecStartPost
在來源故障時讓控制 API 無法啟動；`audio-readiness.conf` 保留供歷史 fix1 核對。
MediaMTX 是 Wants 相依性，允許它重啟時音訊 supervisor 繼續執行並重新發布。

API：`GET /audio-api/status`、`GET /audio-api/health`；
`POST /audio-api/source` 接受 `source`、整數 `volume`、布林 `muted`；
`POST /audio-api/restart` 接受空 JSON 物件。寫入 API 驗證同源、請求格式與來源清單，
僅透過現有 nginx/Tailscale 入口存取。reliability1 起音訊失效可錄純影像，介面及 session 明示缺音訊；恢復後接續新片段，不把無音軌標成音訊正常。

## 媒體管理（library1）

首頁只顯示即時影像、錄影控制及精簡的音訊監聽列；右上角「設定」統一管理影像顯示、自動重連、動態觸發錄影及音訊來源／音量／靜音／重啟。
`/snapshots.html` 顯示全部實際快照，按台北日期分類並以游標上一頁／下一頁瀏覽；舊 `/motion.html` 導向此頁。
`/recordings.html` 使用相同導覽、日期篩選及設定視窗，提供實際影片縮圖、回放、下載和刪除。

一般移除會將檔案搬到同磁碟的回收桶，保留原檔內容與復原記錄；mobile1 新增回收桶內的確認式永久刪除：

- 快照：`/var/lib/camera-ai/trash/`，移除時將關聯事件的圖片欄位清空，復原時恢復原關聯，事件本身保留。
- 錄影：`/var/lib/camera-recorder/.trash/`，錄影進行中的檔案拒絕刪除。
- 刪除通知可直接「復原」，或由各頁的「回收桶」復原；回收桶也納入每日備份。
- 影片縮圖快取位於 `/var/lib/camera-recorder/.thumbnails/`，以檔名、大小及修改時間辨識版本，同時最多兩個編碼工作；損壞檔案仍保留下載與刪除功能。

API 使用既有的 `/ai-api/` 及 `/recordings-api/`：`GET /library?limit=12&date=YYYY-MM-DD&before=...`、
`GET /trash?limit=24&before=...`、`POST /trash {"filename":"..."}`、`POST /restore {"token":"..."}`、`POST /purge {"token":"...","confirmation":"永久刪除"}`；批量操作使用 `/batch`，逐項回傳成功與失敗。
快照原圖為 `/ai-api/snapshots/<filename>`；影片縮圖為 `/recordings-api/thumbnail/<filename>`。
寫入驗證同源、JSON 格式及檔名，nginx 會轉送原始 Host。完整媒體清單包含沒有事件關聯的快照，沒有固定的歷史資料上限。

事件使用 `/events-api/list`、`/events-api/favorite`、`/events-api/export` 與 `/events-api/exports/<id>`／`download`。收藏資料庫為 `/var/lib/camera-maintenance/event-preferences.sqlite3`；備份透過 SQLite 線上備份產生一致副本（包含 WAL 尚未 checkpoint 的提交），保存到封存的 `maintenance-state/`。

介面設計依 `PRODUCT.md` 及 Impeccable 的表面 brief；截圖檢查檔位於 Git 排除的 `.impeccable/review/`，不將實際快照或錄影納入版本控制。

## 即時音量與錄影時間戳（meter1）

首頁的音量條在桌面與手機皆顯示，直接量測伺服器所選麥克風的輸入，不必開啟瀏覽器監聽。
數字為 dBFS；−60 dBFS 以下顯示空條，−3 dBFS 以上轉紅。斷線或兩秒未更新會清空，
不把舊值顯示成即時訊號。`GET /audio-api/level` 僅回傳目前量測值，不儲存音訊。
量測使用既有 FFmpeg 發布程序的 astats，不新增麥克風擷取程序；網頁隱藏時停止更新請求。

新錄影改取用 `http://127.0.0.1:8093/stream` 的時間戳影像，左上角的時間直接寫入影片影格。
動作偵測繼續取用 8080 原始串流，避免時鐘變動觸發偵測。時間戳來源中斷時錄影會報錯，
不會降級為無時間戳錄影。原有歷史影片不會被改寫。可用 `sudo python3 verify_meter_timestamp.py`
在隔離目錄產生短片及驗證影格，確認實際 H.264/AAC 輸出。

## 維護

```bash
sudo camera-maintenance check
sudo camera-maintenance backup --dry-run
sudo camera-maintenance cleanup
systemctl list-timers 'camera-*' --no-pager
```

`retention_days: 0` 代表保留全部正式資料。變更保存政策、正式環境設定及資料復原時，
先閱讀完整維護文件並確認備份。`cleanup` 只預覽，`cleanup --apply` 才執行已選政策，且只移回收桶；不會自動永久清除。`rollback-core.sh` 是特定部署的人工回退工具，不是一般安裝程式。
