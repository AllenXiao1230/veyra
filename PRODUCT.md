# Camera Monitor

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

使用者透過 Tailscale 私有網路，以手機 Safari 或桌面瀏覽器操作自己的攝影機監控服務。

## Product Purpose

觀看即時影像、監聽伺服器麥克風、控制錄影，並依日期查閱與管理動態偵測快照及錄影。

## Operating Context

Ubuntu 伺服器獨立執行偵測與錄影。網頁關閉後背景服務仍持續執行。
日期與時間以 Asia/Taipei 呈現。既有入口為 nginx 與 Tailscale Serve，不新增公開入口。

## Capabilities and Constraints

- 首頁專注即時監看及必要控制，不顯示偵測快照。
- 快照頁整合全部快照並依日期分類；可分批載入，但不可永久隱藏較舊資料。
- 錄影頁顯示實際影片縮圖、播放／下載與刪除操作。
- 使用者已明確選擇刪除後移至回收桶，可復原；本次不提供永久清除。
- 使用中的錄影不可刪除。保留現有歷史事件與既有損壞檔案的可辨識狀態。
- 所有持續性設定選項集中在按鈕開啟的設定視窗；內容的日期篩選、播放與刪除是頁面操作。
- 音訊有來源選擇、輸入音量、靜音、手動重啟及自動恢復；瀏覽器播放需使用者手勢。
- 以現有 Python、靜態 HTML/CSS/JavaScript 與 systemd 架構擴充。

## Brand Commitments

Camera Monitor；繁體中文。延續現有深色工具介面，不導入行銷文案或新的品牌形象。

## Evidence on Hand

既有實際攝影機串流、音訊、SQLite 事件、伺服器快照與錄影。縮圖來自實際影片，沒有示意圖或捏造資料。

## Product Principles

1. 監看優先，設定退到需要時才出現的視窗。
2. 全部資料可找到，日期、數量、載入狀態應清楚。
3. 刪除可復原，進行中的工作受到保護。
4. 只保留影響當下操作的提示與警告。
