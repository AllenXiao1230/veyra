# Camera Monitor

Ubuntu 攝影機監控專案的 `fix1`、`audio1`、`library1` 與 `meter1` 部署來源：背景移動偵測、事件快照、時間戳直播、
動態錄影、健康監控與增量備份。網頁透過 nginx 和 Tailscale 提供私有存取。

這份 repository 對應已部署的維護版本，不是從零安裝套件；完整操作說明與驗收限制見
[README_camera_monitor.md](README_camera_monitor.md)。

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
| `camera-ui.css`、`camera-ui.js`、`camera-live.js`、`camera-library.js` | 三個頁面的共用介面、設定、監看與相簿互動 |
| `camera-audio.service` | 使用者 unit，`~/.config/systemd/user/camera-audio.service` |
| `camera-audio-controls.js` | 網頁音訊控制；API 經 `/audio-api/` 代理至 loopback 8094 |
| `live.html`、`snapshots.html`、`recordings.html` | `/var/www/camera-monitor/`；live.html 部署為 index.html，舊 motion.html 網址轉向快照 |
| `camera-*.service`、`camera-*.timer` | systemd 健康檢查、備份與保存期限排程 |
| `nginx-camera-monitor` | loopback Web 入口與反向代理設定 |
| `camera-monitor-fix1-manifest.json` | 24 個部署檔案的路徑與 SHA-256 |
| `camera-monitor-meter1-manifest.json` | 目前完整部署檔案的路徑與 SHA-256；保留前版 manifest 供比較 |

檔案保持平面配置，便於核對部署內容與在隔離目錄執行既有測試。Repository 不包含
錄影、快照、事件資料庫、密碼、SSH 連線工具或正式環境的憑證。

## 測試

在具有 Python 3、OpenCV、NumPy、FFmpeg 及 rsync 的 Ubuntu 主機上執行：

```bash
python3 test_camera_fixes.py
python3 test_camera_audio.py
python3 test_camera_media.py
sudo python3 test_camera_maintenance.py
```

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
僅透過現有 nginx/Tailscale 入口存取。錄影仍要求有效音訊，沒有加入靜默降級為無聲錄影的行為。

## 媒體管理（library1）

首頁只顯示即時影像、錄影控制及精簡的音訊監聽列；右上角「設定」統一管理影像顯示、自動重連、動態觸發錄影及音訊來源／音量／靜音／重啟。
`/snapshots.html` 顯示全部實際快照，按台北日期分類並以游標分頁載入；舊 `/motion.html` 導向此頁。
`/recordings.html` 使用相同導覽、日期篩選及設定視窗，提供實際影片縮圖、回放、下載和刪除。

刪除會將檔案搬到同磁碟的回收桶，保留原檔內容與復原記錄，不提供永久清除：

- 快照：`/var/lib/camera-ai/trash/`，移除時將關聯事件的圖片欄位清空，復原時恢復原關聯，事件本身保留。
- 錄影：`/var/lib/camera-recorder/.trash/`，錄影進行中的檔案拒絕刪除。
- 刪除通知可直接「復原」，或由各頁的「回收桶」復原；回收桶也納入每日備份。
- 影片縮圖快取位於 `/var/lib/camera-recorder/.thumbnails/`，以檔名、大小及修改時間辨識版本，同時最多兩個編碼工作；損壞檔案仍保留下載與刪除功能。

API 使用既有的 `/ai-api/` 及 `/recordings-api/`：`GET /library?limit=48&date=YYYY-MM-DD&before=...`、
`GET /trash`、`POST /trash {"filename":"..."}`、`POST /restore {"token":"..."}`。
快照原圖為 `/ai-api/snapshots/<filename>`；影片縮圖為 `/recordings-api/thumbnail/<filename>`。
寫入驗證同源、JSON 格式及檔名，nginx 會轉送原始 Host。完整媒體清單包含沒有事件關聯的快照，沒有固定的歷史資料上限。

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
先閱讀完整維護文件並確認備份。`rollback-core.sh` 是特定部署的人工回退工具，不是一般安裝程式。
