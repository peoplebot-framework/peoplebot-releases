# Publishing peoplebot.me

Website source lives in `website/` on `main`. Mintlify documentation remains
separate. Only website files are sent to the restricted account's `/` directory.

Connection: explicit FTPS (AUTH TLS), `ftp.webcustoms.com`, port 21,
user `peoplebot@peoplebot.me`. Password: Actions secret
`PEOPLEBOT_FTP_PASSWORD`. TLS certificate validation is required. No SSH or
primary cPanel credential is used.

## First run

Open Actions → PeopleBot website → Run workflow. Select `main` and mode `import`.
This reads the current homepage, robots.txt, sitemap.xml, llms.txt, and referenced
static assets, then commits them to `website/`. It does not modify the web host.
Import refuses to overwrite an existing `website/` directory.

After successful import, inspect the commit, then run mode `deploy` once.
The import commit uses GITHUB_TOKEN, so it does not itself trigger a push workflow.
Later changes to `website/` merged or pushed to `main` publish automatically.

The deployment uploads only `website/`, uploads index.html last, and verifies
every uploaded file by downloading it over FTPS and comparing SHA-256 hashes.
It never deletes remote files. Multi-file deployments are not atomic; a failed
deployment can leave a partially updated site. Correct the issue and rerun.
Removed source files require a separately authorized host-side deletion.

Review future changes in a branch/PR, then merge when ready to publish. To roll
back content, revert the relevant website commit and publish that revision.
HTTP crawler accessibility must be checked independently of FTPS transfer.

If curl returns exit 60, check the FTP hostname against the host's certificate;
do not disable certificate verification. Exit 67 usually means rejected login.
Branch rules may prevent the import commit; inspect the failed run rather than
loosening repository protection automatically.

The importer is scoped to this static homepage and referenced public assets,
not a general account backup. It does not copy hidden files, server-side code,
or other sites. Nested CSS asset layouts require adapting the importer first.
