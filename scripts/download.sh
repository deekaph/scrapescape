#!/bin/bash

# File storing URLs to download from
URL_FILE="targets.txt"
# File to store downloaded URLs
PREVIOUSLY_DOWNLOADED_FILE="downloaded_history.txt"
# User agent for wget
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36"
# Maximum number of simultaneous downloads
MAX_DOWNLOADS=4
# Array to hold process IDs of background jobs
PIDS=()

# Function to download the highest quality video from a URL
download_video() {
    local video_url="$1"
    echo -e "Analyzing ${video_url} for embedded videos..."

    # Retrieve the page content
    page_content=$(wget -qO- --user-agent="$USER_AGENT" "$video_url")

    # Extract the title of the page for renaming
    page_title=$(echo "$page_content" | grep -oP '(?<=<title>)(.*)(?=</title>)' | head -n 1)
    [[ -z "$page_title" ]] && page_title="unknown_title"

    # Sanitize the page title to avoid filesystem issues
    page_title=$(echo "$page_title" | tr -cd '[:alnum:]_.-')

    # Extract video URLs (assuming the pattern is `src=` and contains ".mp4" or similar)
    video_link=$(echo "$page_content" | grep -oP '(?<=src=["\x27])[^"\x27]+\.mp4' | head -n 1)

    if [[ -z "$video_link" ]]; then
        echo -e "No downloadable video found for ${video_url}. Skipping..."
        return 1
    fi

    # Determine the filename from the extracted video link
    base_filename=$(basename "$video_link")
    output_file="${base_filename%.*}_$page_title.${base_filename##*.}"

    # Download the video with verbose output, visible progress bar, and resume support
    echo "Downloading video from: $video_link"
    stdbuf -oL wget -c --progress=dot:giga --verbose --user-agent="$USER_AGENT" "$video_link" -O "$output_file"
    
    # Check if the download was successful
    if [[ $? -eq 0 ]]; then
        echo "Download successful: $output_file"
        # Record the URL as downloaded
        echo "$video_url" >> "$PREVIOUSLY_DOWNLOADED_FILE"
        
        # Remove the URL from targets.txt
        sed -i "\|^$video_url$|d" "$URL_FILE"
    else
        echo "Failed to download: $video_url"
        return 1
    fi
}

# Main download process
echo "Starting download process from targets.txt..."
while IFS= read -r url; do
    [[ -z "$url" ]] && continue  # Skip empty lines

    # Start the download in the background
    download_video "$url" &

    # Store the process ID of the background job
    PIDS+=($!)

    # Wait if we reach the maximum number of simultaneous downloads
    if [[ ${#PIDS[@]} -ge $MAX_DOWNLOADS ]]; then
        # Wait for any process to finish
        wait -n
        # Remove finished processes from the array
        PIDS=($(jobs -p))
    fi
done < "$URL_FILE"

# Wait for all background jobs to complete
wait

echo "Download process completed."
