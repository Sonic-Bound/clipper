# Clipper privacy policy

*Last updated: 18 November 2026*

Clipper is a free, open-source desktop app that runs entirely on your own Windows PC. This policy explains what happens to your data when you use it, including when you connect it to your YouTube channel.

## The short version

- Clipper has no servers and no accounts. The Clipper project never receives your videos, clips, chat data, Google account details or YouTube data.
- Everything Clipper makes or saves stays on your PC.
- If you connect YouTube, Clipper talks directly from your PC to Google, using a Google Cloud app that **you** created, only to upload the clips you choose to your own channel.

## Data Clipper handles on your PC

| Data | Why | Where it is kept |
|---|---|---|
| Video and live-chat data from the YouTube links you paste | To find the moments chat reacted to and cut clips | Temporary files in your clips folder, deleted after each job |
| Your clips, captions and `clips.json` | The clips you asked for | `Videos\Clipper` |
| Speech in the clips | Transcribed **on your PC** with Whisper to make captions; never uploaded | Inside the caption files |
| Settings | To remember your choices | The app's local storage and `%LOCALAPPDATA%\Clipper` |
| App log | To help you troubleshoot | `%LOCALAPPDATA%\Clipper\clipper.log` |

## YouTube connection (optional)

Clipper uses **YouTube API Services**. When you press **Connect YouTube**, you sign in on Google's own page and grant Clipper permission to:

- **Upload videos to your YouTube channel** (`youtube.upload`): only clips you choose to upload, either from a clip's card or because you turned on *Upload to YouTube* for a job.
- **See your YouTube account** (`youtube.readonly`): only to read your channel's name, so Clipper can show which channel is connected.

Clipper does not read, change or delete any other videos, comments, playlists, subscriptions or analytics.

What is stored, and where:

- The OAuth client ID and secret you entered, the channel name, and a refresh token are stored in `%LOCALAPPDATA%\Clipper\youtube.json` on your PC. The refresh token is encrypted with Windows (DPAPI), so only your Windows account on that PC can use it.
- A list of which clips were uploaded, with their YouTube video IDs, is kept in `%LOCALAPPDATA%\Clipper\uploads.json`.
- Nothing is sent anywhere other than Google's own servers.

To stop Clipper's access at any time, press **Disconnect** in Clipper (this deletes the stored token and revokes it with Google), or remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions). Uninstalling Clipper and deleting `%LOCALAPPDATA%\Clipper` removes everything it stored.

By using the YouTube features you agree to the [YouTube Terms of Service](https://www.youtube.com/t/terms). Google's handling of your data is described in the [Google Privacy Policy](https://policies.google.com/privacy).

## Sharing

Clipper does not sell, share or transfer any of your data. It has no analytics, tracking, ads or crash reporting.

## Children

Clipper is not directed at children under 13.

## Changes and contact

Changes to this policy are published in this file on GitHub, with the date at the top. Questions: open an issue at [github.com/Sonic-Bound/clipper/issues](https://github.com/Sonic-Bound/clipper/issues).
