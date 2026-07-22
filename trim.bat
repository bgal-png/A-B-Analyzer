@echo off
REM Trim a big sales export for the A/B Sales Analyzer, then upload the small file.
REM Double-click and drag a CSV in, OR drag a CSV file onto this .bat.
setlocal
cd /d "%~dp0"
set "OUTDIR=C:\Users\blank\Desktop\Random codes\AB trims"
if "%~1"=="" (
    set /p "FILE=Drag your export CSV onto this window and press Enter: "
) else (
    set "FILE=%~1"
)
set FILE=%FILE:"=%
echo.
echo Trimming and splitting by project: "%FILE%"
echo.
python "%~dp0trim_export.py" "%FILE%" --split --outdir "%OUTDIR%"
echo.
echo Done. Trimmed files are in:  %OUTDIR%
echo Upload the project file you need to the app.
pause
endlocal
