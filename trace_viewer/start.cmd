@echo off
rem Starts the trace viewer. Run from anywhere: .\trace_viewer\start.cmd
setlocal
set "HERE=%~dp0"
rem Never let a benchmark virtualenv leak into the viewer's own environment.
set "VIRTUAL_ENV="
uv run --frozen --project "%HERE:~0,-1%" python "%HERE%main.py" %*
