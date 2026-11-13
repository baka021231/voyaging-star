#!/bin/sh
# Stands in for ffmpeg when SurfCore grabs frames (qq_chat/bili.py sets $FFMPEG to this file).
# ffmpeg 8 refuses to write JPEG from limited-range video unless told it may; SurfCore does not
# say so, and every frame of such a video failed. Adds `-strict unofficial` before the output.
last=""; n=$#; i=1
for a in "$@"; do if [ $i -eq $n ]; then last=$a; else set -- "$@" "$a"; fi; i=$((i+1)); done
shift $n
exec /usr/bin/ffmpeg "$@" -strict unofficial "$last"
