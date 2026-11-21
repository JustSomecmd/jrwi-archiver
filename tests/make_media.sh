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
# The YouTube version in TubeArchivist: 3 s of something else first, then the same noise, so the same moment is 3 s
# later than in the members-only video
ffmpeg -loglevel error -y -f lavfi -i anoisesrc=color=white:seed=7:duration=3:amplitude=0.3 intro.wav
ffmpeg -loglevel error -y -i intro.wav -i noise.wav -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1" -t 23 yt.wav
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x180:rate=25 -i yt.wav -t 23 -c:v libx264 -preset veryfast \
  -c:a aac -movflags +faststart youtube.mp4
rm noise.wav intro.wav yt.wav
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=400x300 -frames:v 1 img.webp
# A real two-page PDF, so previews can count and draw its pages
python3 - <<'PY'
pages = ["Zombie Survival Guide", "Page two: the rules"]
objs = ["<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages))) + f"] /Count {len(pages)} >>"]
for i, text in enumerate(pages):
    stream = f"BT /F1 28 Tf 72 700 Td ({text}) Tj ET"
    objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                f"/Resources << /Font << /F1 {3 + 2 * len(pages)} 0 R >> >> >>")
    objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
out, offsets = b"%PDF-1.4\n", []
for n, o in enumerate(objs, 1):
    offsets.append(len(out))
    out += f"{n} 0 obj\n{o}\nendobj\n".encode()
xref = len(out)
out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
out += "".join(f"{x:010d} 00000 n \n" for x in offsets).encode()
out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
open("sheet.pdf", "wb").write(out)
PY
echo "test media ready in $(pwd)"
