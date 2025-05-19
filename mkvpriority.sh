#!/bin/bash

FILE_PATH="${sonarr_episodefile_path:-${radarr_moviefile_path}}" || exit 0

curl -sS -X POST http://mkvpriority:8080/process \
     -H "Content-Type: application/json" \
     -d '{"file_path": "'"$FILE_PATH"'"}'
