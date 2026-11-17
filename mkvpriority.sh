#!/bin/bash

# Configuration:
# - If MKVPriority and Sonarr/Radarr are on the same Docker network,
#     use the container name for MKVPriority (e.g., 'mkvpriority').
# - If running on bare metal on the same machine as Sonarr/Radarr,
#     use '127.0.0.1' (the default loopback bind address).
# - If running on different machines or separate Docker networks,
#     use the host's local IP address (e.g., '192.168.1.100') and
#     set MKVPRIORITY_WEBHOOK_HOST=0.0.0.0 when running on bare metal.
# - If mixing Docker and bare metal, ensure media paths match across both
#     (or rewrite FILE_PATH below before sending the webhook request).
MKVPRIORITY_HOST='mkvpriority'  # or '127.0.0.1' or local IP
MKVPRIORITY_PORT='8080'

FILE_PATH="${sonarr_episodefile_path:-${radarr_moviefile_path}}"

[ -z "$FILE_PATH" ] && exit 0

if [ -n "$sonarr_eventtype" ]; then
  curl -sS -X POST "http://${MKVPRIORITY_HOST}:${MKVPRIORITY_PORT}/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_tags": "'"$sonarr_series_tags"'",
            "orig_lang": "'"$sonarr_series_originallanguage"'"
            
          }'
elif [ -n "$radarr_eventtype" ]; then
  curl -sS -X POST "http://${MKVPRIORITY_HOST}:${MKVPRIORITY_PORT}/process" \
      -H "Content-Type: application/json" \
      -d '{
            "file_path": "'"$FILE_PATH"'",
            "item_tags": "'"$radarr_movie_tags"'",
            "orig_lang": "'"$radarr_movie_originallanguage"'"
          }'
fi
