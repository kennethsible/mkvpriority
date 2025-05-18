#!/bin/bash

if [ ! -z "$sonarr_episodefile_path" ]; then
    ARR_NAME="Sonarr"
    FILE_PATH="$sonarr_episodefile_path"
    MEDIA_ID="$sonarr_series_id"
elif [ ! -z "$radarr_moviefile_path" ]; then
    ARR_NAME="Radarr"
    FILE_PATH="$radarr_moviefile_path"
    MEDIA_ID="$radarr_movie_id"
else
    exit 0
fi

curl -sS -X POST http://mkvpriority:8080/preprocess \
     -H "Content-Type: application/json" \
     -d '{
         "arr_name": "'"$ARR_NAME"'",
         "file_path": "'"$FILE_PATH"'",
         "media_id": "'"$MEDIA_ID"'"
     }'
