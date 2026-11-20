#!/bin/sh
# Small test files for the mock site: an HLS video, an audio file, an image and a PDF.
set -e
cd "$(dirname "$0")"
mkdir -p media && cd media
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=640x360:rate=25 -f lavfi -i sine=frequency=440 -t 6 \
  -c:v libx264 -preset veryfast -c:a aac -f hls -hls_time 2 -hls_playlist_type vod \
  -master_pl_name master.m3u8 stream.m3u8
ffmpeg -loglevel error -y -f lavfi -i sine=frequency=330 -t 5 -c:a aac audio.m4a
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=400x300 -frames:v 1 img.webp
printf '%%PDF-1.4\n%% test pdf\n' > sheet.pdf
echo "test media ready in $(pwd)"
