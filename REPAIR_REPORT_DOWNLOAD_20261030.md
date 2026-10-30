# download1：下載中斷與傳輸修正

日期：2026-10-30（Asia/Taipei）。

## 已確認原因

正式影片下載在送出 200 後約 20 秒再次回報 503；Nginx 有 `upstream prematurely closed connection`。影片 Handler 的 API socket 為 20 秒、事件匯出為 10 秒，原檔案傳輸沿用該期限；慢速接收讓單次寫入超時，例外向外傳遞後又嘗試送錯誤 JSON，造成截斷／污染回應。調高 Nginx upstream 讀取期限本身不能解決後端先結束的問題。

外網對照另有持續停頓：同一時間本機 Nginx／HTTPS 快速完成，遠端 HTTPS 及 SSH 大檔皆停頓。Tailscale 顯示 direct，並有 endpoint timeout 紀錄，故不能直接把外網速度歸因於 DERP，也不能只靠專案程式保證修復整條網路。經一次 Tailscale 重啟仍可重現；網卡 offload 暫時對照未改善，已完整恢復原值，未留下 offload／MTU／防火牆修改。私人 Serve、TLS 與存取範圍保留。

## 修正

- 共用固定 256KiB 分段的檔案傳輸；可用的正式 socket 使用 sendfile，避免 Python 反覆讀入／複製整段影片。
- 檔案寫入獨立使用 300 秒逾時，傳輸結束恢復原 API timeout。Nginx 對客戶端寫入亦為 300 秒；均非整份影片的總下載期限。
- 原檔下載代理採有限記憶體緩衝，不將整支影片另寫代理暫存。
- 影片、快照、匯出共用 Range／ETag／Last-Modified／If-Range；匯出支援 HEAD。版本不同回傳完整內容，避免拼接不同版本。
- 送出標頭後的斷線、Timeout 與 socket 錯誤只結束該回應並記錄，不再寫第二份 JSON 503。
- 保留檔案先開啟再傳輸、符號連結拒絕、匯出效期、原路徑驗證及既有資料保護。

## 驗證

- Linux 89 項回歸通過，包含真實 socket 接收暫停、連線中斷後新連線續傳、檔案版本變更、Range 邊界、匯出 HEAD／Range、原有媒體／備份測試。測試 TMPDIR 使用獨立可寫目錄，避免舊 root 測試殘留的 `/tmp/trash` 權限影響。
- 部署後實際後端回應：限制接收緩衝，接收暫停 25 秒，再繼續取得 8MiB；內容 SHA-256 正確。此暫停超過舊影片 API 的 20 秒期限。
- 透過正式 HTTPS 入口在伺服器本機完整下載 180,609,746 bytes，1.044297 秒完成，SHA-256 與原檔相同。這是伺服器本機回環驗證，不代表遠端下載速度。
- 實際遠端 HTTPS 中斷後，Range＋If-Range 從 6,926,934 位元組處續傳 1MiB，206／Content-Range 正確；合併前綴與續傳片段共 7,975,510 bytes，另與原檔前綴核對雜湊。
- 正式事件 ZIP 的 HEAD 與位元組 10–99 Range 回應正確，與封存檔相同。
- 7 個部署檔案雜湊一致，8091–8095 健康皆正常；動作錄影啟用、停止延遲保留 30 秒。

## 部署與限制

已部署 `camera-monitor-download1-manifest.json`，回滾副本：
`/var/backups/camera-monitor/releases/camera-monitor-download1-20261030-132430`。
既有 `deploy_release.py --rollback <副本目錄>` 可恢復程式與設定，正式媒體與使用者設定不覆寫。

使用者目前無法切換固定 Wi-Fi／有線，已指示先完成伺服器修正。遠端單一連線整檔下載仍有停頓，未列為通過；網路恢復穩定後仍須實測。伺服器已提供續傳能力，但是否自動恢復由瀏覽器／下載工具決定，亦不會繞過匯出 24 小時效期。

來源依據：[Python socket.sendfile](https://docs.python.org/3/library/socket.html#socket.socket.sendfile)、[HTTP If-Range](https://datatracker.ietf.org/doc/html/rfc9110#section-13.1.5)、[Nginx send_timeout](https://nginx.org/en/docs/http/ngx_http_core_module.html#send_timeout)。
