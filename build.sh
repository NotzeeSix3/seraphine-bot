#!/bin/bash
pip install --upgrade pip
pip install --only-binary=:all: pynacl==1.5.0
pip install -r requirements.txt

# Download static ffmpeg if not exists
if [ ! -f ./ffmpeg ]; then
  echo "Downloading static ffmpeg..."
  curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o ffmpeg.tar.xz
  tar -xf ffmpeg.tar.xz
  mv ffmpeg-*-static/ffmpeg ./ffmpeg
  mv ffmpeg-*-static/ffprobe ./ffprobe
  rm -rf ffmpeg.tar.xz ffmpeg-*-static
  chmod +x ./ffmpeg ./ffprobe
  echo "FFmpeg installed successfully!"
fi
