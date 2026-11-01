# 重建、預檢與回滾

目前結構及相容路徑見 [LAYOUT.md](LAYOUT.md)。原始碼位於 `/srv/camera-monitor/source/`；乾淨主機需先按目錄圖建立資料夾與系統註冊連結，再啟動服務。一次性搬移工具用於既有主機，不是乾淨主機安裝器。

本專案目前使用 Ubuntu、systemd、Python/OpenCV、FFmpeg/VAAPI、uStreamer、MediaMTX、PipeWire 與 Tailscale 私有 HTTPS。完整來源清單不包含影片、快照、ONNX 權重、密碼、Tailscale 身分或正式憑證。

## 目前發版清單

在來源根目錄執行 `python3 release_manifest.py --release camera-monitor-pagesize1 --output camera-monitor-pagesize1-manifest.json`，可建立所有現役映射的 SHA-256。`pagesize1` 為目前完整清單；86 個映射檔案，另有 `current-release.json`，共 87 個檔案。重新產生清單不等於已部署，來源或文件更新後必須重新核對；精確 commit、CI 與最終文件／部署核對以本版交付紀錄為準。歷版 manifest 保留作歷史，已退役的音訊 readiness override、HLS light build 與舊授權檔名不重裝。

安裝器只替換不同雜湊的程式／設定、只重啟受影響服務；不再遞迴 chown 錄影。部署期間拒絕新錄影與匯出，等待中的備份／清理鎖會使部署安全拒絕。已有錄影或匯出時須待其完成，不能用部署強行中斷工作。

完成後 `/srv/camera-monitor/app/management/current-release.json` 是本次完整清單；每版程式／設定的回滾位於 `/srv/camera-monitor/backups/releases/`。清單不是備份，也不包含日後變動的使用者設定與媒體。

tuning1 基準程式回滾包為 `/srv/camera-monitor/backups/releases/camera-monitor-tuning1-20261102-002816`。01:23:52 最後修正麥克風 RTP clock 的部署只重啟整合串流；音訊輸入不再使用抵達 wallclock，JPEG wallclock 與正規化起點保留。恢復舊程式可能重新引入已修正的間歇靜音，回滾後應核對實際版本與輸出，不將程式回滾視為音訊驗收。

pagesize1 的「每頁顯示」偏好只存在目前瀏覽器 localStorage，依快照／錄影分開；伺服器備份或隔離還原不會保存或恢復這份瀏覽器偏好。沒有有效保存值時回到自動（載入時 ≤600px 為 12 項，其餘 24 項），也可在頁面重新選擇；儲存不可用時目前頁面仍可使用。

## 乾淨主機恢復流程

1. 先在隔離環境驗證備份 manifest、SHA-256、資料庫 quick_check、設定封存與預期媒體數量；不要直接覆寫運作中的主機。記錄備份建立時間與允許的資料落差。
2. 安裝 Ubuntu 相容套件：Python3、python3-opencv、python3-numpy、python3-yaml（PyYAML）、FFmpeg、rsync、Nginx；依實際硬體恢復 uStreamer、MediaMTX、PipeWire 及 VAAPI 驅動。保留新舊主機的套件版本輸出供比對。帳號、USB 裝置與 render/video 群組須重新核對。Node 僅供前端測試，正式伺服器目前未安裝。
3. 將已驗證的服務設定、來源與媒體恢復到隔離 staging。服務設定 tar 可能含私有資訊，限制檔案權限；外部副本需另外管理加密金鑰。
4. 核對原機 tmpfiles 設定並建立 `/run/camera-monitor/media.lock`；恢復專用服務帳號及資料目錄擁有者。確認模型權重符合 models/person-yolox.json 的固定雜湊。缺少模型時動作仍記錄，AI 顯示未完成。
5. 執行 `sudo python3 preflight.py --stage <staging> --manifest <完整清單>`。它只讀檢查工具、模組、VAAPI 裝置、runtime lock 與來源雜湊，不替使用者登入 Tailscale或建立公開入口。
6. 恢復媒體／事件／收藏資料庫，以及備份 `maintenance-state/` 中的 `event-preferences.sqlite3`（含 `event_details` 標籤／備註）與 `notifications.sqlite3`（通知政策／已讀／事件）。如備份存在 `detector-state/motion-settings.json`，先驗證四欄位與值域，再於停止相關服務的恢復流程中放回 `data/ai/motion-settings.json` 並核對擁有者；它只在使用者自訂時存在，缺少並不代表備份損壞。正式備份以 SQLite backup 取得一致副本；先檢查各資料庫 quick_check、session 對帳、損壞片段及回收桶，再啟動服務。Tailscale 身分／TLS／來源裝置需由管理者按實際環境恢復。
7. 恢復原有 MediaMTX 主機／麥克風設定；使用 `camera_stream_config.py` 的合併流程加入 `live`／`live-hls`，不可用範例直接覆蓋原設定。確認 `camera-stream.service` 及 LAYOUT.md 所列 loopback 8091–8096 六項健康 API、WebRTC／HLS 相對 HTTPS 路由與本站完整 hls.js 1.7.2／MIT 授權檔。HLS 程式需保持按需載入。
8. 逐層驗證影像、純影像／含音訊錄影、時間戳、檔案時長、回放、Range、私有檔案 404、備份與隔離還原。伺服器回環測試不能代替穩定外網整檔下載及實體 iPhone 可聽聲音／觸控／長時間記憶體驗收；這些目前仍未驗證。

這是帶檢查點的重建流程，沒有宣稱未提供硬體、身分或備份位置時可一鍵恢復。實際恢復時間應由隔離演練測量。

features1 功能後備份 `20261031-021419-060215` 已完成深度校驗：1,122 個檔案、13,152,353,834 bytes，通知資料庫與含 `event_details` 的事件偏好資料庫均已收錄，quick_check 為 ok。這是特定時間點的本機副本，後續變動須另行備份。

## tuning1 備份與隔離還原界線

本輪功能後備份 `20261102-010557-441353` 已校驗 2,900 檔、21,478,673,751 bytes，無活動片段略過。它是該時間點的本機副本，早於最後 RTP clock 修正與文件；後續程式、設定、快照及錄影不會自動出現在此副本。較早的 `20261102-002712-482793` 為 20,689,262,125 bytes，略過 1 個活動片段，不能與功能後備份混為同一份完整性證據。

設定封存涵蓋當時實際 app／config／web／bin／vendor；來源 Git 歷史及 `source/` 不是這份執行設定封存的完整替代物，需分別保存。媒體按當次 manifest 收錄；一般備份會明列略過錄製中或無法固定的片段，不保證每次無缺口。後來拍攝／修改的原始資料、Tailscale 身分／憑證及主機套件仍須依重建流程另行準備。release manifest 與 Git 來源都不含正式媒體或私有設定。

有自訂動作政策時，備份將原子保存的 `data/ai/motion-settings.json` 固定到 `detector-state/motion-settings.json`，與其他項目一同校驗。備份頁「隔離還原」先驗證備份，再複製到 `data/maintenance/recoveries/` 的獨立目錄，保留此相對位置；不自動套用到運作中的偵測器。先檢查還原報告與檔案，再按上述人工恢復檢查點處理正式資料。未自訂時沒有此檔，初始政策仍由環境推導（本次 1.085%／18／3／true）。

本輪 Linux 143 項 Python 測試（24.628 秒）及本機 5 組 Node 測試通過，正式音訊 126.59 秒同一發布 session／恢復 0 次、Opus 與 AAC 各 6/6 秒非零且錄影 PID 不變。這些證據不代表精確唇音同步、實體 iPhone 可聽聲音／觸控／長時間記憶體或穩定外網整檔下載已驗證。

## 回滾

使用 staging 中的安裝器副本執行 `sudo python3 deploy_release.py --rollback /srv/camera-monitor/backups/releases/<版本>`。回滾只恢復程式與部署設定，不刪除正式媒體或覆寫使用者設定。新增 SQLite 欄位採加法遷移；舊程式讀取後可能忽略新狀態，降版前應保存資料庫副本並驗證該版本是否仍能開啟。

外部備份位置尚未設定；本機同磁碟副本無法防護整顆磁碟故障。不要把任意其他服務的掛載目錄視為本專案已獲授權的備份目的地。
