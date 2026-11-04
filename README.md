# bsky-readwise

Automatically sync your **Bluesky bookmarks** ("Saved posts") into **Readwise** and **Readwise Reader**, like Readwise's built-in Twitter bookmark sync.

It's one Python file with no dependencies. Run it by hand, from cron, or every hour for free on GitHub Actions.

## What you get

For each post you save on Bluesky:

| Destination | What appears |
|---|---|
| **Readwise** (highlights) | A highlight in the *Tweets* category, filed under a "book" per author (e.g. *Alice on Bluesky*), as Readwise does for tweets. The highlight links back to the post, so it shows up in Daily Review, search and exports. |
| **Reader** | A document tagged `bluesky`, with the post's text, images and a link back to it. |

By default it sends to both. Each post includes:

- the full text, with links expanded (Bluesky shortens long URLs on screen)
- images, including multi-image posts, with their alt text
- quoted posts, shown inline with their author and link
- link preview cards (title and URL)
- with `--threads`, the author's follow-up posts when the post you saved starts a thread

## Quick start

1. **Bluesky app password.** In Bluesky, go to *Settings → Privacy and security → App passwords* and create one. Don't use your main password.
2. **Readwise token.** Copy it from <https://readwise.io/access_token>.
3. **Run it** (Python 3.9+):

   ```bash
   git clone https://github.com/htried/bsky-readwise && cd bsky-readwise
   export BSKY_HANDLE=you.bsky.social
   export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   export READWISE_TOKEN=your-readwise-token

   python3 bsky_readwise.py --dry-run -v   # show what would be sent, send nothing
   python3 bsky_readwise.py                # sync
   ```

The first run sends all your existing bookmarks. Later runs send only new ones.

## Run it automatically with GitHub Actions

The included workflow ([`.github/workflows/sync.yml`](.github/workflows/sync.yml)) syncs every hour.

1. **Fork** this repo (or use it as a template).
2. In your fork, go to *Settings → Secrets and variables → Actions*.
   - Under **Secrets**, add `BSKY_HANDLE`, `BSKY_APP_PASSWORD` and `READWISE_TOKEN`.
   - Under **Variables** (optional), set `DEST` to `both` (default), `readwise` or `reader`, and set `THREADS` to `true` to include threads.
3. Open the **Actions** tab, enable workflows if GitHub asks, and run **Sync Bluesky bookmarks to Readwise** once by hand to check it works.

GitHub turns off scheduled workflows in forks with no activity for 60 days. If syncing stops, re-enable the workflow in the Actions tab.

### Is it safe to run in a public fork?

Yes. Bluesky bookmarks are private, and the script avoids revealing them:

- Your credentials are stored as GitHub secrets, which never appear in logs.
- By default the logs show only counts ("3 new bookmark(s)"), never post URLs or text. Post URLs are logged only if you pass `-v`, so don't use `-v` in a public repo.
- Error messages have post IDs removed.
- The list of posts already sent (`state.json`, kept in the Actions cache between runs) stores keyed hashes, not post IDs. Without your app password, nobody can tell from it which posts you saved.

## Options

```
python3 bsky_readwise.py [--dest both|readwise|reader] [--threads] [--full]
                         [--state state.json] [--dry-run] [-v]
```

| Flag | Meaning |
|---|---|
| `--dest` | Where to send posts: `both` (default), `readwise` (highlights only) or `reader` (Reader only). |
| `--threads` | When a saved post starts a thread, include the author's follow-up posts. |
| `--full` | Check every page of bookmarks instead of stopping once it reaches ones already sent. |
| `--state` | Where to keep the list of posts already sent. Default: `state.json`. |
| `--dry-run` | Print what would be sent without sending anything or updating the state file. |
| `-v`, `--verbose` | Log each post's URL. |

| Environment variable | |
|---|---|
| `BSKY_HANDLE` | Your handle, e.g. `you.bsky.social`. |
| `BSKY_APP_PASSWORD` | A Bluesky app password. |
| `READWISE_TOKEN` | Your Readwise access token. Not needed for `--dry-run`. |
| `BSKY_SERVICE` | Optional. The server you log in through. Default: `https://bsky.social`. If your account is on a self-hosted PDS, use that server's address. |

## How it works

1. It logs in with your app password (`com.atproto.server.createSession`) and reads your bookmarks newest first with [`app.bsky.bookmark.getBookmarks`](https://docs.bsky.app/docs/api/app-bsky-bookmark-get-bookmarks). Bluesky bookmarks are private, so this request has to be signed in.
2. It stops paging at the first page with no new bookmarks.
3. It sends highlights to the [Readwise API](https://readwise.io/api_deets) (`POST /api/v2/highlights/`) and documents to the [Reader API](https://readwise.io/reader_api) (`POST /api/v3/save/`), oldest first. Reader saves are spaced out to stay under its rate limit of about 50 requests a minute.
4. It records which posts were sent in the state file.

**Running it again is safe.** Readwise ignores duplicate highlights and Reader ignores documents it already has with the same URL. Losing the state file or changing your app password therefore just re-sends everything once, without creating duplicates.

## Limitations

- Removing a bookmark on Bluesky doesn't remove it from Readwise.
- Videos appear as a thumbnail with a `[video]` marker, not as playable video.
- `--threads` follows the author's own replies down from the saved post. It doesn't add earlier posts when you saved a post from the middle of a thread.
- A highlight holds at most 8,191 characters, so very long threads are cut off in Readwise. Reader gets the full thread.

## License

MIT
