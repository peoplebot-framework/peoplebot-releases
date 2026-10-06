# Publishing peoplebot.me

Website source lives in `website/` on `main`. Mintlify documentation remains
separate. Only website files are sent to the restricted account's `/` directory.

Connection: explicit FTPS (AUTH TLS), `ftp.webcustoms.com`, port 21,
user `peoplebot@peoplebot.me`. Password: Actions secret
`PEOPLEBOT_FTP_PASSWORD`. TLS certificate validation is required. No SSH or
primary cPanel credential is used.

## First run

The first push that installs this workflow imports the current homepage,
robots.txt, sitemap.xml, llms.txt, and referenced static assets into website/.
It commits those exact bytes to GitHub before publishing and verifying them.
The import phase does not modify the web host.

Later changes to website/ merged or pushed to main publish automatically.
Workflow/script changes also run publishing, allowing deployment fixes to be tested.
To run manually: Actions → PeopleBot website → Run workflow → main.
Mode import only imports if source is absent; mode deploy publishes, importing
first if necessary. Existing source is never overwritten by a fresh import.
The import commit uses GITHUB_TOKEN and does not trigger a recursive workflow.

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

## Verified setup — October 5, 2026 (Pacific)

Initial import and deployment succeeded in Actions run 37421881366. Five files
were imported from the live host and committed as 1afcb3b: index.html, llms.txt,
peoplebot-logo.png, robots.txt, and sitemap.xml. Every upload was downloaded
and SHA-256 verified. No page content was edited during setup.

The server certificate covers chi200.greengeeks.net rather than the
ftp.webcustoms.com alias. curl uses chi200.greengeeks.net for TLS identity and
--connect-to to route to the same user-specified ftp.webcustoms.com endpoint.
Certificate validation stays enabled. Keep this mapping current if hosting moves.

Make future content changes in GitHub. Manual changes made on the host will not
be imported again automatically and may be overwritten by the next deployment.
