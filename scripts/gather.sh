#!/bin/bash

# File to store the URLs to download
URL_FILE="targets.txt"
# File to store previously downloaded URLs
PREVIOUSLY_DOWNLOADED_FILE="downloaded_history.txt"

# Ensure the necessary files exist
touch "$URL_FILE"
touch "$PREVIOUSLY_DOWNLOADED_FILE"

# Regular expression to check for valid URL format (http or https)
URL_REGEX="^(https?|ftp)://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,3}(/.*)?$"

# Color definitions
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'  # For highlighting the most recent URL
NC='\033[0m'  # No Color

# Function to display the current queue, highlighting the most recent entry
display_queue() {
	echo -e ""
    echo ":.:.:.:.:.::.:.:.:.:.::.:.:.:.:.::.:.:.:.:.:"
    echo -e "${NC}[] Current URLs in the queue:${NC}"
    echo ":.:.:.:.:.::.:.:.:.:.::.:.:.:.:.::.:.:.:.:.:"
    # Read all URLs into an array
    mapfile -t urls < "$URL_FILE"
    
    # Display all URLs, highlighting the most recent
    for i in "${!urls[@]}"; do
        if [[ $i -eq $((${#urls[@]} - 1)) ]]; then
            # Highlight the most recent URL (last entry)
            echo -e "| ${MAGENTA}${urls[$i]}${NC} "
        else
            echo -e "|  ${urls[$i]}  "
        fi
    done
    echo ":.:.:.:.:.::.:.:.:.:.::.:.:.:.:.::.:.:.:.:.:"
}

# Function to display the header banner
display_banner() {
    clear
    echo -e "${CYAN}#############################################"
    echo -e "#           ${NC}ScrapeScape v0.2${CYAN}                #"
    echo -e "#                                           #"
    echo -e "#   ${NC}github.com/deekaph/scrapescape ${CYAN}         #"
    echo -e "#                                           #"
	echo -e "#  ${NC}History Filename: ${GREEN}$PREVIOUSLY_DOWNLOADED_FILE${CYAN} #"
	echo -e "#  ${NC}Queue Filename: ${GREEN}$URL_FILE${NC}              ${CYAN}#"
    echo -e "#############################################"
    echo -e "${NC}"
}

# Function to gather URLs from the user
gather_urls() {
    # Display the banner and initial queue when the script starts
    display_banner
    display_queue

    while true; do
        # Show the instructions before the input prompt
        echo -e "\n${YELLOW}Paste URL to scrape, ${NC}(${GREEN}d${NC})${YELLOW}ownload the queue, or ${NC}(${GREEN}q${NC})${YELLOW}uit${NC}:"
        
        # Updated prompt format [url/d/q] without a newline
        echo -ne "${YELLOW}[${NC}url${YELLOW}/${NC}d${YELLOW}/${NC}q${YELLOW}]:${MAGENTA} "  # Using -n to suppress the newline
        
        read -r url  # User input on the same line
        
        # If the user types 'q', break the loop and exit
        if [[ "$url" == "q" ]]; then
            echo -e ""
			echo -e ""
			echo -e ""
			echo -e "${CYAN}Exiting the script... "
			echo -e ""
			echo -e "${NC}You can manually run ${MAGENTA}download.sh${NC} later without loading this script first.${NC}"
			echo -e ""
            echo -e ""
			break
        fi

        # If the user types 'd', trigger the download
        if [[ "$url" == "d" ]]; then
            echo -e "${CYAN}You can now run the download script (download.sh).${NC}"
            ./download.sh  # Call the download script
            continue
        fi

        # Check if the URL is empty
        if [[ -z "$url" ]]; then
            echo -e "\n${RED}Please enter a valid URL.${NC}"
            continue
        fi

        # Check if the URL matches the regex
        if [[ ! "$url" =~ $URL_REGEX ]]; then
            echo -e "\n${RED}Invalid URL format. You're being stupid! Try again.${NC}"
            continue
        fi

        # Check if the URL has been previously downloaded
        if grep -Fxq "$url" "$PREVIOUSLY_DOWNLOADED_FILE"; then
            echo -e "\n${YELLOW}This URL was previously downloaded. Would you like to download it again?${NC}"
            echo -ne "${YELLOW}[d]ownload anyway / [a]bort: ${NC}"
            read -r choice
            if [[ "$choice" == "d" ]]; then
                echo "$url" >> "$URL_FILE"
                echo -e "\n${GREEN}URL re-added to the queue: $url${NC}"
                sleep 1
                display_banner
                display_queue
            else
                echo -e "${CYAN}Aborted adding URL: $url${NC}"
            fi
        else
            # Add URL to the queue and mark it as downloaded
            echo "$url" >> "$URL_FILE"
            echo "$url" >> "$PREVIOUSLY_DOWNLOADED_FILE"
            echo -e "\n${GREEN}URL added to the queue: $url${NC}"
            
            # Pause for 1 second after showing the message
            sleep 1
            
            # Clear the screen and refresh the banner and queue
            display_banner
            display_queue
        fi
    done
}

# Rinse and repeat!
gather_urls