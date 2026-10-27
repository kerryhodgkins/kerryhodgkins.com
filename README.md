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
