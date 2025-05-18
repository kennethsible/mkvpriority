#!/bin/bash

if [ ! -z "$sonarr_eventtype" ]; then
    APP_NAME="Sonarr"
    FILE_PATH="$sonarr_episodefile_path"
    ITEM_ID="$sonarr_series_id"
elif [ ! -z "$radarr_eventtype" ]; then
    APP_NAME="Radarr"
    FILE_PATH="$radarr_moviefile_path"
    ITEM_ID="$radarr_movie_id"
else
    exit 1
fi

curl -X POST http://mkvpriority:8080/preprocess \
     -H "Content-Type: application/json" \
     -d '{
         "app_name": "'"$APP_NAME"'",
         "file_path": "'"$FILE_PATH"'",
         "item_id": "'"$ITEM_ID"'"
     }'
