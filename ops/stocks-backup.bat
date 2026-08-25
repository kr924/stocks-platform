@echo off
setlocal EnableDelayedExpansion
rem ============================================================================
rem  stocks-backup.bat - pull a backup of the Oracle VM down to this machine.
rem
rem  Usage:  stocks-backup.bat [command] [options]
rem  Run     stocks-backup.bat help     for the full list.
rem
rem  The database is copied with sqlite3 .backup rather than scp'd directly.
rem  The app writes to it continuously in WAL mode, and copying the file while
rem  a write is in flight yields a torn database that only fails when you come
rem  to restore it. .backup takes a consistent snapshot of a live database.
rem ============================================================================

rem ---- defaults -------------------------------------------------------------
set "VMHOST=ubuntu@129.159.23.190"
set "SSHKEY=%USERPROFILE%\.ssh\oci_key"
rem Deliberately not under OneDrive: these run to ~50 MB each and syncing every
rem one of them is not what you want.
set "OUTROOT=%USERPROFILE%\stocks-backups"
set "REMOTE=/home/ubuntu/stocks"
set "DBPATH=/home/ubuntu/stocks/backend/market_tracker.db"
set "PRUNEDAYS=30"
set "TARGETFILE="
set "ASSUMEYES=0"
set "CMD="
set "USAGERC=0"

rem ---- argument parsing -----------------------------------------------------
:parse
if "%~1"=="" goto parsed
set "A=%~1"
if /i "!A!"=="-o"       ( set "OUTROOT=%~2" & shift & shift & goto parse )
if /i "!A!"=="--out"    ( set "OUTROOT=%~2" & shift & shift & goto parse )
if /i "!A!"=="-h"       ( set "VMHOST=%~2"  & shift & shift & goto parse )
if /i "!A!"=="--host"   ( set "VMHOST=%~2"  & shift & shift & goto parse )
if /i "!A!"=="-k"       ( set "SSHKEY=%~2"  & shift & shift & goto parse )
if /i "!A!"=="--key"    ( set "SSHKEY=%~2"  & shift & shift & goto parse )
if /i "!A!"=="-d"       ( set "PRUNEDAYS=%~2" & shift & shift & goto parse )
if /i "!A!"=="--days"   ( set "PRUNEDAYS=%~2" & shift & shift & goto parse )
if /i "!A!"=="-f"       ( set "TARGETFILE=%~2" & shift & shift & goto parse )
if /i "!A!"=="--file"   ( set "TARGETFILE=%~2" & shift & shift & goto parse )
if /i "!A!"=="--yes"    ( set "ASSUMEYES=1" & shift & goto parse )
if /i "!A!"=="-y"       ( set "ASSUMEYES=1" & shift & goto parse )
if "!CMD!"=="" ( set "CMD=!A!" & shift & goto parse )
echo [x] Unexpected argument: !A!
set "USAGERC=1"
goto usage
:parsed
if "%CMD%"=="" set "CMD=all"

set "SSH=ssh -i "%SSHKEY%" -o BatchMode=yes -o ConnectTimeout=20"
set "SCP=scp -i "%SSHKEY%" -o BatchMode=yes -o ConnectTimeout=20"

if /i "%CMD%"=="help"    goto usage
if /i "%CMD%"=="-h"      goto usage
if /i "%CMD%"=="--help"  goto usage
if /i "%CMD%"=="/?"      goto usage
if /i "%CMD%"=="list"    goto do_list
if /i "%CMD%"=="prune"   goto do_prune
if /i "%CMD%"=="verify"  goto do_verify

rem Validate the command and its arguments before doing anything remote. An
rem unknown command used to connect to the VM and create a timestamped folder
rem before deciding it had nothing to do, and restore-db without -f did the
rem same. Neither should touch the network to find out it was mistyped.
rem
rem `exit /b` is not used inside these nested blocks: two levels deep in
rem parentheses it prints its message but leaves the exit code at 0, so a
rem caller checking errorlevel sees success. goto to a label always works.
if /i "%CMD%"=="restore-db" (
    if "%TARGETFILE%"=="" goto need_file
    if not exist "%TARGETFILE%" goto no_such_file
)
for %%C in (all db env code config logs restore-db) do if /i "%CMD%"=="%%C" goto cmd_ok
echo [x] Unknown command: %CMD%
set "USAGERC=1"
goto usage
:cmd_ok

rem ---- everything below needs the VM ----------------------------------------
if not exist "%SSHKEY%" (
    echo [x] SSH key not found: %SSHKEY%
    echo     Pass a different one with:  -k ^<path^>
    exit /b 1
)

echo Checking connection to %VMHOST% ...
%SSH% %VMHOST% "echo ok" >nul 2>&1
if errorlevel 1 (
    echo [x] Cannot reach %VMHOST% with key %SSHKEY%
    exit /b 1
)
echo     connected.
echo.

rem ---- timestamped destination ----------------------------------------------
rem %DATE% and %TIME% are locale-dependent and produce different strings on
rem different machines; PowerShell gives one sortable format everywhere.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"') do set "STAMP=%%i"
set "DEST=%OUTROOT%\%STAMP%"
if not exist "%OUTROOT%" mkdir "%OUTROOT%"
if not exist "%DEST%"    mkdir "%DEST%"

if /i "%CMD%"=="all"        goto do_all
if /i "%CMD%"=="db"         goto do_db
if /i "%CMD%"=="env"        goto do_env
if /i "%CMD%"=="code"       goto do_code
if /i "%CMD%"=="config"     goto do_config
if /i "%CMD%"=="logs"       goto do_logs
if /i "%CMD%"=="restore-db" ( rmdir "%DEST%" 2>nul & goto do_restore )

rem ============================================================================
:do_all
call :backup_db      || goto failed
call :backup_env     || goto failed
call :backup_code    || goto failed
call :backup_config  || goto failed
call :backup_logs    || goto failed
goto finished

:do_db
call :backup_db || goto failed
call :write_manifest
goto finished

:do_env
call :backup_env || goto failed
call :write_manifest
goto finished

:do_code
call :backup_code || goto failed
call :write_manifest
goto finished

:do_config
call :backup_config || goto failed
call :write_manifest
goto finished

:do_logs
call :backup_logs || goto failed
call :write_manifest
goto finished

rem ============================================================================
:backup_db
echo [db]     snapshotting the live database ...
rem .backup is safe against a database being written to right now; a plain
rem copy is not. integrity_check runs on the snapshot, not the original, so it
rem costs the running app nothing.
%SSH% %VMHOST% "rm -f /tmp/mt_backup.db* && sqlite3 '%DBPATH%' \".backup '/tmp/mt_backup.db'\" && sqlite3 /tmp/mt_backup.db 'PRAGMA integrity_check;' > /tmp/mt_integrity.txt && gzip -9 -f /tmp/mt_backup.db"
if errorlevel 1 ( echo [x] snapshot failed & exit /b 1 )

rem Read via a temp file, not `for /f`. %SSH% carries a quoted key path, and
rem `for /f` re-parses its command line, which splits on those quotes and hands
rem ssh a bare -o with no argument.
%SSH% %VMHOST% "cat /tmp/mt_integrity.txt" > "%TEMP%\_stocks_ic.txt" 2>nul
set "INTEGRITY="
if exist "%TEMP%\_stocks_ic.txt" set /p INTEGRITY=<"%TEMP%\_stocks_ic.txt"
del /q "%TEMP%\_stocks_ic.txt" >nul 2>&1
echo          integrity_check: !INTEGRITY!
if /i not "!INTEGRITY!"=="ok" (
    echo [x] The snapshot did not pass integrity_check. Keeping it anyway,
    echo     but do not trust it for a restore.
)

echo [db]     downloading ...
%SCP% %VMHOST%:/tmp/mt_backup.db.gz "%DEST%\market_tracker.db.gz" >nul
if errorlevel 1 ( echo [x] download failed & exit /b 1 )
%SSH% %VMHOST% "rm -f /tmp/mt_backup.db.gz /tmp/mt_integrity.txt" >nul 2>&1
call :show_size "%DEST%\market_tracker.db.gz" "market_tracker.db.gz"
echo !INTEGRITY!> "%DEST%\market_tracker.integrity.txt"
exit /b 0

rem ============================================================================
:backup_env
echo [env]    downloading secrets ...
%SCP% %VMHOST%:%REMOTE%/backend/.env "%DEST%\backend.env" >nul
if errorlevel 1 ( echo [x] .env download failed & exit /b 1 )
call :show_size "%DEST%\backend.env" "backend.env"
echo          NOTE: this file holds the Upstox secret and Telegram token in clear text.
exit /b 0

rem ============================================================================
:backup_code
echo [code]   archiving the repository ...
rem .git is included so the archive restores as a working checkout, not a
rem snapshot with no history. The database and .env are excluded because they
rem are captured separately - the db needs a consistent snapshot, not a tar.
%SSH% %VMHOST% "cd /home/ubuntu && tar czf /tmp/stocks_code.tgz --exclude='backend/market_tracker.db*' --exclude='backend/.env' --exclude='*.log' --exclude='backend/.venv' --exclude='frontend/node_modules' stocks"
if errorlevel 1 ( echo [x] archive failed & exit /b 1 )
%SCP% %VMHOST%:/tmp/stocks_code.tgz "%DEST%\stocks_code.tgz" >nul
if errorlevel 1 ( echo [x] download failed & exit /b 1 )
%SSH% %VMHOST% "rm -f /tmp/stocks_code.tgz" >nul 2>&1
call :show_size "%DEST%\stocks_code.tgz" "stocks_code.tgz"
exit /b 0

rem ============================================================================
:backup_config
echo [config] collecting host configuration ...
rem The container is created with flags, not a compose file, so `docker inspect`
rem is the only record of how to recreate it - notably the two bind mounts,
rem without which the app runs against a database inside the image.
%SSH% %VMHOST% "sudo docker inspect stocks-app" > "%DEST%\docker-inspect.json" 2>nul
%SSH% %VMHOST% "sudo cat /etc/systemd/system/cloudflared-quick.service" > "%DEST%\cloudflared-quick.service" 2>nul
%SSH% %VMHOST% "sudo docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'" > "%DEST%\docker-ps.txt" 2>nul
%SSH% %VMHOST% "crontab -l 2>/dev/null; echo '--- systemd ---'; systemctl list-units --type=service --no-pager | grep -iE 'cloudflar|docker'" > "%DEST%\scheduled-services.txt" 2>nul
call :show_size "%DEST%\docker-inspect.json" "docker-inspect.json"
call :show_size "%DEST%\cloudflared-quick.service" "cloudflared-quick.service"
call :write_manifest
exit /b 0

rem ============================================================================
:backup_logs
echo [logs]   collecting container logs ...
%SSH% %VMHOST% "sudo docker logs --tail 5000 stocks-app 2>&1" > "%DEST%\stocks-app.log" 2>nul
call :show_size "%DEST%\stocks-app.log" "stocks-app.log"
exit /b 0

rem ============================================================================
:write_manifest
set "MANIFEST=%DEST%\MANIFEST.txt"
echo Stocks platform backup> "%MANIFEST%"
echo taken   : %STAMP%>> "%MANIFEST%"
echo host    : %VMHOST%>> "%MANIFEST%"
echo command : %CMD%>> "%MANIFEST%"
echo.>> "%MANIFEST%"
rem One round trip, and no `for /f` - see the note in :backup_db.
%SSH% %VMHOST% "cd %REMOTE% && echo commit  : $(git log --oneline -1) && echo sha     : $(git rev-parse HEAD) && echo image   : $(sudo docker images stocks-app:latest --format '{{.ID}} {{.Size}}') && echo db bytes: $(stat -c %%s %DBPATH%)" >> "%MANIFEST%" 2>nul
echo.>> "%MANIFEST%"
echo To restore the database:>> "%MANIFEST%"
echo   stocks-backup.bat restore-db -f "%DEST%\market_tracker.db.gz">> "%MANIFEST%"
exit /b 0

:show_size
rem Report bytes below 1 KB: integer division made an 862-byte .env read "0 KB",
rem which looks exactly like a failed download.
for %%F in (%1) do set "SZ=%%~zF"
if not defined SZ ( echo          %~2  ^(MISSING^) & exit /b 0 )
if !SZ! LSS 1024 (
    echo          %~2  ^(!SZ! bytes^)
) else (
    set /a SZKB=!SZ! / 1024
    echo          %~2  ^(!SZKB! KB^)
)
exit /b 0

rem ============================================================================
:do_restore
echo.
echo  *** This OVERWRITES the live database on %VMHOST% ***
echo      with: %TARGETFILE%
echo      The container is stopped, the current database is copied aside, and
echo      the container is started again.
echo.
if "%ASSUMEYES%"=="0" (
    set /p "CONFIRM=Type RESTORE to continue: "
    if /i not "!CONFIRM!"=="RESTORE" ( echo Aborted. & exit /b 1 )
)
echo Uploading ...
%SCP% "%TARGETFILE%" %VMHOST%:/tmp/restore.db.gz >nul || ( echo [x] upload failed & exit /b 1 )
%SSH% %VMHOST% "set -e; gunzip -f /tmp/restore.db.gz; sudo docker rm -f stocks-app >/dev/null; cp '%DBPATH%' ~/market_tracker.replaced-$(date +%%Y%%m%%d-%%H%%M%%S).db; mv /tmp/restore.db '%DBPATH%'; rm -f '%DBPATH%-wal' '%DBPATH%-shm'; cd ~/stocks && sudo docker run -d --name stocks-app --restart always --network host --env-file ~/stocks/backend/.env -v ~/stocks/backend/market_tracker.db:/app/backend/market_tracker.db -v ~/stocks/backend/.env:/app/backend/.env:ro stocks-app >/dev/null; echo restored"
if errorlevel 1 ( echo [x] restore failed - check the VM & exit /b 1 )
echo Done. The database it replaced is kept on the VM as ~/market_tracker.replaced-*.db
exit /b 0

rem ============================================================================
:do_list
if not exist "%OUTROOT%" ( echo No backups yet under %OUTROOT% & exit /b 0 )
echo Backups under %OUTROOT%:
echo.
powershell -NoProfile -Command ^
  "Get-ChildItem -LiteralPath '%OUTROOT%' -Directory | Sort-Object Name -Descending | ForEach-Object { $s=(Get-ChildItem -LiteralPath $_.FullName -File -Recurse | Measure-Object Length -Sum).Sum; '{0,-22} {1,8:N1} MB  {2}' -f $_.Name, ($s/1MB), ((Get-ChildItem -LiteralPath $_.FullName -File | ForEach-Object Name) -join ', ') }"
exit /b 0

rem ============================================================================
:do_prune
if not exist "%OUTROOT%" ( echo Nothing to prune. & exit /b 0 )
echo Backups older than %PRUNEDAYS% days under %OUTROOT%:
powershell -NoProfile -Command ^
  "$old = Get-ChildItem -LiteralPath '%OUTROOT%' -Directory | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-%PRUNEDAYS%) }; if (-not $old) { '  (none)'; exit }; $old | ForEach-Object { '  ' + $_.Name }; if ('%ASSUMEYES%' -eq '1') { $old | Remove-Item -Recurse -Force; 'Deleted ' + $old.Count + ' backup(s).' } else { ''; 'Re-run with --yes to delete these.' }"
exit /b 0

rem ============================================================================
:do_verify
if "%TARGETFILE%"=="" (
    echo [x] verify needs a file:  stocks-backup.bat verify -f "path\to\market_tracker.db.gz"
    exit /b 1
)
if not exist "%TARGETFILE%" ( echo [x] No such file: %TARGETFILE% & exit /b 1 )
echo Verifying %TARGETFILE% ...
rem Decompresses and checks the SQLite file header, so a truncated download or
rem a half-written snapshot is caught here rather than during a restore.
powershell -NoProfile -Command ^
  "$in=[System.IO.File]::OpenRead('%TARGETFILE%'); $gz=New-Object System.IO.Compression.GzipStream($in,[System.IO.Compression.CompressionMode]::Decompress); $buf=New-Object byte[] 16; $n=$gz.Read($buf,0,16); $hdr=[System.Text.Encoding]::ASCII.GetString($buf,0,15); $total=[int64]$n; $tmp=New-Object byte[] 1048576; while(($r=$gz.Read($tmp,0,$tmp.Length)) -gt 0){$total+=$r}; $gz.Close(); $in.Close(); if($hdr -eq 'SQLite format 3'){ '  header    : SQLite format 3  OK'; '  unpacked  : {0:N1} MB' -f ($total/1MB); exit 0 } else { '  header    : ' + $hdr + '  -- NOT a SQLite database'; exit 1 }"
if errorlevel 1 ( echo [x] Verification FAILED & exit /b 1 )
if exist "%~dp1market_tracker.integrity.txt" (
    set /p ICHK=<"%~dp1market_tracker.integrity.txt"
    echo   integrity : !ICHK! ^(recorded on the VM when the snapshot was taken^)
)
echo   Verified.
exit /b 0

rem ============================================================================
:finished
echo.
echo Backup written to:
echo   %DEST%
echo.
dir /b "%DEST%"
exit /b 0

:failed
echo.
echo [x] Backup did not complete. Partial files are in:
echo     %DEST%
exit /b 1

:need_file
echo [x] restore-db needs the file to push back:
echo     stocks-backup.bat restore-db -f "path\to\market_tracker.db.gz"
exit /b 1

:no_such_file
echo [x] No such file: %TARGETFILE%
exit /b 1

rem ============================================================================
:usage
echo.
echo   stocks-backup.bat [command] [options]
echo.
echo   Commands:
echo     all           db + env + code + config + logs      (default)
echo     db            database only, via a consistent sqlite3 snapshot
echo     env           backend/.env - secrets, stored in clear text
echo     code          repo tarball including .git, excluding db/.env/logs
echo     config        docker inspect, systemd unit, service list, manifest
echo     logs          last 5000 lines of the container log
echo     list          list the backups already on this machine
echo     verify        check a downloaded .db.gz is intact   (needs -f)
echo     prune         delete local backups older than N days
echo     restore-db    push a backup back to the VM          (needs -f)
echo     help          this text
echo.
echo   Options:
echo     -o, --out ^<dir^>    where to write         (default %USERPROFILE%\stocks-backups)
echo     -h, --host ^<user@ip^>                      (default ubuntu@129.159.23.190)
echo     -k, --key ^<path^>   ssh private key        (default %%USERPROFILE%%\.ssh\oci_key)
echo     -d, --days ^<n^>     age cutoff for prune   (default 30)
echo     -f, --file ^<path^>  target for verify / restore-db
echo     -y, --yes          skip confirmation prompts
echo.
echo   Examples:
echo     stocks-backup.bat
echo     stocks-backup.bat db
echo     stocks-backup.bat all -o D:\backups\stocks
echo     stocks-backup.bat list
echo     stocks-backup.bat prune -d 14 --yes
echo     stocks-backup.bat verify -f "%%USERPROFILE%%\stocks-backups\2026-08-25_1700\market_tracker.db.gz"
echo.
exit /b %USAGERC%
