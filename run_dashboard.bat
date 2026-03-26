@echo off
cd /d C:\intraday
venv\Scripts\python.exe -m streamlit run dashboard/app.py
pause
