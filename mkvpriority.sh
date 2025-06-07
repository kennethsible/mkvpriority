#!/bin/bash

CONTAINER_NAME='mkvpriority' # <-- change if you use a different name
FILE_PATH="${sonarr_episodefile_path:-${radarr_moviefile_path}}"

[ -z "$FILE_PATH" ] && exit 0

if [ -n "$sonarr_eventtype" ]; then
  curl -sS -X POST "http://${CONTAINER_NAME}:8080/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_type": "series",
            "item_tags": "'"$sonarr_series_tags"'",
            "item_id": "'"$sonarr_series_id"'"
            
          }'
elif [ -n "$radarr_eventtype" ]; then
  curl -sS -X POST "http://${CONTAINER_NAME}:8080/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_type": "movie",
            "item_tags": "'"$radarr_movie_tags"'",
            "item_id": "'"$radarr_movie_id"'"
          }'
fi
