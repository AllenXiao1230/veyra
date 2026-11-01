# Camera Monitor 專案目錄

正式專案根目錄：`/srv/camera-monitor/`。本檔也是根目錄 README 的目標。

```text
/srv/camera-monitor/
├── source/                 Git 來源、測試、發版清單與文件
├── app/
│   ├── ai/                 動作偵測與 ONNX 模型
│   ├── recorder/           錄影控制 API
│   ├── timestamp/          時間戳影像服務
│   ├── common/             共用 Python 模組、音訊與整合串流服務
│   └── management/         備份 API、部署工具、current-release
├── web/                    Nginx 唯一的靜態網站根目錄
├── config/
│   ├── ai/                 正式 AI 設定
│   ├── audio/              使用者音訊選擇（限制存取）
│   ├── mediamtx/           音訊、WebRTC／HLS 串流設定
│   ├── systemd/{system,user}/
│   ├── nginx/
│   ├── logrotate/
│   ├── tmpfiles/
│   └── maintenance.json    保存政策
├── data/
│   ├── ai/                 事件資料庫、快照、回收桶與自訂 motion-settings.json
│   ├── recordings/         影片、session、設定與影片回收桶
│   ├── maintenance/        工作、收藏／備註、站內通知、匯出與隔離還原
│   └── timestamp/
├── backups/                備份快照、回收桶與歷版程式回滾包
├── cache/                  衍生播放快取
├── logs/                   FFmpeg 檔案日誌及輪替檔
├── bin/                    維護指令
├── vendor/mediamtx/        專案使用的 MediaMTX 執行檔
├── archive/                舊部署、測試產物、移轉日誌（root 限制存取）
└── README.md -> source/LAYOUT.md
```

只有 `web/` 經 Nginx 對外提供。設定、原始碼、資料庫、影片與備份不放在網站根目錄；媒體仍透過原有 API 存取。Git 只追蹤 source 內的程式、範本與文件，不包含正式設定、資料、模型或憑證。

## tuning1 服務與入口

Nginx 仍只監聽 `127.0.0.1:8090`，由現有 Tailscale Serve 私有 HTTPS 提供。六項健康 API 都只綁 loopback：

| 連接埠 | 服務 | Nginx 入口 |
| --- | --- | --- |
| 8091 | `camera-recorder-api` 錄影控制／媒體 | `/recordings-api/` |
| 8092 | `camera-ai` 動作偵測／快照 | `/ai-api/` |
| 8093 | `camera-timestamp` 時間戳影像 | `/camera/` |
| 8094 | `camera-audio` 麥克風發布／控制 | `/audio-api/` |
| 8095 | `camera-backup-api` 備份、事件、通知、工作 | `/backups-api/`、`/events-api/` |
| 8096 | `camera-stream` 音畫發布狀態 | `/stream-api/` |

`camera-stream.service` 從時間戳來源取影像、從既有 mic 路徑取聲音，單一 VAAPI H.264 編碼分送 MediaMTX 的 WebRTC／Opus 與 HLS／AAC。WebRTC HTTP 經 `/stream/` 代理至 8889；HLS 經 `/stream-hls/` 代理至回環 8888，重新導向保留相對私有 HTTPS 路徑。部署合併 `live`／`live-hls` 與 HLS 設定，同時保留原有主機及麥克風設定；`mediamtx.yml.example` 不是可直接覆蓋正式設定的副本。

`web/` 新增 `camera-center.js`、`camera-stream.js`、完整 `hls.min.js` 1.7.2 與 MIT 授權 `LICENSE-hls.txt`；HLS 程式只在需要時由本站載入。PyYAML（Ubuntu `python3-yaml`）供設定合併使用。`data/maintenance/notifications.sqlite3` 保存通知政策、事件及已讀狀態；`event-preferences.sqlite3` 的 `event_details` 保存標籤與備註。兩個資料庫以 SQLite backup 納入備份的 `maintenance-state/`。`jobs/`、`event-exports/`、`downloads/` 分別供 API 工作、匯出及原檔捷徑使用，工作中心不列出所有 systemd 排程歷史。

原檔下載仍支援 Range，伺服器準備完成不代表瀏覽器已存檔。私有資料不由靜態根目錄提供，對應直接網址仍回 404。

tuning1 沿用上述六項服務與入口。`app/common/camera_motion.py` 驗證動作政策，`GET/POST /ai-api/motion/settings` 經既有 AI API 提供；只有自訂值改變才原子建立／更新 `data/ai/motion-settings.json`，未自訂時使用既有環境推導的初始值。自訂政策隨備份保存到 `detector-state/motion-settings.json`，隔離還原保留相對位置，不直接寫回正式資料。

`web/camera-connection.js` 只在首頁可見期間每 5 秒探測現有 `/ai-api/status`，保留近 5 分鐘最多 60 筆 HTTP 觀測；不是另一項服務或歷史 SLA。音畫發布保留 JPEG wallclock、麥克風 RTP clock 與正規化起點；`POST /stream-api/restart` 只要求整合發布器重建，不重啟錄影／麥克風。前端仍由原三頁、共用設定與實際媒體網格／清單組成。

pagesize1 僅擴充既有快照／錄影前端工具列，沒有新增服務、API 或伺服器設定；每頁偏好保存在瀏覽器 localStorage，依 `snapshots`／`recordings` 分開，不放在伺服器資料目錄。

現役清單為 `camera-monitor-pagesize1-manifest.json`，86 個映射檔案加 `app/management/current-release.json` 共 87。精確 commit、CI 及最終文件／部署核對見本版交付紀錄。

## 系統入口與相容路徑

systemd 需在 `/etc/systemd/system/` 及使用者的 systemd 設定目錄註冊，Nginx、logrotate、tmpfiles 也需標準系統入口。這些入口是指向本專案 config 的符號連結，實體檔案集中於本目錄。新服務與程式使用 `/srv/camera-monitor` 路徑。

舊 `/opt/camera-*`、`/var/lib/camera-*`、`/var/backups/camera-monitor` 等路徑保留相容連結，並非另一份資料；既有工具或歷史工作路徑仍可使用。家目錄原始碼入口 `/home/xuan/camera-monitor` 指向 `source/`。

`/run/camera-monitor/` 的跨程序鎖、`/run/user/1000/` 的 PipeWire／DBus socket 與暫存執行狀態留在標準 runtime 位置，重開機由 tmpfiles 重建。Ubuntu 套件、uStreamer snap、systemd journal、Tailscale 身分與其他應用程式不搬入專案。

持久快取由搬移保留或部署程式明確建立，tmpfiles 只管理 `/run`，避免共用 `/srv` 為一般使用者擁有時的安全路徑檢查拒絕；不修改共用 `/srv` 的權限。

## 維護與備份

```sh
cd /srv/camera-monitor/source
sudo /srv/camera-monitor/bin/camera-maintenance check
sudo /srv/camera-monitor/bin/camera-maintenance backup
sudo python3 preflight.py --stage . --manifest camera-monitor-pagesize1-manifest.json
```

新備份 configuration.tar.gz 封存實際的 app／config／web／bin／vendor，避免只備份系統入口連結。媒體與資料庫仍由既有一致性備份流程處理；不遞迴備份 backups、archive 或自己。驗證器同時支援搬移前的 `opt/camera-ai/...` 與新 `srv/camera-monitor/app/ai/...` 封存格式，舊備份不改寫。

功能後備份 `20261102-010557-441353` 已校驗 2,900 檔／21,478,673,751 bytes，無活動片段略過；它早於最後音訊 clock 修正與文件。備份並非日後程式／媒體的持續副本；來源 Git、模型／身分與活動片段限制見 [RECOVERY.md](RECOVERY.md)。

同磁碟搬移不會增加異地備份能力。外部備份位置、實機 iPhone 與穩定外網整檔下載仍維持既有待驗收狀態。

## 一次性移轉與回復

`migrate_layout.py` 預設只列出搬移計畫；`--apply` 才會短暫停止服務。它先取得維護／部署鎖，拒絕活動錄影或匯出；同檔案系統 rename 保留媒體與備份 hardlink、權限及 inode。切換失敗時依日誌反向搬移並恢復原本啟用的服務／timer。它不是任意目錄搬移器，不支援跨磁碟自動複製。

日誌與原設定副本保存在 `archive/layout-migration-<時間>/`。其中包含私有設定，不得加入 Git。若斷電或腳本被 SIGKILL，先保持服務停止並按 journal 的反向順序恢復，再檢查 SQLite 與服務；不要直接重跑或刪除新根目錄。正常完成後不應用舊日誌覆蓋日後已修改的設定。舊版程式回滾包的歷史路徑由相容連結保留；回滾舊的系統註冊檔後需檢查連結是否被實體檔取代。
