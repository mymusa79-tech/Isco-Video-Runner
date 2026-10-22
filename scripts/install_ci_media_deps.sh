#!/usr/bin/env bash
# Offline CI test prerequisites. apt mirror unreliability must not be mistaken
# for a media/renderer regression. Keep FFmpeg and Noto requirements intact.
set -euo pipefail

missing=()
command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
command -v ffprobe >/dev/null 2>&1 || { [[ " ${missing[*]} " == *" ffmpeg "* ]] || missing+=(ffmpeg); }
command -v fc-match >/dev/null 2>&1 || missing+=(fontconfig)
dpkg-query -W -f='${Status}' fonts-noto-core 2>/dev/null | grep -q 'install ok installed' || missing+=(fonts-noto-core)

if (( ${#missing[@]} )); then
  # Retry using the public Ubuntu archive if the runner's Azure mirror stalls
  # on large codec packages. Both are standard signed Ubuntu apt repositories.
  apt_opts=(
    -o Acquire::Retries=1
    -o Acquire::http::Timeout=20
    -o Acquire::https::Timeout=20
    -o DPkg::Lock::Timeout=60
  )
  for attempt in 1 2; do
    if (( attempt == 2 )); then
      echo "::warning::Azure apt mirror stalled; retry using Ubuntu public archive"
      for file in /etc/apt/apt-mirrors.txt /etc/apt/sources.list.d/ubuntu.sources; do
        if [[ -f "$file" ]]; then
          sudo sed -i -e 's@http://azure.archive.ubuntu.com/ubuntu@https://archive.ubuntu.com/ubuntu@g' \
                      -e 's@https://azure.archive.ubuntu.com/ubuntu@https://archive.ubuntu.com/ubuntu@g' "$file"
        fi
      done
    fi
    if sudo timeout --signal=TERM --kill-after=15s 120s apt-get "${apt_opts[@]}" update \
       && sudo timeout --signal=TERM --kill-after=15s 240s apt-get "${apt_opts[@]}" install -y --no-install-recommends "${missing[@]}"; then
      break
    fi
    if (( attempt == 2 )); then
      echo "::error::Media dependencies could not be installed from either signed Ubuntu mirror"
      exit 1
    fi
  done
fi

ffmpeg -version >/dev/null
ffprobe -version >/dev/null
fc-match "Noto Sans" >/dev/null
fc-list :family | grep -qi 'Noto'
