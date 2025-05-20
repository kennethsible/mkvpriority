#!/bin/bash

CONTAINER_NAME='mkvpriority' # <-- change if you use a different name
FILE_PATH="${sonarr_episodefile_path:-${radarr_moviefile_path}}"

[ -z "$FILE_PATH" ] && exit 0

curl -sS -X POST "http://${CONTAINER_NAME}:8080/process" \
     -H "Content-Type: application/json" \
     -d '{"file_path": "'"$FILE_PATH"'"}'
