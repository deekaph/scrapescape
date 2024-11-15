# ScrapeScape v0.2
For backing up videos embedded in web pages.

dependencies:

wget

curl


In short, I wanted to automate the process of viewing a source, finding the video and then downloading it.

Lightweight and simple, it runs in bash and uses common Linux utils.

*gather.sh* : copy the full URL from the address bar of a page that has a video embedded in it and paste it into the input field presented by this script. It will check to make sure it's a valid URL, whether you've ever downloaded it before, whether it's in the current queue, and append it to the bottom. 

*download.sh* : reads the file created by *gather.sh* and downloads the videos it finds embedded there.

After experimenting with a few different strategies, what I've found works best for me is to have a split screen terminal open, with the top running *gather.sh* and the bottom running *download.sh*

Maybe later I'll tweak it, for now I just drop the scripts into a directory, chmod +x the pair of them, and start loading up the gather script with URLs. Once it's got a pile of them, I start the download script.

For now I've got it running 4 concurrent downloads. Much more and I think it throws red flags server side, and doing just one was painfully slow.

PRIVACY CONCERN: 

If you're concerned about such things, be aware that the scripts create a file to track which URLs you have previously scraped (not just the ones currently in the queue). This is to help prevent duplicates. If this worries you, delete the "download_history.txt" file or add a line at the end of gather.sh to do so automatically.

