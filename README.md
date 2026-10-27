# kerryhodgkins.com

Static site. No build step, no trackers.

## Open items
- After Nov 1: update the Sleepless Node card (Season 0 ends on Halloween). After Nov 4: ForeverArtisan moves from Beta to launched.
- New project or launch: add an <item> to feed.xml (newest first).

## Publish on GitHub Pages
1. New public repo, e.g. `kerryhodgkins/kerryhodgkins.com`; add `index.html`, `feed.xml` and `CNAME`.
2. Settings > Pages > Deploy from branch `main`, folder `/`.
3. At DreamHost DNS: four A records for `kerryhodgkins.com` -> 185.199.108.153, 185.199.109.153, 185.199.110.153, 185.199.111.153, and `www` CNAME -> `<github-user>.github.io`. Remove the old A record for the domain first.
4. Back in Settings > Pages, tick "Enforce HTTPS" once the certificate is issued.

## Email sign-up (subscribe.kerryhodgkins.com)
`_subscribe/` is a small service that runs on the VPS (Jekyll leaves underscore folders out of the site). Someone enters an email, gets a confirm link, and only after clicking it is added as a Resend contact in the segment "kerryhodgkins.com". Send the monthly update from Resend (Broadcasts); Resend adds the unsubscribe link.
- First time: DNS A record `subscribe` -> the VPS; verify kerryhodgkins.com in Resend (Domains); create the segment; then on the VPS: `sudo ./install.sh` (asks for the API key, hidden).
- After a change: `sudo ./install.sh update`. Tests: `python3 _subscribe/test_subscribe.py`.
- Cadence: about once a month (shipped / on the bench / one thing worth reading), plus the odd launch email. Never more than two a month.
