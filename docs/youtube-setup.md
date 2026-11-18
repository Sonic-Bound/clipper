# Connecting Clipper to your YouTube channel

Clipper can publish every clip to your channel as soon as it is made. It uploads through **your own** Google Cloud app, so no one else (including the Clipper project) ever has access to your channel. The setup takes about 10 minutes and is free.

You need a Google account that owns a YouTube channel.

## 1. Create a Google Cloud project

1. Open **[Google Cloud Console → New project](https://console.cloud.google.com/projectcreate)**.
2. Give it any name, for example `Clipper`, and press **Create**.
3. Make sure the new project is selected in the project picker at the top of the page.

No billing account or payment details are needed.

## 2. Turn on the YouTube Data API

1. Open **[YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com)**.
2. Press **Enable**.

## 3. Set up the consent screen

1. Open **[Google Auth Platform](https://console.cloud.google.com/auth/overview)** and press **Get started**.
2. **App name:** `Clipper`. **User support email:** your email. Press **Next**.
3. **Audience:** choose **External**. Press **Next**.
4. **Contact information:** your email. Press **Next**, agree to the policy, and press **Create**.
5. Open **[Audience](https://console.cloud.google.com/auth/audience)** and press **Publish app**, then **Confirm**.

> [!IMPORTANT]
> Don't skip **Publish app**. While an app is in *Testing*, Google signs you out of it every 7 days and Clipper would stop uploading. Publishing does **not** make anything public or send the app for review; it only stops the 7-day sign-out.

## 4. Create the client ID

1. Open **[Clients](https://console.cloud.google.com/auth/clients)** and press **Create client**.
2. **Application type:** **Desktop app**. **Name:** `Clipper`. Press **Create**.
3. A window shows the **Client ID** and **Client secret**. Keep it open (or press **Download JSON**).

## 5. Connect in Clipper

1. In Clipper, press **YouTube** in the top-right corner.
2. Paste the **Client ID** and **Client secret**, and press **Save**.
3. Press **Connect YouTube**. Your browser opens Google's sign-in.
4. Choose the Google account that owns your channel. If you manage several channels, Google asks which one.
5. Google warns **"Google hasn't verified this app"**. That's expected: the app is the one you just made yourself. Click **Advanced**, then **Go to Clipper (unsafe)**.
6. Tick the boxes to allow uploading and press **Continue**.
7. The page says **Connected to *your channel***. Close the tab and go back to Clipper.

## Using it

- Turn on **Upload to YouTube** under the link box. Every clip that job makes is uploaded as soon as it is saved.
- Any clip in the gallery can also be uploaded from its card with **Upload to YouTube**.
- Each card shows the upload's progress, and **▶ On YouTube** opens the video when it's done.
- Vertical clips are uploaded as **Shorts**. The title comes from the stream, and the description quotes the clip's captions and links to the original video.
- **YouTube → New uploads are** chooses Private, Unlisted or Public (see the limits below).

## YouTube's limits

**New apps upload as private.** YouTube locks every video uploaded through a Google app that hasn't been through its [API compliance audit](https://support.google.com/youtube/contact/yt_api_form) to *Private*, whatever privacy you choose. Until your app passes the audit, open **[YouTube Studio → Content](https://studio.youtube.com)** and change each clip to Public yourself. You can apply for the audit with the form linked above; Google usually takes days to weeks to reply.

**About 6 uploads a day.** Each project gets 10,000 API quota units a day for free, and one upload costs 1,600 units. When the allowance runs out, the remaining clips show **"Daily YouTube limit"** and upload automatically after it resets at midnight Pacific time, as long as Clipper is open. More quota can be requested in the same audit form.

**Copyright.** Re-uploading another creator's stream can get your channel copyright strikes (three strikes deletes the channel). Upload clips of your own streams, or of creators who allow clip channels.

## Troubleshooting

| What you see | What to do |
|---|---|
| "That doesn't look like an OAuth client ID" | Copy the **Client ID**, not the project ID or the client name. It ends in `.apps.googleusercontent.com`. |
| Google shows "Access blocked: Clipper can only be used within its organization" (Error 403: org_internal) | The audience is set to *Internal*. Open [Audience](https://console.cloud.google.com/auth/audience), press **Make external**, then **Publish app**, and connect again. Your client ID stays the same. |
| Google shows "Access blocked: … has not completed the Google verification process" | The app is still in *Testing*. Do step 3.5 (**Publish app**), or add your Google account under **Audience → Test users**. |
| Google shows "Error 400: redirect_uri_mismatch" | The client isn't a **Desktop app**. Create a new client of type Desktop app (step 4). |
| "This Google account has no YouTube channel yet" | Sign in to [youtube.com](https://youtube.com) with that account and create a channel, then connect again. |
| Uploads say "YouTube sign-in expired" | Press **YouTube → Connect YouTube** again. This happens if you removed Clipper's access in your Google account, or if the app is still in *Testing*. |
| "Upload failed" on a card | Hover over it to see why, then press **Retry**. Clipper already retries network errors up to three times by itself. |

To remove Clipper's access to your channel at any time, press **Disconnect** in Clipper, or remove it at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
