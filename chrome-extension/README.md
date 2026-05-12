# Chrome Extension Popup

The popup now has two live states based on an exact `/api/feed/urls?url=...` lookup for the active tab.

- If the URL is already in the feed, the popup shows Star and Thumbs Up toggles, a sibling note composer, a collapsed note preview, and an `Open in feed` link to `#r-{resource_id}`.
- If the URL is not in the feed, the popup keeps the add flow and now lets you attach an optional note at submission time.

`Surprise me`, badge syncing, and the options page flow are unchanged.
