#!/bin/bash

if [ ! -z "$sonarr_eventtype" ]; then
    ARR_NAME="Sonarr"
    FILE_PATH="$sonarr_episodefile_path"
    ITEM_ID="$sonarr_series_id"
elif [ ! -z "$radarr_eventtype" ]; then
    ARR_NAME="Radarr"
    FILE_PATH="$radarr_moviefile_path"
    ITEM_ID="$radarr_movie_id"
else
    exit 1
fi

curl -sS -X POST http://mkvpriority:8080/preprocess \
     -H "Content-Type: application/json" \
     -d '{
         "arr_name": "'"$ARR_NAME"'",
         "file_path": "'"$FILE_PATH"'",
         "item_id": "'"$ITEM_ID"'"
     }'
