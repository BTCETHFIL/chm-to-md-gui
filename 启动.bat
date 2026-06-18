@echo off
cd /d "%~dp0"
python "%~dp0chm_to_md_gui.py"
if errorlevel 1 (
    echo.
    echo ========================================
    echo  程序异常退出，请检查上方错误信息
    echo ========================================
    pause
)
