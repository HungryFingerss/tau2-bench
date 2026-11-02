@echo off
rem Starts a recorded benchmark run. See trace_viewer\README.md.
setlocal
set "HERE=%~dp0"
set "VIRTUAL_ENV="
uv run --frozen --project "%HERE:~0,-1%" python "%HERE%record_run.py" %*
