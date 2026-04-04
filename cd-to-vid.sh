#!/bin/sh

CHAPTER_FILE="makevideo-chapters.txt"
TRACK_FILE="makevideo-concat.txt"
THUMBNAIL="folder.jpg"

echo ";FFMETADATA1" > "$CHAPTER_FILE"
> "$TRACK_FILE"

START=0

for i in *.flac; do
    echo "file '$i'" >> "$TRACK_FILE"

    DURATION=$(ffprobe -v error -select_streams a:0 \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$i")

    END=$(echo "$START + $DURATION" | bc)

    name="${i%.flac}"
    name="${name#??. }"

    START_MS=$(printf "%.0f" "$(echo "$START * 1000" | bc)")
    END_MS=$(printf "%.0f" "$(echo "$END * 1000" | bc)")

    {
        echo ""
        echo "[CHAPTER]"
        echo "TIMEBASE=1/1000"
        echo "START=$START_MS"
        echo "END=$END_MS"
        echo "title=$name"
    } >> "$CHAPTER_FILE"

    START="$END"
done

FILTER="[1:v]scale=1920:1080,boxblur=20:10[bg]; \
[1:v]scale=700:-1[cover]; \
[bg][cover]overlay=W-w-40:H-h-40"

Y=50
LINE_SPACING=40
INDEX=0
CURRENT_TIME=0

for i in *.flac; do
    DURATION=$(ffprobe -v error -select_streams a:0 \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$i")

    name="${i%.flac}"

    # escape text safely for drawtext
    SAFE_NAME=$(printf "%s" "$name" | sed "s/'/\\\\'/g; s/:/\\\\:/g")

    START_TIME=$CURRENT_TIME
    END_TIME=$(echo "$START_TIME + $DURATION" | bc)

    POS_Y=$(($Y + $INDEX * $LINE_SPACING))

    # Dim text (ONLY when not playing)
    FILTER="$FILTER,drawtext=text='$SAFE_NAME':\
    x=50:y=$POS_Y:\
    fontsize=28:\
    fontcolor=gray:\
    shadowcolor=black:\
    shadowx=1:shadowy=1:\
    enable='not(between(t,$START_TIME,$END_TIME))'"

    # Highlighted text (ONLY when playing)
    FILTER="$FILTER,drawtext=text='$SAFE_NAME':\
    x=50:y=$POS_Y:\
    fontsize=28:\
    fontcolor=white:\
    borderw=1:\
    bordercolor=black:\
    shadowcolor=black:\
    shadowx=3:shadowy=3:\
    enable='between(t,$START_TIME,$END_TIME)'"

    CURRENT_TIME="$END_TIME"
    INDEX=$((INDEX + 1))
done

ffmpeg -y -f concat -safe 0 -i "$TRACK_FILE" \
-loop 1 -i "$THUMBNAIL" \
-filter_complex "$FILTER" \
-map 0:a \
-map 1:v \
-c:v libx264 -crf 23 -preset veryfast \
-tune stillimage \
-c:a flac \
-shortest output_without_chapters.mkv

ffmpeg -y -i output_without_chapters.mkv -i "$CHAPTER_FILE" \
-map_metadata 1 -c copy output.mkv

rm output_without_chapters.mkv
