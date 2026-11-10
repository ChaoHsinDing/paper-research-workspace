# 將論文工作區範本上傳 GitHub

匯出套件包含 `paper-reader`、`paper-discovery`、專案設定、程式和測試。原始論文、抽取文字、閱讀卡、筆記、搜尋結果和登入資料留在本機。

## 匯出獨立副本

從目前專案根目錄執行以下指令。匯出程式會建立 `output/packages/paper-research-workspace/` 及同名 ZIP。重新匯出時，程式會保護有額外檔案或內容已變更的目錄。將 ZIP 解壓到新的資料夾後，再在副本初始化 Git。

```zsh
python3 scripts/package_research.py
mkdir -p "$HOME/Projects"
publish_dir="$HOME/Projects/paper-research-github"
mkdir "$publish_dir" && \
  unzip output/packages/paper-research-workspace.zip -d "$publish_dir" && \
  cd "$publish_dir/paper-research-workspace"
```

## 建立私人儲存庫

以下指令需要 Git 2.28 或更新版本及 GitHub CLI。若已經登入 GitHub CLI，可跳過 `gh auth login`。

```zsh
gh auth login
git init -b main
git add -- .gitignore .agents .codex AGENTS.md README.md discovery docs requirements.txt scripts
git diff --cached --name-only
```

確認清單是準備分享的技能、程式、文件及設定。`discovery/interests.json` 含主題與排序偏好，可在這份副本中依分享對象調整後再執行 `git add -- discovery/interests.json`。確認後提交並推送到目前 GitHub 帳號下的新私人儲存庫。

```zsh
git commit -m "Add portable paper research workspace"
gh repo create paper-research-workspace --private --source=. --remote=origin --push
```

若想公開，先確認對外分享範圍並決定程式碼授權，再提交選定的 `LICENSE`。維持論文 PDF、逐頁文字與憑證在 GitHub 之外。

- [GitHub 將本機程式碼加入 GitHub 的說明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)
- [GitHub CLI `gh repo create` 指令手冊](https://cli.github.com/manual/gh_repo_create)

## 在另一台電腦使用

將 `YOUR-ACCOUNT` 換成 GitHub 帳號名，複製儲存庫並進入專案根目錄。若儲存庫是私人的，先以有存取權的帳號登入。

```zsh
gh repo clone YOUR-ACCOUNT/paper-research-workspace
cd paper-research-workspace
```

接著依專案 `README.md` 安裝執行環境、加入自己的 PDF 並在 Codex 開啟根目錄。排程提示詞可跟著套件移植，排程本身在自己的 Codex 帳號中設定。
