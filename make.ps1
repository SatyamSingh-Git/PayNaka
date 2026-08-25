# Windows shim, so `.\make <task>` works the way `make <task>` does elsewhere.
# Everything real lives in make.py, which reads the Makefile rather than copying it.
python (Join-Path $PSScriptRoot 'make.py') @args
