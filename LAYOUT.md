# Camera Monitor 專案目錄

正式專案根目錄：`/srv/camera-monitor/`。本檔也是根目錄 README 的目標。

```text
/srv/camera-monitor/
├── source/                 Git 來源、測試、發版清單與文件
├── app/
│   ├── ai/                 動作偵測與 ONNX 模型
│   ├── recorder/           錄影控制 API
│   ├── timestamp/          時間戳影像服務
│   ├── common/             共用 Python 模組、音訊服務
│   └── management/         備份 API、部署工具、current-release
├── web/                    Nginx 唯一的靜態網站根目錄
├── config/
│   ├── ai/                 正式 AI 設定
│   ├── audio/              使用者音訊選擇（限制存取）
│   ├── mediamtx/           音訊串流設定
│   ├── systemd/{system,user}/
│   ├── nginx/
│   ├── logrotate/
│   ├── tmpfiles/
│   └── maintenance.json    保存政策
├── data/
│   ├── ai/                 事件資料庫、快照與快照回收桶
│   ├── recordings/         影片、session、設定與影片回收桶
│   ├── maintenance/        工作、收藏、匯出與隔離還原
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
sudo python3 preflight.py --stage . --manifest camera-monitor-layout1-manifest.json
```

新備份 configuration.tar.gz 封存實際的 app／config／web／bin／vendor，避免只備份系統入口連結。媒體與資料庫仍由既有一致性備份流程處理；不遞迴備份 backups、archive 或自己。驗證器同時支援搬移前的 `opt/camera-ai/...` 與新 `srv/camera-monitor/app/ai/...` 封存格式，舊備份不改寫。

同磁碟搬移不會增加異地備份能力。外部備份位置、實機 iPhone 與穩定外網整檔下載仍維持既有待驗收狀態。

## 一次性移轉與回復

`migrate_layout.py` 預設只列出搬移計畫；`--apply` 才會短暫停止服務。它先取得維護／部署鎖，拒絕活動錄影或匯出；同檔案系統 rename 保留媒體與備份 hardlink、權限及 inode。切換失敗時依日誌反向搬移並恢復原本啟用的服務／timer。它不是任意目錄搬移器，不支援跨磁碟自動複製。

日誌與原設定副本保存在 `archive/layout-migration-<時間>/`。其中包含私有設定，不得加入 Git。若斷電或腳本被 SIGKILL，先保持服務停止並按 journal 的反向順序恢復，再檢查 SQLite 與服務；不要直接重跑或刪除新根目錄。正常完成後不應用舊日誌覆蓋日後已修改的設定。舊版程式回滾包的歷史路徑由相容連結保留；回滾舊的系統註冊檔後需檢查連結是否被實體檔取代。
