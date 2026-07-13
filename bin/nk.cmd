@echo off
set "NK_ROOT=%~dp0.."
set "PYTHONPATH=%NK_ROOT%\app;%PYTHONPATH%"
python -m nk %*
