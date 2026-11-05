# YouTube packaging (Weekly Sync)

Rules for the full-episode **16:9 cover** (the YouTube thumbnail) and the **title**. Chapters, tags, hashtags, and the upload gate stay in `/sofit-seo`. Read this file before writing either piece, and again before the cover is shown for approval.

Weekly Sync is a Hebrew tech conversation (hosts Navot and Tor). A full episode does about 100 to a few hundred YouTube views. The package has to be recognizable at a glance. It will not be tuned by a stack of tests.

**Source.** Samarth, [Lessons from Working on a Top 1% Podcast (David Perell's How I Write)](https://heysamarth.com/essays/david-perell), also posted by [@samarthgowda215](https://x.com/samarthgowda215/status/2117352212418507047). That essay is how How I Write packaged a writing show (thumbnail promise, title frame, one visual system, credibility first, evergreen over trends). They made 15–20 thumbnail variations and ran as many as eight A/B tests per video. The sections below keep the principles and mark where Weekly Sync does it differently.

## The promise

The cover makes a promise. The title frames that promise. The first ~30 seconds deliver it.

YouTube hover-preview plays the opening on top of the thumbnail, so the still and the opening have to feel like the same shot: same people, same grade, same idea. Packaging is one component. The guest, the edit, and those opening seconds have to pull the same way.

One strong standalone clip, posted rarely, can lift the rest of the catalog. A clip every week is a different show.

## One visual system

The same system on every cover, so a viewer recognizes the show and clicks before reading the title. That system is the [Cover template](#cover-template) below. A variation changes the face, the two words, and the episode number. Layout, type, and color stay put.

Study outliers in a swipe file. A variation stays inside this template. Another channel's style, copied because it got views, breaks the row of covers.

Match the weight of the conversation. Most YouTube thumbnails shout (urgent, gadget, this week only). This show is a longer conversation. A serious face and two words beat a loud layout.

## Cover template

Direction B, "big face, two words", is the fixed Weekly Sync YouTube cover. Navot picked it. Every episode uses it.

- **Canvas.** 1280×720, JPG, under 2MB. YouTube rejects a larger thumbnail, and the uploader refuses it. The face and the two words have to stay readable when the cover is a phone thumbnail.
- **Face.** One large host face. Tor is the default. Use a natural, flattering full-resolution frame from the episode, and use that frame directly. A still that was already cropped does not get cropped again. The face fades into a navy field on one side. The fade sits beside the face: it does not cut the head, cover the eyes, or dissolve the face.
- **Words.** At most two Hebrew words, set huge, in Simpler Black, white. Those two words are the cover promise. The longer YouTube title is the line in [Titles](#titles) below.
- **Marks.** A thin pink strip along the bottom, the Weekly Sync badge, and a small pink `פרק N` tag with this episode's number. No frame and no border around the cover.
- **Files.** Simpler Black and the badge live in the machine-local brand kit (`~/.sofit/assets/`). Leave the font and the art there. They do not go in this repo.

## Titles

Hebrew. The show name is «וויקלי סינק», in the same place every week. "Weekly Sync" is a search tag, not the title. Stay under YouTube's 100-character cap (the SEO gate enforces it) and keep a similar length, so the line under the thumbnail looks like one show. Write a guest's name the same way every time.

- **Say the obvious credential.** If the guest has one a cold viewer would not already know, put it in the title. Ship that version before a cleverer line.
- **Broaden past the week's headline.** This is a news-driven show. Title the bigger question the episode actually argues. The headline expires; the question is what a later viewer can still click.
- **Prefer a concrete result.** On this show's clips, "I built / I found something with AI" plus a tangible result holds better than a gadget-opinion hook. Use that shape for the title and the cover text when the episode delivers it.
- **Frame it so it is still true later.** A trendy package gets a bump, then stalls. Evergreen framing gives the video time to find viewers.
- The title frames the cover, and both have to be true in the opening half-minute.

## One test, not a slate

How I Write could run 15–20 thumbnails and up to eight A/B tests because the channel had the impressions. This one does not. WS214's YouTube Test & Compare ran 14 days on 119 views and never picked a winner.

- Run at most one thumbnail test at a time.
- Choose from the template plus what the clip log already showed, and let a test confirm one open question.
- The next test waits until this one has a result or has been called off.
- Every option in that test still uses the Cover template above. Swap the two words or the frame. Leave the layout alone.
