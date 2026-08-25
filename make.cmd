@echo off
REM Windows shim, so `.\make <task>` works the way `make <task>` does elsewhere.
REM PowerShell will not run a command from the current directory without the `.\`,
REM and will tell you so. Everything real lives in make.py, which reads the Makefile.
python "%~dp0make.py" %*
