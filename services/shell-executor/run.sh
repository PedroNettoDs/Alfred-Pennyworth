#!/bin/bash
export SHELL_EXECUTOR_TOKEN=$(grep SHELL_EXECUTOR_TOKEN /mnt/SSD/alfred/.env | cut -d= -f2)
cd /mnt/SSD/alfred/services/shell-executor
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 7070
