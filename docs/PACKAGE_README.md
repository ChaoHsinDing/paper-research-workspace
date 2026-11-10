# 論文研究工作區

在電腦版 Codex 開啟這個資料夾，就能在同一專案中搜尋新論文及查讀本機 PDF。

| 技能 | 用途 |
| --- | --- |
| `paper-discovery` | 依研究興趣搜尋與篩選論文，記錄書目、來源及取得管道。 |
| `paper-reader` | 索引本機 PDF、查閱完整頁面，以及用頁碼與圖表位置支持導讀及跨篇比較。 |

兩個技能都從專案根目錄尋找設定和程式。解壓或複製後保留 `.agents/`、`.codex/`、`discovery/` 和 `scripts/` 的目錄結構。套件附執行所需檔案，論文庫與逐頁文字索引由各使用者在本機建立。

## 設定工作區

1. 安裝 Python 3.9 或更新版本。在 Codex 開啟此專案根目錄，並依需要信任專案以載入專案模型設定。能使用的模型依帳號及執行環境而定。
2. 將有權存取的 PDF 放到專案內，例如 `papers/`。執行下列指令以安裝抽取工具、建立索引及驗證檔案。首次安裝需連網。渲染圖表需要 Poppler 的 `pdftoppm`，可使用 Codex 的內建版本或自行安裝。

   ```sh
   python3 scripts/bootstrap.py
   python3 scripts/papers.py index
   python3 scripts/papers.py check
   ```

3. 視需要調整 `discovery/interests.json`，執行 `python3 scripts/discover.py check` 驗證設定。開新聊天後可直接輸入以下提示詞。

   - 「用 `$paper-discovery` 找近 30 天與北大西洋海洋熱浪混合層機制相關的論文，附 DOI、出版狀態和已核對的證據。」
   - 「用 `$paper-reader` 導讀本機的〈論文標題〉，每個主要發現附 PDF 頁碼與章節或圖表位置。」

Windows 可用 `py -3` 取代 `python3`。搜尋與索引結果保存在本機，可依需求重新建立。若附有 `discovery/weekly-prompt.txt`，可用它在自己的 Codex 帳號設定每週搜尋。

文獻搜尋需連線到設定中的公開 API 與 RSS。專案設定允許 Codex 在工作區沙盒中連線，實際存取仍受裝置、組織及執行環境的管理政策限制。來源失敗時，搜尋報告會列出受影響的來源。

## 分享或重建套件

從專案根目錄執行 `python3 scripts/package_research.py`。匯出的資料夾與 ZIP 位於 `output/packages/`。要把這份範本推送到自己的儲存庫，請依 [GitHub 上傳步驟](docs/GITHUB.md) 操作。
