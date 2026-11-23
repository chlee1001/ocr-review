# ocr-review

This Python receiver runs [OpenCodeReview](https://github.com/alibaba/open-code-review)
on signed Gitea pull request events and maintains one review summary per PR.
It is a standalone integration and does not require Gitea Actions. It does
not approve, merge, or reopen pull requests.

## Requirements

- Python 3.10+, Git, and OpenSSH on a machine that can reach Gitea and the
  configured model endpoint. Run it on a workstation or a VM; the receiver
  needs no third-party Python packages.
- OpenCodeReview Native v1.14.1, the version used for validation. Download
  the release for your OS and architecture and verify its published checksum.
- A dedicated service account for unattended operation, with read-only Git
  SSH access to the registered repositories. Verify the Gitea SSH host key
  out of band and install it in that account's `known_hosts`. Verify Git
  access with `git ls-remote` as the service account.

## Register repositories

1. Configure OCR for `ocr-review` with an approved `openai-responses` model and
   CI credential. Run `ocr llm test` as that account. The model receives the
   reviewed code. Do not reuse a personal ChatGPT session for shared reviews.
2. Give the Gitea bot access only to enrolled repositories. Use `read:repository`
   for pull request lookup and `write:issue` for summary comments. Verify native
   review permissions separately before relying on inline comments. Keep the
   token and webhook registration file readable only by the service account
   and its administrator.
3. Run the receiver as `ocr-review`. Use absolute paths for the script,
   OCR binary, and credential files. Use the HTTPS address of your Gitea
   instance and the SSH base from its clone URLs.

   ```sh
   OCR_GITEA_URL=https://gitea.example.org \
   OCR_GIT_SSH_BASE=ssh://git@gitea.example.org:2222 \
   OCR_BIN=/path/to/ocr \
   OCR_BOT_LOGIN=ocr-review-bot \
   OCR_WEBHOOK_SECRETS_FILE=/path/to/repository-secrets.json \
   OCR_GITEA_TOKEN_FILE=/path/to/bot-token \
   python3 /path/to/scripts/ocr_webhook.py --port 8765
   ```

4. Proxy `/gitea` from an internal HTTPS address to `127.0.0.1:8765`. Limit
   access to Gitea's network source. In Gitea, open the test repository's
   **Settings → Webhooks**. Add a **Gitea** webhook with that HTTPS URL and
   select pull request events. Generate a unique secret (`openssl rand -hex 32`)
   for each repository. Set it on that hook and add it to the receiver's JSON
   object under the exact repository name (for example, `example/project`).
   Restart the receiver to load changes. A valid hook for one repository cannot
   authorize another repository. Use the same endpoint for every registration.
5. Open a same-repository pull request with synthetic code. Look for the bot's
   👀 reaction below the pull request description during review. The bot
   removes the reaction when the review ends and posts one Markdown summary
   for the reviewed commit. Check that Korean findings show the impact and
   suggested fix, with the English text folded below. A verified source line
   opens in a new tab. Up to three high or critical findings on verified
   added lines can also appear on the native pull request diff. The summary
   retains findings when inline publishing is blocked or uncertain. A partial,
   skipped, or failed review is distinct from a completed review with no
   findings. Push a second synthetic commit and check that the bot updates
   the same summary and does not repost the same inline finding.

The webhook does not execute scripts from a pull request. Do not share a single
organization-wide webhook secret across repository registrations.

## Verify and switch to automatic reviews

- Run `python3 -m unittest discover -s tests` locally. For a delivery-only
  check, register exactly one sandbox repository and start the receiver with
  `--comment-smoke`. This posts a test summary without sending source to a model.
- The receiver binds to loopback. If Gitea runs on a different machine, put an
  HTTPS reverse proxy in front of it and restrict inbound traffic to Gitea's
  network source. An authenticated test-delivery `204` only confirms transport
  and signing; a qualifying PR delivery returns `202` when queued, not when
  review is finished.
- For unattended use, run the same command under the host service manager
  (systemd on Linux or launchd on macOS), using the service account, absolute
  paths and restart-on-failure. Keep the proxy and its access rules persistent.
- Validate signed delivery on a synthetic repository before switching. Remove
  `--comment-smoke` from the service command to enable model calls, restart the
  service, and use a new synthetic PR to verify delivery, model output, and
  comment updates end to end. Register production repositories only after
  these checks pass. Closed or already merged PRs are not replayed.
- Roll back by disabling the affected Gitea webhooks or stopping the receiver.
  Restoration of the previous service must include its matching configuration.
- Before publishing this repository, review both files and Git history. Do not
  include real registration files, tokens, local session state, private PR
  fixtures, or host-specific configuration. Choose and add a license before
  distributing it as open source.