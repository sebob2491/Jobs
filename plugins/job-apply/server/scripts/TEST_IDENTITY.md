# Setting up the test identity for the nightly check

The nightly live check normally stops at each job site's sign-in page, because its throwaway
applicant has no inbox and practice mode never makes accounts. The **test identity** goes further:
a clearly fake applicant, "Jobdesk Test", with a real inbox of its own. With it, the nightly
"accounts" check makes an account at one or two employers per job system (the list is in
`test_identity_employers.yaml`, next to this file), reads the codes those sites email, and carries
on into the application. It **never submits** an application: practice mode stays on.

Until the three secrets below exist, the accounts check just says "not set up" and the nightly
stays green. It takes about 15 minutes to set up.

## Steps

1. **Make a new Gmail account just for this.** Go to accounts.google.com and choose "Create
   account". Name it Jobdesk Test, and pick an address you have never used for anything else
   (something like `jobdesk-check-` followed by a few random letters). Don't use your own email:
   job sites will send this inbox their sign-up emails every night.
2. **Turn on 2-Step Verification** for that new account: in the Google Account, open
   Security, then 2-Step Verification, and follow the steps.
3. **Make an app password** for it: in the Google Account, search for "App passwords" (it's
   under Security once 2-Step Verification is on). Name it "Job Desk live check" and press
   Create. Copy the 16-letter password it shows; you'll need it in step 5. (The check only
   reads sign-up codes with it.)
4. **Choose one strong password for the job sites.** The test identity uses the same password
   for its account on every job site. Make it at least 16 characters, with capital and small
   letters, a number and a symbol such as `!` or `#`, and no spaces, quotes or backslashes (some
   job sites refuse those). Don't use it anywhere else.
5. **Add the three secrets to the GitHub repository.** On GitHub, open the repository, then
   Settings, then Secrets and variables, then Actions, and press "New repository secret" for
   each of these. Type the name exactly as shown:

   | Name | Value |
   |---|---|
   | `LIVE_TEST_EMAIL` | the new Gmail address from step 1 |
   | `LIVE_TEST_EMAIL_PASSWORD` | the app password from step 3 |
   | `LIVE_TEST_SITE_PASSWORD` | the job-site password from step 4 |

6. **That's all.** The next nightly run includes the accounts check. To try it right away, open
   the repository's Actions tab, choose "live-nightly" and press "Run workflow". Its results
   show in the "Nightly live check" issue under "Apply pipeline with the test identity (accounts
   made and signed in)". The first night only records them; after that, a change gets a comment.

## Good to know

- The secrets are never printed: the check hides all three in everything it writes, and GitHub
  hides secrets in its logs too. The address can still show up in a screenshot of a form in the
  run's files, which is one reason it should be an inbox used for nothing else.
- To stop the accounts check, delete any one of the three secrets. To use a different job-site
  password later, change `LIVE_TEST_SITE_PASSWORD`: the check resets each site's password to the
  new one through the site's own "Forgot password" email, where the site lets it.
- If Google emails the test inbox about a sign-in it blocked, open that email and confirm it was
  you: the check reads the inbox from GitHub's computers.
- When an employer's run ends still waiting on an emailed code, the log has a `LIVE_INBOX` line:
  the inbox's mail of the last 15 minutes (its Spam folder too), each as the sender's domain,
  the subject (any email address shown as `<email>`, long numbers as `<digits>`), the time it
  arrived, whether the desk's sender check lets it through (`allowed`) and whether the desk can
  read a code from it (`code`). Never the address, the password or a message's text.
- To run it on your own computer, from `plugins/job-apply/server`, set the same three names in
  your terminal and run `uv run python scripts/live_smoke.py --pipeline --test-identity`.
