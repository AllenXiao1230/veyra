# Camera Monitor

Ubuntu 攝影機監控專案的 `fix1` 部署來源：背景移動偵測、事件快照、時間戳直播、
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
| `live.html`、`motion.html`、`recordings.html`、事件 JS/CSS | `/var/www/camera-monitor/`；live.html 部署為 index.html |
| `camera-*.service`、`camera-*.timer` | systemd 健康檢查、備份與保存期限排程 |
| `nginx-camera-monitor` | loopback Web 入口與反向代理設定 |
| `camera-monitor-fix1-manifest.json` | 24 個部署檔案的路徑與 SHA-256 |

檔案保持平面配置，便於核對部署內容與在隔離目錄執行既有測試。Repository 不包含
錄影、快照、事件資料庫、密碼、SSH 連線工具或正式環境的憑證。

## 測試

在具有 Python 3、OpenCV、NumPy、FFmpeg 及 rsync 的 Ubuntu 主機上執行：

```bash
python3 test_camera_fixes.py
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

## 維護

```bash
sudo camera-maintenance check
sudo camera-maintenance backup --dry-run
sudo camera-maintenance cleanup
systemctl list-timers 'camera-*' --no-pager
```

`retention_days: 0` 代表保留全部正式資料。變更保存政策、正式環境設定及資料復原時，
先閱讀完整維護文件並確認備份。`rollback-core.sh` 是特定部署的人工回退工具，不是一般安裝程式。
