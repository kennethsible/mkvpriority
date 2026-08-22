#!/bin/bash

# Configuration:
# - If MKVPriority and Sonarr/Radarr are on the same Docker network, 
#     use the container name for MKVPriority. The webhook port does NOT need to be exposed.
# - If they are not on the same network or you are not using Docker,
#     use the local IP address of your machine and ensure that the webhook port is exposed.
MKVPRIORITY_HOST='mkvpriority'

FILE_PATH="${sonarr_episodefile_path:-${radarr_moviefile_path}}"

[ -z "$FILE_PATH" ] && exit 0

if [ -n "$sonarr_eventtype" ]; then
  curl -sS -X POST "http://${MKVPRIORITY_HOST}:8080/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_tags": "'"$sonarr_series_tags"'",
            "orig_lang": "'"$sonarr_series_originallanguage"'"
            
          }'
elif [ -n "$radarr_eventtype" ]; then
  curl -sS -X POST "http://${MKVPRIORITY_HOST}:8080/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_tags": "'"$radarr_movie_tags"'",
            "orig_lang": "'"$radarr_movie_originallanguage"'"
          }'
fi
