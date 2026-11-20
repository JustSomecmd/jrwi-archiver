#!/bin/sh
# Small test files for the mock site: an HLS video, an audio file, an image and a PDF.
set -e
cd "$(dirname "$0")"
mkdir -p media && cd media
# The video's soundtrack and the audio version are the same noise, with the audio starting 2 s into it, so the
# archiver should find that the audio runs 2 s ahead (offset -2.0)
ffmpeg -loglevel error -y -f lavfi -i anoisesrc=color=pink:seed=42:duration=24:amplitude=0.5 noise.wav
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=640x360:rate=25 -i noise.wav -t 20 \
  -c:v libx264 -preset veryfast -c:a aac -f hls -hls_time 2 -hls_playlist_type vod \
  -master_pl_name master.m3u8 stream.m3u8
ffmpeg -loglevel error -y -ss 2 -t 20 -i noise.wav -c:a aac audio.m4a
rm noise.wav
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=400x300 -frames:v 1 img.webp
printf '%%PDF-1.4\n%% test pdf\n' > sheet.pdf
echo "test media ready in $(pwd)"
