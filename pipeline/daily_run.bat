@echo off
rem DataCenter Atlas daily news run.
rem Probes the local accelerator; uses FETCH_PROXY only when it is up, so
rem direct sources (W.Media, iTnews, Edge, IDCquan, newsrooms) still get
rem fetched on days the accelerator is off. DCD/Nikkei archive sources make
rem multi-day backfills possible once it is back on.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs

set FETCH_PROXY=
curl -s -o nul -m 8 -x http://127.0.0.1:7078 https://www.datacenterdynamics.com/en/rss/
if not errorlevel 1 set FETCH_PROXY=http://127.0.0.1:7078

echo. >> logs\daily.log
echo ===== %DATE% %TIME% ===== >> logs\daily.log
if defined FETCH_PROXY (echo accelerator: ON >> logs\daily.log) else (echo accelerator: OFF >> logs\daily.log)
.venv\Scripts\python.exe run.py >> logs\daily.log 2>&1

rem Commit fresh data and push; the GitHub Actions "Deploy site" workflow
rem rebuilds and publishes the site on every push.
cd /d "%~dp0\.."
git add data/news data/reports data/projects.json >> pipeline\logs\daily.log 2>&1
git diff --cached --quiet >> pipeline\logs\daily.log 2>&1
if errorlevel 1 (
  git commit -m "data: daily digest (local run)" >> pipeline\logs\daily.log 2>&1
  git pull --rebase >> pipeline\logs\daily.log 2>&1 || git rebase --abort >> pipeline\logs\daily.log 2>&1
  rem GitHub connectivity from CN is flaky: try direct first, fall back to
  rem the accelerator when it is up.
  git -c http.proxy= -c https.proxy= push >> pipeline\logs\daily.log 2>&1
  if errorlevel 1 if defined FETCH_PROXY git -c http.proxy=%FETCH_PROXY% -c https.proxy=%FETCH_PROXY% push >> pipeline\logs\daily.log 2>&1
)
